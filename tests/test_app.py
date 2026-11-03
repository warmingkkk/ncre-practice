import io
import json
import sqlite3
import sys
import time
from pathlib import Path
import pytest
from openpyxl import Workbook
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import create_app
from importers import parse_students, parse_docx
from werkzeug.security import generate_password_hash


@pytest.fixture
def env(tmp_path,monkeypatch):
    monkeypatch.setenv('DATA_DIR',str(tmp_path))
    app=create_app({'TESTING':True,'DATABASE':str(tmp_path/'test.db')})
    con=sqlite3.connect(app.config['DATABASE']);con.row_factory=sqlite3.Row
    pw=generate_password_hash('TestPassword123')
    con.execute('UPDATE users SET password=?,must_change=0',(pw,))
    con.execute('INSERT INTO users(id,username,name,password,role,must_change) VALUES(2,"teacher","教师",?,"teacher",0)',(pw,))
    con.execute('INSERT INTO users(id,username,name,password,role,must_change) VALUES(3,"other","其他教师",?,"teacher",0)',(pw,))
    con.execute('INSERT INTO classes(id,name,teacher_id) VALUES(1,"测试班",2)')
    for uid in (4,5):con.execute('INSERT INTO users(id,username,name,password,role,class_id,must_change) VALUES(?,?,?,?,"student",1,0)',(uid,str(uid),'学生'+str(uid),pw))
    con.execute('INSERT INTO tasks(id,class_id,title,kind,question_ids,starts,ends,duration,threshold) VALUES(1,1,"每日练习","daily","[1,2]","2000-01-01T00:00","2099-01-01T00:00",10,100)')
    con.execute('INSERT INTO tasks(id,class_id,title,kind,question_ids,starts,ends,duration,threshold) VALUES(2,1,"模拟考","exam","[1,2]","2000-01-01T00:00","2099-01-01T00:00",10,100)')
    con.commit()
    yield app,con
    con.close()


def client(app,uid):
    c=app.test_client()
    with c.session_transaction() as s:s['uid']=uid;s['csrf']='token'
    return c


def test_security_and_pages(env):
    app,db=env
    assert app.test_client().get('/').status_code==302
    c=client(app,4)
    assert c.post('/practice',data={}).status_code==400
    assert c.get('/admin').status_code==403
    assert c.get('/classes/1').status_code==403
    assert c.get('/tasks/1/progress').status_code==403
    assert client(app,3).get('/classes/1').status_code==403
    for uid,paths in [(1,['/','/bank','/admin','/classes/1','/tasks/1/progress','/tasks/2/ranking']), (4,['/','/bank','/mistakes','/password'])]:
        for p in paths:assert client(app,uid).get(p).status_code==200,p
    assert c.get('/tasks/2/ranking').status_code==403


def test_daily_scoring_mistakes_repeat_and_privacy(env):
    app,db=env;c=client(app,4)
    start=c.post('/tasks/1/start',data={'csrf':'token'})
    path=start.location
    html=c.get(path).get_data(as_text=True)
    assert '正确答案' not in html and 'explanation' not in html
    assert client(app,5).get(path).status_code==403
    assert client(app,3).get(path).status_code==403
    assert c.post(path,data={'csrf':'token','1':'A'}).status_code==302
    a=db.execute('SELECT * FROM attempts').fetchone()
    assert not a['passed']
    assert db.execute('SELECT COUNT(*) FROM mistakes').fetchone()[0]>=1
    assert '正确答案' in c.get(path).get_data(as_text=True)
    c.post(path,data={'csrf':'token','1':'B'})
    assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==1
    second=c.post('/tasks/1/start',data={'csrf':'token'}).location
    assert second!=path
    answers={str(q['id']):q['answer'] for q in db.execute('SELECT * FROM questions WHERE id IN (1,2)')}
    c.post(second,data={'csrf':'token',**answers})
    assert db.execute('SELECT passed FROM attempts ORDER BY id DESC').fetchone()[0]==1
    assert db.execute('SELECT COUNT(*) FROM mistakes WHERE mastered=0').fetchone()[0]==0
    data=client(app,2).get('/tasks/1/progress?format=json').json
    assert data[0]['completed']==1 and data[0]['attempts']==2
    third=c.post('/tasks/1/start',data={'csrf':'token'}).location
    c.post(third,data={'csrf':'token'})
    assert client(app,2).get('/tasks/1/progress?format=json').json[0]['completed']==1


def test_exam_timeout_autosave_and_single_attempt(env):
    app,db=env;c=client(app,4)
    path=c.post('/tasks/2/start',data={'csrf':'token'}).location
    answer=db.execute('SELECT answer FROM questions WHERE id=1').fetchone()[0]
    assert c.post(path,data={'csrf':'token','save':'1','1':answer}).json['saved']
    db.execute('UPDATE attempts SET deadline=?',(time.time()-1,));db.commit()
    html=c.get(path).get_data(as_text=True)
    a=db.execute('SELECT * FROM attempts').fetchone()
    assert a['submitted'] and a['score']==1
    assert a['submitted']==a['deadline']
    assert '正确答案' not in html
    assert c.post('/tasks/2/start',data={'csrf':'token'}).location==path
    db.execute('UPDATE tasks SET ends="2001-01-01T00:00" WHERE id=2');db.commit()
    assert '正确答案' in c.get(path).get_data(as_text=True)
    assert c.get('/tasks/2/ranking').status_code==200


def test_import_two_column_and_atomic_rollback(env):
    app,db=env
    wb=Workbook();ws=wb.active;ws.append(['学号','姓名']);ws.append(['000001','新同学'])
    f=io.BytesIO();wb.save(f);f.seek(0)
    assert parse_students(f,'测试班')[0]['number']=='000001'
    f.seek(0)
    r=client(app,2).post('/classes/1/students',data={'csrf':'token','file':(f,'students.xlsx')})
    assert r.status_code==200
    u=db.execute('SELECT * FROM users WHERE username="000001"').fetchone()
    assert u and u['must_change']==1
    wb=Workbook();ws=wb.active;ws.append(['学号','姓名']);ws.append(['000002','新同学2']);ws.append(['teacher','冲突'])
    f=io.BytesIO();wb.save(f);f.seek(0)
    client(app,2).post('/classes/1/students',data={'csrf':'token','file':(f,'students.xlsx')})
    assert db.execute('SELECT id FROM users WHERE username="000002"').fetchone() is None


def test_login_password_change_and_reset(env):
    app,db=env;c=app.test_client();c.get('/login')
    with c.session_transaction() as s:token=s['csrf']
    assert c.post('/login',data={'csrf':token,'username':'4','password':'TestPassword123'}).status_code==302
    c.get('/')
    with c.session_transaction() as s:token=s['csrf']
    for _ in range(8):c.post('/login',data={'csrf':token,'username':'missing','password':'bad'})
    assert c.post('/login',data={'csrf':token,'username':'missing','password':'bad'}).status_code==429
    teacher=client(app,2);r=teacher.post('/users/4/reset',data={'csrf':'token'})
    assert r.status_code==200
    assert client(app,4).get('/').location.endswith('/login')


def test_seed_integrity():
    qs=json.loads((Path(__file__).resolve().parents[1]/'seed/questions.json').read_text(encoding='utf-8'))
    assert len(qs)==520 and len({q['category'] for q in qs})==10
    assert all(q['answer'] in 'ABCD' and len(q['options'])==4 and q['explanation'] for q in qs)
