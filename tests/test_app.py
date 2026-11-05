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
from werkzeug.security import generate_password_hash, check_password_hash


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
    assert db.execute('SELECT COUNT(*) FROM mistakes').fetchone()[0]==0
    assert '正确答案' not in c.get('/mistakes').get_data(as_text=True)
    assert c.post('/tasks/2/start',data={'csrf':'token'}).location==path
    db.execute('UPDATE tasks SET ends="2001-01-01T00:00" WHERE id=2');db.commit()
    assert '正确答案' in c.get(path).get_data(as_text=True)
    assert c.get('/tasks/2/ranking').status_code==200
    c.get('/mistakes')
    assert db.execute('SELECT COUNT(*) FROM mistakes').fetchone()[0]==1
    c.get('/mistakes')
    assert db.execute('SELECT SUM(wrong_count) FROM mistakes').fetchone()[0]==1


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
    assert u['password']!='123456' and check_password_hash(u['password'],'123456')
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


def test_question_create_delete_restore_permissions(env):
    app,db=env;admin=client(app,1)
    form=dict(csrf='token',category='新增测试分类',stem='新增测试题：哪个选项正确？',A='选项一',B='选项二',C='选项三',D='选项四',answer='B',explanation='选项二正确。')
    before=db.execute('SELECT COUNT(*) FROM questions').fetchone()[0]
    for uid in (2,4):
        c=client(app,uid)
        assert c.get('/bank/new').status_code==403
        assert c.post('/bank/new',data=form).status_code==403
        assert c.post('/bank/1/delete',data={'csrf':'token'}).status_code==403
        assert c.post('/bank/1/restore',data={'csrf':'token'}).status_code==403
        assert c.get('/bank?deleted=1').status_code==403
    invalid={**form,'answer':'E'}
    assert admin.post('/bank/new',data=invalid).status_code==200
    assert db.execute('SELECT COUNT(*) FROM questions').fetchone()[0]==before
    assert admin.post('/bank/new',data=form).status_code==302
    q=db.execute('SELECT * FROM questions WHERE category=?',(form['category'],)).fetchone()
    assert json.loads(q['options'])==['选项一','选项二','选项三','选项四']
    assert q['answer']=='B' and q['deleted_at'] is None
    admin.post('/bank/new',data=form)
    assert db.execute('SELECT COUNT(*) FROM questions').fetchone()[0]==before+1
    assert '选项四' in admin.get('/bank?q=新增测试题').get_data(as_text=True)
    path=client(app,4).post('/practice',data={'csrf':'token','category':form['category'],'count':1}).location
    aid=int(path.rsplit('/',1)[1])
    assert json.loads(db.execute('SELECT question_ids FROM attempts WHERE id=?',(aid,)).fetchone()[0])==[q['id']]
    assert admin.post(f'/bank/{q["id"]}/delete',data={'csrf':'token'}).status_code==302
    assert db.execute('SELECT deleted_at FROM questions WHERE id=?',(q['id'],)).fetchone()[0]
    assert form['stem'] not in admin.get('/bank?q=新增测试题').get_data(as_text=True)
    assert form['stem'] in admin.get('/bank?deleted=1&q=新增测试题').get_data(as_text=True)
    assert client(app,4).post('/practice',data={'csrf':'token','category':form['category'],'count':1}).location=='/bank'
    client(app,4).post(path,data={'csrf':'token',str(q['id']):'A'})
    assert client(app,4).post('/practice',data={'csrf':'token','mode':'wrong','count':100}).location=='/bank'
    assert admin.post(f'/bank/{q["id"]}/restore',data={'csrf':'token'}).status_code==302
    assert db.execute('SELECT deleted_at FROM questions WHERE id=?',(q['id'],)).fetchone()[0] is None
    assert client(app,4).post('/practice',data={'csrf':'token','mode':'wrong','count':100}).location.startswith('/attempts/')


def test_deleted_question_preserves_tasks_and_grading(env):
    app,db=env;admin=client(app,1);student=client(app,4)
    q=db.execute('SELECT * FROM questions WHERE id=1').fetchone()
    admin.post('/bank/1/delete',data={'csrf':'token'})
    path=student.post('/tasks/1/start',data={'csrf':'token'}).location
    assert q['stem'] in student.get(path).get_data(as_text=True)
    student.post(path,data={'csrf':'token','1':q['answer']})
    assert db.execute('SELECT score FROM attempts WHERE task_id=1').fetchone()[0]==1
    assert q['explanation'] in student.get(path).get_data(as_text=True)
    task_form=dict(csrf='token',title='不应发布',kind='daily',starts='2000-01-01T00:00',ends='2099-01-01T00:00',duration=40,threshold=100,count=1,questions='1')
    before=db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
    client(app,2).post('/classes/1/tasks',data=task_form)
    assert db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]==before
    practice=student.post('/practice',data={'csrf':'token','category':q['category'],'count':100}).location
    ids=json.loads(db.execute('SELECT question_ids FROM attempts WHERE id=?',(int(practice.rsplit('/',1)[1]),)).fetchone()[0])
    assert 1 not in ids
    assert 'name="questions" value="1"' not in client(app,2).get('/classes/1').get_data(as_text=True)


def test_existing_database_gets_question_migration(env):
    app,db=env
    db.execute('DROP INDEX active_question_categories')
    db.execute('ALTER TABLE questions DROP COLUMN deleted_at');db.commit()
    count=db.execute('SELECT COUNT(*) FROM questions').fetchone()[0]
    upgraded=create_app({'TESTING':True,'DATABASE':app.config['DATABASE']})
    assert client(upgraded,1).get('/bank').status_code==200
    assert db.execute('SELECT COUNT(*) FROM questions WHERE deleted_at IS NULL').fetchone()[0]==count


def test_single_student_default_password_and_first_login(env):
    app,db=env
    r=client(app,2).post('/classes/1/students',data={'csrf':'token','number':'000003','name':'新增学生'})
    assert r.status_code==200
    u=db.execute('SELECT * FROM users WHERE username="000003"').fetchone()
    assert check_password_hash(u['password'],'123456') and u['must_change']==1
    c=app.test_client();c.get('/login')
    with c.session_transaction() as s:token=s['csrf']
    c.post('/login',data={'csrf':token,'username':'000003','password':'123456'})
    assert c.get('/').location.endswith('/password')
    with c.session_transaction() as s:token=s['csrf']
    c.post('/password',data={'csrf':token,'old':'123456','password':'NewStudentPassword123'})
    u=db.execute('SELECT * FROM users WHERE username="000003"').fetchone()
    assert not u['must_change'] and check_password_hash(u['password'],'NewStudentPassword123')
    # Reimporting the same account must not revert a student's own password.
    client(app,2).post('/classes/1/students',data={'csrf':'token','number':'000003','name':'新增学生'})
    assert db.execute('SELECT password FROM users WHERE id=?',(u['id'],)).fetchone()[0]==u['password']


@pytest.mark.parametrize('actor',[1,2])
def test_student_password_reset_scope_and_unlock(env,actor):
    app,db=env
    old=client(app,4)
    db.execute('INSERT INTO login_limits VALUES("4",8,?)',(time.time()+900,));db.commit()
    assert client(app,3).post('/users/4/reset',data={'csrf':'token'}).status_code==403
    assert client(app,4).post('/users/4/reset',data={'csrf':'token'}).status_code==403
    assert client(app,2).post('/users/3/reset',data={'csrf':'token'}).status_code==403
    assert client(app,actor).post('/users/4/reset',data={'csrf':'token'}).status_code==200
    u=db.execute('SELECT * FROM users WHERE id=4').fetchone()
    assert check_password_hash(u['password'],'123456') and u['must_change']==1
    assert db.execute('SELECT * FROM login_limits WHERE username="4"').fetchone() is None
    assert old.get('/').location.endswith('/login')
    c=app.test_client();c.get('/login')
    with c.session_transaction() as s:token=s['csrf']
    assert c.post('/login',data={'csrf':token,'username':'4','password':'123456'}).status_code==302
    assert c.get('/').location.endswith('/password')
