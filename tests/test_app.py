import io
import json
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
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


def test_new_task_is_unanswered_until_student_starts(env):
    app,db=env;teacher=client(app,2)
    form=dict(csrf='token',title='新发布任务状态测试',kind='daily',starts='2000-01-01T00:00',ends='2099-01-01T00:00',duration=40,threshold=100,count=2,questions=['1','2'])
    teacher.post('/classes/1/tasks',data=form)
    tid=db.execute('SELECT MAX(id) FROM tasks').fetchone()[0]
    assert db.execute('SELECT COUNT(*) FROM attempts WHERE task_id=?',(tid,)).fetchone()[0]==0
    for _ in range(2):
        assert client(app,4).get('/').status_code==200
        progress=teacher.get(f'/tasks/{tid}/progress?format=json').json
        assert all(s['active']==0 and s['attempts']==0 for s in progress)
        assert all(s['status']=='未作答' for s in progress)
        assert '未作答' in teacher.get(f'/tasks/{tid}/progress').get_data(as_text=True)
        assert '未作答' in client(app,4).get('/').get_data(as_text=True)
    assert db.execute('SELECT COUNT(*) FROM attempts WHERE task_id=?',(tid,)).fetchone()[0]==0
    student=client(app,4)
    path=student.post(f'/tasks/{tid}/start',data={'csrf':'token'}).location
    progress=teacher.get(f'/tasks/{tid}/progress?format=json').json
    assert progress[0]['active']==1 and progress[1]['active']==0
    assert progress[0]['status']=='作答中' and progress[1]['status']=='未作答'
    assert '继续作答' in student.get('/').get_data(as_text=True)
    student.post(path,data={'csrf':'token'})
    progress=teacher.get(f'/tasks/{tid}/progress?format=json').json
    assert progress[0]['active']==0 and progress[0]['attempts']==1
    assert progress[1]['active']==0 and progress[1]['attempts']==0
    assert progress[0]['status']=='待达标' and progress[1]['status']=='未作答'


def test_created_teacher_login_and_forced_password_change(env):
    app,db=env
    response=client(app,1).post('/classes',data=dict(csrf='token',name='新班级',username='new-teacher',teacher='新班主任'))
    assert response.status_code==200
    initial=re.search(r'<code>([^<]+)</code>',response.get_data(as_text=True)).group(1)
    assert initial!='123456'
    c=app.test_client();c.get('/login')
    with c.session_transaction() as s:token=s['csrf']
    invalid=c.post('/login',data=dict(csrf=token,username='new-teacher',password='123456'))
    assert '账号或密码不正确' in invalid.get_data(as_text=True)
    response=c.post('/login',data=dict(csrf=token,username='new-teacher',password=initial),follow_redirects=True)
    assert response.request.path=='/password'
    assert '已成功登录班主任账号 new-teacher' in response.get_data(as_text=True)
    with c.session_transaction() as s:token=s['csrf']
    response=c.post('/password',data=dict(csrf=token,old=initial,password='NewTeacherPassword123'),follow_redirects=True)
    assert response.request.path=='/' and '新班级' in response.get_data(as_text=True)
    cid=db.execute('SELECT id FROM classes WHERE name="新班级"').fetchone()[0]
    assert c.get(f'/classes/{cid}').status_code==200


def test_creating_teacher_clears_old_login_lock(env):
    app,db=env
    db.execute('INSERT INTO login_limits VALUES("new-teacher",8,?)',(time.time()+900,));db.commit()
    response=client(app,1).post('/classes',data=dict(csrf='token',name='新班级',username='new-teacher',teacher='新班主任'))
    initial=re.search(r'<code>([^<]+)</code>',response.get_data(as_text=True)).group(1)
    c=app.test_client();c.get('/login')
    with c.session_transaction() as s:token=s['csrf']
    assert c.post('/login',data=dict(csrf=token,username='new-teacher',password=initial)).status_code==302


def test_admin_can_choose_and_reset_teacher_initial_password(env):
    app,db=env;admin=client(app,1)
    data=dict(csrf='token',name='指定密码班',username='chosen-teacher',teacher='指定教师',initial_password='123456')
    admin.post('/classes',data=data)
    assert db.execute('SELECT id FROM users WHERE username="chosen-teacher"').fetchone() is None
    initial='TeacherInitial123'
    response=admin.post('/classes',data={**data,'initial_password':initial})
    assert response.status_code==200 and initial in response.get_data(as_text=True)
    u=db.execute('SELECT * FROM users WHERE username="chosen-teacher"').fetchone()
    assert u['must_change'] and check_password_hash(u['password'],initial)
    c=app.test_client();c.get('/login')
    with c.session_transaction() as s:token=s['csrf']
    assert c.post('/login',data=dict(csrf=token,username='chosen-teacher',password=initial),follow_redirects=True).request.path=='/password'
    reset='TeacherReset456'
    assert client(app,2).post(f'/users/{u["id"]}/reset',data=dict(csrf='token',initial_password=reset)).status_code==403
    admin.post(f'/users/{u["id"]}/reset',data=dict(csrf='token',initial_password=reset))
    updated=db.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone()
    assert check_password_hash(updated['password'],reset) and updated['must_change']
    assert c.get('/').location=='/login'
    c.get('/login')
    with c.session_transaction() as s:token=s['csrf']
    assert c.post('/login',data=dict(csrf=token,username='chosen-teacher',password=reset),follow_redirects=True).request.path=='/password'


def test_student_edit_preserves_records_and_enforces_class_scope(env):
    app,db=env;student=client(app,4)
    path=student.post('/tasks/1/start',data={'csrf':'token'}).location
    student.post(path,data={'csrf':'token'})
    password=db.execute('SELECT password FROM users WHERE id=4').fetchone()[0]
    assert client(app,3).get('/students/4/edit').status_code==403
    assert client(app,4).get('/students/4/edit').status_code==403
    assert client(app,2).get('/students/4/edit').status_code==200
    teacher=client(app,2)
    teacher.post('/students/4/edit',data=dict(csrf='token',name='修改后姓名',number='5',class_id=1))
    assert db.execute('SELECT username FROM users WHERE id=4').fetchone()[0]=='4'
    assert teacher.post('/students/4/edit',data=dict(csrf='token',name='修改后姓名',number='000004',class_id=1)).status_code==302
    u=db.execute('SELECT * FROM users WHERE id=4').fetchone()
    assert u['name']=='修改后姓名' and u['username']=='000004' and u['password']==password
    assert db.execute('SELECT user_id FROM attempts WHERE id=?',(int(path.rsplit('/',1)[1]),)).fetchone()[0]==4
    assert student.get('/').location=='/login'
    db.execute('INSERT INTO classes(id,name,teacher_id) VALUES(2,"另一个班",3)');db.commit()
    assert teacher.post('/students/4/edit',data=dict(csrf='token',name=u['name'],number=u['username'],class_id=2)).status_code==403
    assert db.execute('SELECT class_id FROM users WHERE id=4').fetchone()[0]==1
    assert client(app,1).post('/students/4/edit',data=dict(csrf='token',name=u['name'],number=u['username'],class_id=2)).status_code==302
    assert db.execute('SELECT class_id FROM users WHERE id=4').fetchone()[0]==2
    assert teacher.get('/students/4/edit').status_code==403
    assert client(app,3).get('/students/4/edit').status_code==200


def test_student_delete_restore_disables_login_and_preserves_history(env):
    app,db=env;teacher=client(app,2);student=client(app,4)
    path=student.post('/tasks/1/start',data={'csrf':'token'}).location
    student.post(path,data={'csrf':'token'})
    assert client(app,3).post('/students/4/delete',data={'csrf':'token'}).status_code==403
    assert client(app,4).post('/students/5/delete',data={'csrf':'token'}).status_code==403
    assert teacher.post('/students/4/delete',data={'csrf':'token'}).status_code==302
    assert db.execute('SELECT deleted_at FROM users WHERE id=4').fetchone()[0]
    assert db.execute('SELECT COUNT(*) FROM attempts WHERE user_id=4').fetchone()[0]==1
    assert student.get('/').location=='/login'
    assert all(r['id']!=4 for r in teacher.get('/tasks/1/progress?format=json').json)
    assert '/students/4/edit' not in teacher.get('/classes/1').get_data(as_text=True)
    assert '/students/4/restore' in teacher.get('/classes/1?students=deleted').get_data(as_text=True)
    assert teacher.post('/users/4/reset',data={'csrf':'token'}).status_code==404
    c=app.test_client();c.get('/login')
    with c.session_transaction() as s:token=s['csrf']
    r=c.post('/login',data=dict(csrf=token,username='4',password='TestPassword123'))
    assert '账号或密码不正确' in r.get_data(as_text=True)
    db.execute('INSERT OR REPLACE INTO login_limits VALUES("4",8,?)',(time.time()+900,));db.commit()
    assert teacher.post('/students/4/restore',data={'csrf':'token'}).status_code==302
    assert db.execute('SELECT deleted_at FROM users WHERE id=4').fetchone()[0] is None
    assert db.execute('SELECT * FROM login_limits WHERE username="4"').fetchone() is None
    c.get('/login')
    with c.session_transaction() as s:token=s['csrf']
    assert c.post('/login',data=dict(csrf=token,username='4',password='TestPassword123'),follow_redirects=True).request.path=='/'
    assert db.execute('SELECT COUNT(*) FROM attempts WHERE user_id=4').fetchone()[0]==1


def test_daily_task_edit_before_and_after_start(env):
    app,db=env;teacher=client(app,2)
    data=dict(csrf='token',title='修改后的每日练习',starts='2000-01-01T00:00',ends='2099-01-01T00:00',threshold=50,selection='manual',questions=['3','4'])
    assert teacher.get('/tasks/1/edit').status_code==200
    assert client(app,3).get('/tasks/1/edit').status_code==403
    assert client(app,4).post('/tasks/1/edit',data=data).status_code==403
    assert teacher.get('/tasks/2/edit').status_code==200
    assert teacher.post('/tasks/1/edit',data=data).status_code==302
    task=db.execute('SELECT * FROM tasks WHERE id=1').fetchone()
    assert task['title']==data['title'] and json.loads(task['question_ids'])==[3,4] and task['threshold']==50
    invalid={**data,'ends':'1999-01-01T00:00'}
    assert teacher.post('/tasks/1/edit',data=invalid).status_code==200
    assert db.execute('SELECT ends FROM tasks WHERE id=1').fetchone()[0]==data['ends']
    student=client(app,4);path=student.post('/tasks/1/start',data={'csrf':'token'}).location
    html=teacher.get('/tasks/1/edit').get_data(as_text=True)
    assert '题目与达标率已锁定' in html and 'name="selection"' not in html
    assert teacher.post('/tasks/1/edit',data={**data,'threshold':100,'questions':['1','2']}).status_code==200
    task=db.execute('SELECT * FROM tasks WHERE id=1').fetchone()
    assert json.loads(task['question_ids'])==[3,4] and task['threshold']==50
    assert teacher.post('/tasks/1/edit',data=dict(csrf='token',title='只修改名称时间',starts=data['starts'],ends=data['ends'])).status_code==302
    answer=db.execute('SELECT answer FROM questions WHERE id=3').fetchone()[0]
    student.post(path,data={'csrf':'token','3':answer})
    a=db.execute('SELECT * FROM attempts WHERE task_id=1').fetchone()
    assert a['total']==2 and a['score']==1 and a['passed']==1


def test_daily_task_delete_restore_preserves_active_attempt(env):
    app,db=env;teacher=client(app,2);student=client(app,4)
    db.execute('UPDATE tasks SET title="要删除的测试练习" WHERE id=1');db.commit()
    path=student.post('/tasks/1/start',data={'csrf':'token'}).location
    assert client(app,3).post('/tasks/1/delete',data={'csrf':'token'}).status_code==403
    assert client(app,4).post('/tasks/1/delete',data={'csrf':'token'}).status_code==403
    assert teacher.post('/tasks/1/delete',data={'csrf':'token'}).status_code==302
    assert db.execute('SELECT deleted_at FROM tasks WHERE id=1').fetchone()[0]
    assert '要删除的测试练习' not in student.get('/').get_data(as_text=True)
    assert '要删除的测试练习' in teacher.get('/classes/1?tasks=deleted').get_data(as_text=True)
    assert client(app,5).post('/tasks/1/start',data={'csrf':'token'}).status_code==400
    assert teacher.get('/tasks/1/edit').status_code==400
    answers={str(q['id']):q['answer'] for q in db.execute('SELECT * FROM questions WHERE id IN (1,2)')}
    assert student.post(path,data={'csrf':'token',**answers}).status_code==302
    a=db.execute('SELECT * FROM attempts WHERE task_id=1').fetchone()
    assert a['score']==2 and a['passed']==1
    assert teacher.post('/tasks/1/restore',data={'csrf':'token'}).status_code==302
    assert db.execute('SELECT deleted_at FROM tasks WHERE id=1').fetchone()[0] is None
    assert teacher.get('/tasks/1/progress?format=json').json[0]['completed']==1
    assert '要删除的测试练习' in student.get('/').get_data(as_text=True)


def test_task_edit_and_first_start_are_serialized(env):
    app,db=env
    for i in range(3):
        tid=db.execute('INSERT INTO tasks(class_id,title,kind,question_ids,starts,ends,duration,threshold) VALUES(1,?,"daily","[1,2]","2000-01-01T00:00","2099-01-01T00:00",40,100)',('并发测试'+str(i),)).lastrowid;db.commit()
        barrier=Barrier(2)
        def edit():
            c=client(app,2);barrier.wait()
            return c.post(f'/tasks/{tid}/edit',data=dict(csrf='token',title='修改题组',starts='2000-01-01T00:00',ends='2099-01-01T00:00',selection='manual',questions=['3','4'],threshold=50)).status_code
        def start():
            c=client(app,4);barrier.wait()
            return c.post(f'/tasks/{tid}/start',data={'csrf':'token'}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            edit_future=pool.submit(edit);start_future=pool.submit(start)
            assert edit_future.result() in (200,302) and start_future.result()==302
        task=db.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
        a=db.execute('SELECT * FROM attempts WHERE task_id=?',(tid,)).fetchone()
        assert json.loads(a['question_ids'])==json.loads(task['question_ids'])


def test_existing_database_gets_student_and_task_trash_columns(env):
    app,db=env
    db.execute('ALTER TABLE users DROP COLUMN deleted_at');db.execute('ALTER TABLE tasks DROP COLUMN deleted_at');db.commit()
    upgraded=create_app({'TESTING':True,'DATABASE':app.config['DATABASE']})
    assert client(upgraded,2).get('/classes/1').status_code==200
    assert client(upgraded,4).get('/').status_code==200
    assert db.execute('SELECT COUNT(*) FROM users WHERE deleted_at IS NULL').fetchone()[0]==5


def test_upgrade_preserves_admin_password_and_login(env,monkeypatch):
    app,db=env
    original=db.execute('SELECT * FROM users WHERE username="admin"').fetchone()
    counts={table:db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ('classes','users','tasks')}
    db.execute('ALTER TABLE users DROP COLUMN deleted_at');db.execute('ALTER TABLE tasks DROP COLUMN deleted_at');db.commit()
    monkeypatch.setenv('ADMIN_PASSWORD','DifferentEnvironmentPassword123')
    upgraded=create_app({'TESTING':True,'DATABASE':app.config['DATABASE']})
    current=db.execute('SELECT * FROM users WHERE username="admin"').fetchone()
    assert current['password']==original['password'] and current['id']==original['id']
    assert check_password_hash(current['password'],'TestPassword123')
    assert not check_password_hash(current['password'],'DifferentEnvironmentPassword123')
    for table,count in counts.items(): assert db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==count
    c=upgraded.test_client();c.get('/login')
    with c.session_transaction() as s:token=s['csrf']
    response=c.post('/login',data=dict(csrf=token,username='admin',password='TestPassword123'),follow_redirects=True)
    assert response.request.path=='/' and c.get('/admin').status_code==200


def test_admin_console_recovery_backs_up_and_preserves_school_data(env):
    from scripts.admin_access import status,reset_admin
    app,db=env
    student=client(app,4);path=student.post('/tasks/1/start',data={'csrf':'token'}).location
    student.post(path,data={'csrf':'token'})
    original=db.execute('SELECT password FROM users WHERE id=1').fetchone()[0]
    student_password=db.execute('SELECT password FROM users WHERE id=4').fetchone()[0]
    counts={table:db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ('classes','users','tasks','attempts','questions','mistakes')}
    old_admin=client(app,1)
    db.execute('INSERT INTO login_limits VALUES("admin",8,?)',(time.time()+900,));db.commit()
    info=status(app.config['DATABASE'],environment_password='TestPassword123')
    assert info['locked'] and info['matches_environment_password']
    result=reset_admin(app.config['DATABASE'],'admin','RecoveredAdminPassword123')
    with sqlite3.connect(result['backup']) as backup:
        assert backup.execute('SELECT password FROM users WHERE id=1').fetchone()[0]==original
    for table,count in counts.items(): assert db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==count
    assert db.execute('SELECT password FROM users WHERE id=4').fetchone()[0]==student_password
    assert old_admin.get('/admin').location=='/login'
    assert not status(app.config['DATABASE'])['locked']
    c=app.test_client();c.get('/login')
    with c.session_transaction() as s:token=s['csrf']
    assert c.post('/login',data=dict(csrf=token,username='admin',password='RecoveredAdminPassword123'),follow_redirects=True).request.path=='/password'
    with c.session_transaction() as s:token=s['csrf']
    assert c.post('/password',data=dict(csrf=token,old='RecoveredAdminPassword123',password='NewAdminPassword456'),follow_redirects=True).request.path=='/'
    assert c.get('/admin').status_code==200


def test_admin_recovery_refuses_missing_database_and_other_roles(env,tmp_path):
    from scripts.admin_access import status,reset_admin
    app,db=env
    missing=tmp_path/'missing.db'
    with pytest.raises(ValueError): reset_admin(missing,'admin','RecoveryPassword123')
    assert not missing.exists()
    before=db.execute('SELECT password FROM users WHERE id=2').fetchone()[0]
    with pytest.raises(ValueError): reset_admin(app.config['DATABASE'],'teacher','RecoveryPassword123')
    assert db.execute('SELECT password FROM users WHERE id=2').fetchone()[0]==before
    with pytest.raises(ValueError): reset_admin(app.config['DATABASE'],'admin','123456')


def test_admin_recovery_cli_environment_password_is_not_printed(env,tmp_path,monkeypatch,capsys):
    from scripts.admin_access import main
    app,db=env
    with sqlite3.connect(tmp_path/'practice.db') as target: db.backup(target)
    password='EnvironmentRecoveryPassword123'
    monkeypatch.setenv('ADMIN_PASSWORD',password)
    assert main(['reset','--from-env'])==0
    output=capsys.readouterr().out
    assert password not in output and json.loads(output)['must_change']
    assert main(['status'])==0
    info=json.loads(capsys.readouterr().out)
    assert info['matches_environment_password'] and not info['locked']


def test_exam_edit_locks_timing_and_questions_after_start(env):
    app,db=env;teacher=client(app,2)
    data=dict(csrf='token',title='新版模拟考',starts='2000-01-01T00:00',ends='2099-01-01T00:00',duration=20,threshold=50,selection='manual',questions=['3','4'])
    assert client(app,3).post('/tasks/2/edit',data=data).status_code==403
    assert client(app,4).post('/tasks/2/edit',data=data).status_code==403
    assert teacher.post('/tasks/2/edit',data=data).status_code==302
    assert db.execute('SELECT duration FROM tasks WHERE id=2').fetchone()[0]==20
    student=client(app,4);path=student.post('/tasks/2/start',data={'csrf':'token'}).location
    attempt=db.execute('SELECT * FROM attempts WHERE task_id=2').fetchone()
    assert json.loads(attempt['question_ids'])==[3,4]
    assert 1199<=attempt['deadline']-attempt['started']<=1201
    for changes in ({'duration':21},{'ends':'2098-01-01T00:00'},{'starts':'2001-01-01T00:00'},{'questions':['1']},{'threshold':100}):
        assert teacher.post('/tasks/2/edit',data={**dict(csrf='token',title='不应保存',starts=data['starts'],ends=data['ends']),**changes}).status_code==200
        assert db.execute('SELECT title FROM tasks WHERE id=2').fetchone()[0]=='新版模拟考'
    assert teacher.post('/tasks/2/edit',data=dict(csrf='token',title='更正考试名称')).status_code==200  # dates remain required
    assert teacher.post('/tasks/2/edit',data=dict(csrf='token',title='更正考试名称',starts=data['starts'],ends=data['ends'],duration=20)).status_code==302
    assert '/tasks/2/delete' in teacher.get('/classes/1').get_data(as_text=True)
    assert teacher.post('/tasks/2/delete',data={'csrf':'token'}).status_code==302
    assert client(app,5).post('/tasks/2/start',data={'csrf':'token'}).status_code==400
    assert student.post(path,data={'csrf':'token'}).status_code==302
    assert teacher.post('/tasks/2/restore',data={'csrf':'token'}).status_code==302
    assert db.execute('SELECT COUNT(*) FROM attempts WHERE task_id=2').fetchone()[0]==1
    assert student.post('/tasks/2/start',data={'csrf':'token'}).location==path


def test_class_edit_and_delete_cascades_accounts_and_keeps_history(env):
    app,db=env;admin=client(app,1);teacher=client(app,2);student=client(app,4)
    original=db.execute('SELECT password FROM users WHERE id=2').fetchone()[0]
    path=student.post('/tasks/1/start',data={'csrf':'token'}).location
    student.post(path,data={'csrf':'token'})
    data=dict(csrf='token',name='改名后的班级',teacher='新教师姓名',username='newteacher')
    assert teacher.post('/classes/1/edit',data=data).status_code==403
    assert admin.get('/classes/1/edit').status_code==200
    assert admin.post('/classes/1/edit',data={**data,'username':'admin'}).status_code==200
    assert db.execute('SELECT name FROM classes WHERE id=1').fetchone()[0]=='测试班'
    assert admin.post('/classes/1/edit',data=data).status_code==302
    assert teacher.get('/').location=='/login'
    assert db.execute('SELECT password FROM users WHERE id=2').fetchone()[0]==original
    assert db.execute('SELECT name FROM classes WHERE id=1').fetchone()[0]==data['name']
    teacher=app.test_client();teacher.get('/login')
    with teacher.session_transaction() as session:token=session['csrf']
    assert teacher.post('/login',data=dict(csrf=token,username='newteacher',password='TestPassword123')).status_code==302
    assert teacher.get('/classes/1').status_code==200
    assert client(app,3).post('/classes/1/delete',data={'csrf':'token'}).status_code==403
    assert student.post('/classes/1/delete',data={'csrf':'token'}).status_code==403
    assert admin.post('/classes/1/delete',data={'csrf':'token'}).status_code==302
    assert student.get('/').location=='/login' and teacher.get('/').location=='/login'
    assert db.execute('SELECT COUNT(*) FROM users WHERE id IN (2,4,5) AND deleted_at IS NOT NULL').fetchone()[0]==3
    assert db.execute('SELECT COUNT(*) FROM users WHERE id IN (1,3) AND deleted_at IS NULL').fetchone()[0]==2
    assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==1
    assert db.execute('SELECT COUNT(*) FROM tasks WHERE deleted_at IS NOT NULL').fetchone()[0]==2
    assert data['name'] not in admin.get('/').get_data(as_text=True)
    for url in ('/classes/1','/classes/1/edit','/tasks/1/edit'):
        assert admin.get(url).status_code==404
    assert admin.post('/students/4/restore',data={'csrf':'token'}).status_code==404
    assert admin.post('/classes/1/students',data=dict(csrf='token',number='6',name='不能新增')).status_code==404
    c=app.test_client();c.get('/login')
    with c.session_transaction() as session:token=session['csrf']
    for username in ('newteacher','4','5'):
        assert '账号或密码不正确' in c.post('/login',data=dict(csrf=token,username=username,password='TestPassword123')).get_data(as_text=True)


def test_class_soft_delete_migration_and_shared_teacher_protection(env):
    app,db=env
    db.execute('ALTER TABLE classes DROP COLUMN deleted_at');db.commit()
    upgraded=create_app({'TESTING':True,'DATABASE':app.config['DATABASE']})
    assert client(upgraded,1).get('/classes/1/edit').status_code==200
    assert db.execute('SELECT COUNT(*) FROM classes WHERE deleted_at IS NULL').fetchone()[0]==1
    db.execute('INSERT INTO classes(name,teacher_id) VALUES("同一班主任的另一个班",2)');db.commit()
    assert client(upgraded,1).post('/classes/1/delete',data={'csrf':'token'}).status_code==400
    assert db.execute('SELECT deleted_at FROM users WHERE id=2').fetchone()[0] is None
