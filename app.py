import csv
import io
import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from functools import wraps
from flask import Flask, g, session, request, redirect, url_for, render_template, abort, flash, jsonify, Response
from werkzeug.security import generate_password_hash, check_password_hash
from importers import parse_docx, parse_students

ROOT = Path(__file__).resolve().parent
STUDENT_INITIAL_PASSWORD = '123456'


def create_app(config=None):
    app = Flask(__name__)
    data = Path(os.environ.get('DATA_DIR', ROOT / 'data'))
    data.mkdir(parents=True, exist_ok=True)
    secret_file = data / 'secret'
    if not secret_file.exists():
        try:
            with secret_file.open('x') as f:
                f.write(secrets.token_hex(32))
        except FileExistsError:
            pass
    app.config.update(SECRET_KEY=os.environ.get('SECRET_KEY') or secret_file.read_text(), DATABASE=str(data/'practice.db'), MAX_CONTENT_LENGTH=8*1024*1024, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE') == '1', PERMANENT_SESSION_LIFETIME=timedelta(hours=12))
    if config:
        app.config.update(config)

    def db():
        if 'db' not in g:
            g.db = sqlite3.connect(app.config['DATABASE'], timeout=20)
            g.db.row_factory = sqlite3.Row
            g.db.execute('PRAGMA foreign_keys=ON')
        return g.db

    def rows(sql, args=()):
        return db().execute(sql,args).fetchall()

    def one(sql, args=()):
        return db().execute(sql,args).fetchone()

    def now():
        return datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%dT%H:%M')

    def task_status(record):
        return '已达标' if record['completed'] else ('作答中' if record['active'] else ('待达标' if record['attempts'] else '未作答'))

    def managed_student(uid, deleted=False):
        student=one('SELECT * FROM users WHERE id=? AND role="student"',(uid,))
        if not student: abort(404)
        classroom(student['class_id']);require_teacher()
        if student['deleted_at'] and not deleted: abort(404)
        return student

    def available_classes():
        return rows('SELECT id,name FROM classes ORDER BY name') if g.user['role']=='admin' else rows('SELECT id,name FROM classes WHERE teacher_id=? ORDER BY name',(g.user['id'],))

    def task_configuration(form, existing=None, locked=False):
        title=form.get('title','').strip()
        kind=existing['kind'] if existing else form.get('kind','daily')
        starts=datetime.fromisoformat(form.get('starts',''))
        ends=datetime.fromisoformat(form.get('ends',''))
        if not title or len(title)>100 or kind not in ('daily','exam') or starts.tzinfo or ends.tzinfo or ends<=starts:
            raise ValueError('请检查任务名称和开放时间')
        if locked:
            if ('threshold' in form and int(form['threshold'])!=existing['threshold']) or any(k in form for k in ('selection','category','count')):
                raise ValueError('已有学生开始作答，题目和达标率已锁定；如需更换请重新发布练习')
            if 'questions' in form and list(dict.fromkeys(int(x) for x in form.getlist('questions')))!=json.loads(existing['question_ids']):
                raise ValueError('已有学生开始作答，不能更换题目')
            ids=json.loads(existing['question_ids']);threshold=existing['threshold'];duration=existing['duration']
        else:
            threshold=int(form.get('threshold',existing['threshold'] if existing else 100))
            duration=int(form.get('duration',existing['duration'] if existing else 40))
            ids=list(dict.fromkeys(int(x) for x in form.getlist('questions')))
            selection=form.get('selection','keep' if existing else ('manual' if ids else 'random'))
            if selection=='keep' and existing:
                ids=json.loads(existing['question_ids'])
            elif selection=='manual':
                if not ids or len(ids)>100 or any(not one('SELECT id FROM questions WHERE id=? AND deleted_at IS NULL',(qid,)) for qid in ids):
                    raise ValueError('请选择1至100道有效题目；已删除题目不能重新加入')
            elif selection=='random':
                count=int(form.get('count',20));cat=form.get('category','')
                if not 1<=count<=100: raise ValueError('随机题数应为1至100')
                ids=[r['id'] for r in rows('SELECT id FROM questions WHERE deleted_at IS NULL AND (?="" OR category=?) ORDER BY RANDOM() LIMIT ?',(cat,cat,count))]
                if len(ids)<count: raise ValueError('该分类题量不足')
            else:
                raise ValueError('请选择有效的组题方式')
        if not 1<=threshold<=100 or not 1<=duration<=240: raise ValueError('请检查达标率或考试时长')
        return dict(title=title,kind=kind,starts=starts.isoformat(timespec='minutes'),ends=ends.isoformat(timespec='minutes'),duration=duration,threshold=threshold,question_ids=json.dumps(ids))

    def require_teacher():
        if g.user['role'] not in ('admin','teacher'):
            abort(403)

    def classroom(cid):
        c = one('SELECT * FROM classes WHERE id=?',(cid,))
        if not c:
            abort(404)
        if g.user['role'] != 'admin' and not (g.user['role']=='teacher' and c['teacher_id']==g.user['id']) and g.user['class_id'] != cid:
            abort(403)
        return c

    def task_access(tid):
        t = one('SELECT * FROM tasks WHERE id=?',(tid,))
        if not t:
            abort(404)
        classroom(t['class_id'])
        return t

    def audit(action):
        db().execute('INSERT INTO audit(user_id,action,created) VALUES(?,?,?)',(g.user['id'],action,now()))

    @app.teardown_appcontext
    def close(error):
        if 'db' in g:
            g.db.close()

    @app.before_request
    def guard():
        g.user = one('SELECT * FROM users WHERE id=?',(session.get('uid'),)) if session.get('uid') else None
        if g.user and (g.user['deleted_at'] or session.get('version',0)!=g.user['session_version']):
            session.clear();g.user=None
        session.setdefault('csrf', secrets.token_hex(24))
        if request.method == 'POST' and not secrets.compare_digest(str(request.form.get('csrf') or request.headers.get('X-CSRF-Token','')), session['csrf']):
            abort(400, '页面已过期，请刷新后重试')
        if request.endpoint not in ('login','static','health'):
            if not g.user:
                return redirect(url_for('login'))
            if g.user['must_change'] and request.endpoint not in ('password','logout'):
                return redirect(url_for('password'))

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Referrer-Policy']='same-origin'
        response.headers['Cache-Control']='no-store'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.context_processor
    def context():
        return dict(current=g.user, csrf=session['csrf'], today=now()[:10], roles={'admin':'管理员','teacher':'班主任','student':'学生'})

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(413)
    def error(e):
        return render_template('error.html', error=e), e.code

    @app.route('/health')
    def health():
        one('SELECT 1')
        return {'status':'ok'}

    @app.route('/login',methods=['GET','POST'])
    def login():
        if request.method=='POST':
            username=request.form.get('username','').strip()
            key=username[:100]
            lock=one('SELECT * FROM login_limits WHERE username=?',(key,))
            if lock and lock['until']>time.time():
                flash('尝试次数过多，请15分钟后重试')
                return render_template('login.html'),429
            u=one('SELECT * FROM users WHERE username=? AND deleted_at IS NULL',(username,))
            if u and check_password_hash(u['password'],request.form.get('password','')):
                db().execute('DELETE FROM login_limits WHERE username=?',(key,)); db().commit()
                session.clear(); session['uid']=u['id']; session['version']=u['session_version']; session.permanent=True
                return redirect(url_for('password' if u['must_change'] else 'home'))
            count=(lock['failures'] if lock else 0)+1
            db().execute('INSERT OR REPLACE INTO login_limits VALUES(?,?,?)',(key,count,time.time()+900 if count>=8 else 0));db().commit()
            flash('账号或密码不正确')
        return render_template('login.html')

    @app.post('/logout')
    def logout():
        session.clear()
        return redirect('/login')

    @app.route('/password',methods=['GET','POST'])
    def password():
        if request.method=='POST':
            p=request.form.get('password','')
            if not check_password_hash(g.user['password'],request.form.get('old','')):
                flash('原密码不正确')
            elif len(p)<10 or p==request.form.get('old'):
                flash('新密码至少10位，且不能与原密码相同')
            else:
                db().execute('UPDATE users SET password=?,must_change=0,session_version=session_version+1 WHERE id=?',(generate_password_hash(p),g.user['id']));db().commit()
                session['version']=g.user['session_version']+1
                flash('密码已更新');return redirect('/')
        return render_template('password.html')

    @app.route('/')
    def home():
        sync_user_mistakes()
        if g.user['role']=='admin':
            classes=rows('SELECT c.*,u.name teacher FROM classes c LEFT JOIN users u ON u.id=c.teacher_id')
        elif g.user['role']=='teacher':
            classes=rows('SELECT c.*,u.name teacher FROM classes c LEFT JOIN users u ON u.id=c.teacher_id WHERE teacher_id=?',(g.user['id'],))
        else:
            classes=rows('SELECT c.*,u.name teacher FROM classes c LEFT JOIN users u ON u.id=c.teacher_id WHERE c.id=?',(g.user['class_id'],))
        tasks=[dict(r) for r in rows('SELECT t.*,c.name class_name,COUNT(a.submitted) attempts,COALESCE(MAX(CASE WHEN a.id IS NOT NULL AND a.submitted IS NULL THEN 1 ELSE 0 END),0) active,COALESCE(MAX(CASE WHEN a.submitted IS NOT NULL THEN a.passed ELSE 0 END),0) completed FROM tasks t JOIN classes c ON c.id=t.class_id LEFT JOIN attempts a ON a.task_id=t.id AND a.user_id=? WHERE t.deleted_at IS NULL AND c.id IN (%s) GROUP BY t.id ORDER BY t.id DESC LIMIT 50' % (','.join('?' for _ in classes) or 'NULL'),(g.user['id'],*[c['id'] for c in classes]))]
        for task in tasks: task['status']=task_status(task)
        stats=one('SELECT COUNT(*) attempts,COALESCE(SUM(score),0) correct,COALESCE(SUM(total),0) total FROM attempts WHERE user_id=? AND submitted IS NOT NULL',(g.user['id'],))
        wrong=one('SELECT COUNT(*) n FROM mistakes m JOIN questions q ON q.id=m.question_id WHERE m.user_id=? AND m.mastered=0 AND q.deleted_at IS NULL',(g.user['id'],))['n']
        return render_template('home.html',classes=classes,tasks=tasks,stats=stats,wrong=wrong,bank_count=one('SELECT COUNT(*) n FROM questions WHERE deleted_at IS NULL')['n'])

    @app.route('/bank')
    def bank():
        category=request.args.get('category','')
        search=request.args.get('q','').strip()[:100]
        deleted=request.args.get('deleted')=='1'
        if deleted and g.user['role']!='admin': abort(403)
        questions=[dict(q) for q in rows('SELECT * FROM questions WHERE (deleted_at IS NOT NULL)=? AND (?="" OR category=?) AND (?="" OR stem LIKE ?) ORDER BY id LIMIT 100',(int(deleted),category,category,search,'%'+search+'%'))] if g.user['role']!='student' else []
        for q in questions: q['options']=json.loads(q['options'])
        return render_template('bank.html',categories=rows('SELECT category,COUNT(*) n FROM questions WHERE deleted_at IS NULL GROUP BY category ORDER BY MIN(id)'), questions=questions, category=category, search=search, deleted=deleted)

    @app.route('/bank/new',methods=['GET','POST'])
    def question_new():
        if g.user['role']!='admin': abort(403)
        form=request.form if request.method=='POST' else {}
        if request.method=='POST':
            question=dict(category=form.get('category','').strip(),stem=form.get('stem','').strip(),options=[form.get(letter,'').strip() for letter in 'ABCD'],answer=form.get('answer','').strip().upper(),explanation=form.get('explanation','').strip())
            if not all([question['category'],question['stem'],question['explanation'],*question['options']]) or question['answer'] not in ('A','B','C','D'):
                flash('请填写分类、题干、四个选项、正确答案和解析')
            elif len(question['category'])>80 or len(question['stem'])>5000 or any(len(x)>2000 for x in question['options']) or len(question['explanation'])>12000:
                flash('内容过长，请缩短后重试')
            else:
                with db():
                    added=insert_question(db(),question)
                    if added: audit('手动新增题目 '+str(added))
                if added:
                    flash('题目已新增，可以用于训练和发布任务')
                    return redirect(url_for('bank',q=question['stem'][:100]))
                flash('完全相同的题目已存在；若之前删除过，请到回收站恢复')
        return render_template('question_new.html',form=form,categories=rows('SELECT DISTINCT category FROM questions ORDER BY category'))

    @app.post('/bank/<int:qid>/delete')
    def question_delete(qid):
        if g.user['role']!='admin': abort(403)
        if not one('SELECT id FROM questions WHERE id=?',(qid,)): abort(404)
        with db():
            changed=db().execute('UPDATE questions SET deleted_at=? WHERE id=? AND deleted_at IS NULL',(now(),qid)).rowcount
            if changed: audit('删除题目 '+str(qid))
        flash('题目已移入回收站，已有任务和历史记录保留原题')
        return redirect('/bank')

    @app.post('/bank/<int:qid>/restore')
    def question_restore(qid):
        if g.user['role']!='admin': abort(403)
        if not one('SELECT id FROM questions WHERE id=?',(qid,)): abort(404)
        with db():
            changed=db().execute('UPDATE questions SET deleted_at=NULL WHERE id=? AND deleted_at IS NOT NULL',(qid,)).rowcount
            if changed: audit('恢复题目 '+str(qid))
        flash('题目已恢复，可以再次用于训练和发布任务')
        return redirect(url_for('bank',deleted=1))

    @app.post('/bank/import')
    def bank_import():
        if g.user['role']!='admin': abort(403)
        file=request.files.get('file')
        category=request.form.get('category','').strip()[:80]
        if not file or not file.filename.lower().endswith('.docx') or not category: abort(400,'需要分类名称和 DOCX 文件')
        try:
            qs, errors=parse_docx(file,category)
            if errors: raise ValueError('；'.join(errors[:10]))
            with db():
                for q in qs: insert_question(db(),q)
                audit('导入题库 '+category)
            flash(f'导入完成：识别 {len(qs)} 题，重复题自动跳过')
        except Exception as e:
            flash('导入失败：'+str(e)[:500])
        return redirect('/bank')

    @app.post('/practice')
    def practice():
        sync_user_mistakes()
        mode=request.form.get('mode','random')
        cat=request.form.get('category','')
        try: count=int(request.form.get('count',20))
        except ValueError: abort(400)
        if not 1<=count<=100: abort(400)
        if mode=='wrong':
            pool=rows('SELECT q.id FROM questions q JOIN mistakes m ON m.question_id=q.id WHERE m.user_id=? AND m.mastered=0 AND q.deleted_at IS NULL ORDER BY RANDOM() LIMIT ?',(g.user['id'],count))
        else:
            pool=rows('SELECT id FROM questions WHERE deleted_at IS NULL AND (?="" OR category=?) ORDER BY RANDOM() LIMIT ?',(cat,cat,count))
        if not pool:
            flash('暂无可练习题目');return redirect('/bank')
        aid=new_attempt([q['id'] for q in pool])
        return redirect(url_for('attempt',aid=aid))

    def new_attempt(ids, task=None):
        try:
            with db():
                if task:
                    db().execute('BEGIN IMMEDIATE')
                    task=one('SELECT * FROM tasks WHERE id=?',(task['id'],))
                    student=one('SELECT * FROM users WHERE id=?',(g.user['id'],))
                    if not task or task['deleted_at']: abort(400,'该任务已删除，不能开始新的作答')
                    if student['deleted_at'] or student['class_id']!=task['class_id']: abort(403)
                    if now()<task['starts'] or now()>=task['ends']: abort(400,'当前不在开放时间内')
                    ids=json.loads(task['question_ids'])
                cur=db().execute('INSERT INTO attempts(user_id,task_id,exam_task_id,question_ids,total,started,deadline) VALUES(?,?,?,?,?,?,?)',(g.user['id'],task['id'] if task else None,task['id'] if task and task['kind']=='exam' else None,json.dumps(ids),len(ids),time.time(),min(time.time()+task['duration']*60, datetime.fromisoformat(task['ends']).replace(tzinfo=timezone(timedelta(hours=8))).timestamp()) if task and task['kind']=='exam' else None))
            return cur.lastrowid
        except sqlite3.IntegrityError:
            db().rollback()
            old=one('SELECT id FROM attempts WHERE task_id=? AND user_id=? ORDER BY id DESC LIMIT 1',(task['id'],g.user['id'])) if task else None
            if old: return old['id']
            raise

    @app.post('/tasks/<int:tid>/start')
    def start_task(tid):
        t=task_access(tid)
        if g.user['role']!='student': abort(403)
        if t['deleted_at']: abort(400,'该任务已删除，不能开始新的作答')
        if now()<t['starts'] or now()>=t['ends']: abort(400,'当前不在开放时间内')
        old=one('SELECT * FROM attempts WHERE task_id=? AND user_id=? ORDER BY id DESC LIMIT 1',(tid,g.user['id']))
        if old and (old['submitted'] is None or t['kind']=='exam'):
            return redirect(url_for('attempt',aid=old['id']))
        return redirect(url_for('attempt',aid=new_attempt(json.loads(t['question_ids']),t)))

    def get_attempt(aid):
        a=one('SELECT * FROM attempts WHERE id=?',(aid,))
        if not a: abort(404)
        if a['user_id']!=g.user['id']:
            if a['task_id']:
                require_teacher();task_access(a['task_id'])
            elif g.user['role']!='admin': abort(403)
        return a

    def grade(a, answers):
        ids=json.loads(a['question_ids'])
        questions=[one('SELECT * FROM questions WHERE id=?',(qid,)) for qid in ids]
        score=sum(answers.get(str(q['id']))==q['answer'] for q in questions)
        task=one('SELECT * FROM tasks WHERE id=?',(a['task_id'],)) if a['task_id'] else None
        threshold=task['threshold'] if task else 100
        with db():
            changed=db().execute('UPDATE attempts SET answers=?,score=?,passed=?,submitted=? WHERE id=? AND submitted IS NULL',(json.dumps(answers),score,int(score*100>=len(ids)*threshold),min(time.time(),a['deadline']) if a['deadline'] else time.time(),a['id'])).rowcount
            if changed and (not task or task['kind']!='exam' or now()>=task['ends']):
                record_mistakes(a,questions,answers)

    def record_mistakes(a,questions,answers):
        changed=db().execute('UPDATE attempts SET mistakes_synced=1 WHERE id=? AND mistakes_synced=0',(a['id'],)).rowcount
        if not changed: return
        for q in questions:
            correct=answers.get(str(q['id']))==q['answer']
            if not correct:
                db().execute('INSERT INTO mistakes(user_id,question_id,wrong_count,mastered) VALUES(?,?,1,0) ON CONFLICT(user_id,question_id) DO UPDATE SET wrong_count=wrong_count+1,mastered=0',(a['user_id'],q['id']))
            else:
                db().execute('UPDATE mistakes SET mastered=1 WHERE user_id=? AND question_id=?',(a['user_id'],q['id']))

    def sync_user_mistakes():
        pending=rows('SELECT a.* FROM attempts a JOIN tasks t ON t.id=a.task_id WHERE a.user_id=? AND a.submitted IS NOT NULL AND a.mistakes_synced=0 AND t.kind="exam" AND t.ends<=? ORDER BY a.submitted',(g.user['id'],now()))
        if pending:
            with db():
                for a in pending:
                    qs=[one('SELECT * FROM questions WHERE id=?',(qid,)) for qid in json.loads(a['question_ids'])]
                    record_mistakes(a,qs,json.loads(a['answers']))

    @app.route('/attempts/<int:aid>',methods=['GET','POST'])
    def attempt(aid):
        a=get_attempt(aid)
        if a['submitted'] is None and a['deadline'] and time.time()>=a['deadline']:
            grade(a,json.loads(a['answers']));a=get_attempt(aid)
        if request.method=='POST' and a['submitted'] is None:
            if a['user_id']!=g.user['id']: abort(403)
            ids=json.loads(a['question_ids'])
            answers={str(q):request.form.get(str(q),'') for q in ids if request.form.get(str(q),'') in ('A','B','C','D')}
            if request.form.get('save'):
                db().execute('UPDATE attempts SET answers=? WHERE id=? AND submitted IS NULL',(json.dumps(answers),aid));db().commit()
                return {'saved':True}
            grade(a,answers)
            return redirect(url_for('attempt',aid=aid))
        task=one('SELECT * FROM tasks WHERE id=?',(a['task_id'],)) if a['task_id'] else None
        review=a['submitted'] is not None and (not task or task['kind']!='exam' or now()>=task['ends'] or g.user['role']!='student')
        qs=[]
        for qid in json.loads(a['question_ids']):
            q=dict(one('SELECT * FROM questions WHERE id=?',(qid,)))
            q['options']=json.loads(q['options'])
            if not review: q.pop('answer');q.pop('explanation')
            qs.append(q)
        return render_template('attempt.html',attempt=a,questions=qs,answers=json.loads(a['answers']),review=review,task=task)

    @app.route('/mistakes')
    def mistakes():
        sync_user_mistakes()
        qs=rows('SELECT q.*,m.wrong_count,m.mastered FROM mistakes m JOIN questions q ON q.id=m.question_id WHERE user_id=? ORDER BY mastered,wrong_count DESC',(g.user['id'],))
        return render_template('mistakes.html',questions=qs)

    @app.post('/classes')
    def create_class():
        if g.user['role']!='admin': abort(403)
        name=request.form.get('name','').strip()[:100]
        username=request.form.get('username','').strip()[:100]
        if not name or not username: abort(400)
        password=request.form.get('initial_password','').strip()
        if password and not 10<=len(password)<=128:
            flash('班主任初始密码需为10至128位；也可以留空自动生成')
            return redirect('/')
        password=password or secrets.token_urlsafe(10)
        try:
            with db():
                uid=db().execute('INSERT INTO users(username,name,password,role,must_change) VALUES(?,?,?,?,1)',(username,request.form.get('teacher','班主任').strip()[:80] or '班主任',generate_password_hash(password),'teacher')).lastrowid
                cid=db().execute('INSERT INTO classes(name,teacher_id) VALUES(?,?)',(name,uid)).lastrowid
                db().execute('DELETE FROM login_limits WHERE username=?',(username,))
                audit('新建班级 '+name)
        except sqlite3.IntegrityError:
            flash('班级名称或班主任账号重复');return redirect('/')
        return render_template('credentials.html',credentials=[dict(username=username,name=name+'班主任',password=password)],back=url_for('class_detail',cid=cid))

    @app.route('/classes/<int:cid>')
    def class_detail(cid):
        c=classroom(cid);require_teacher()
        deleted_students=request.args.get('students')=='deleted';deleted_tasks=request.args.get('tasks')=='deleted'
        students=rows('SELECT id,username,name,must_change,deleted_at FROM users WHERE class_id=? AND role="student" AND (deleted_at IS NOT NULL)=? ORDER BY username',(cid,int(deleted_students)))
        return render_template('class.html',classroom=c,students=students,deleted_students=deleted_students,deleted_tasks=deleted_tasks,tasks=rows('SELECT * FROM tasks WHERE class_id=? AND (deleted_at IS NOT NULL)=? ORDER BY id DESC',(cid,int(deleted_tasks))),categories=rows('SELECT category,COUNT(*) n FROM questions WHERE deleted_at IS NULL GROUP BY category'),questions=rows('SELECT id,category,stem FROM questions WHERE deleted_at IS NULL ORDER BY id'))

    @app.route('/students/<int:uid>/edit',methods=['GET','POST'])
    def student_edit(uid):
        require_teacher();student=managed_student(uid)
        if request.method=='POST':
            try:
                name=request.form.get('name','').strip();number=request.form.get('number','').strip();cid=int(request.form.get('class_id',student['class_id']))
                if not 1<=len(name)<=80 or not 1<=len(number)<=100: raise ValueError('姓名和学号不能为空，姓名最多80字，学号最多100字')
                with db():
                    db().execute('BEGIN IMMEDIATE');student=managed_student(uid);classroom(cid)
                    changed_login=number!=student['username'] or cid!=student['class_id']
                    db().execute('UPDATE users SET name=?,username=?,class_id=?,session_version=session_version+? WHERE id=?',(name,number,cid,int(changed_login),uid))
                    if changed_login: db().execute('DELETE FROM login_limits WHERE username IN (?,?)',(student['username'][:100],number[:100]))
                    audit('修改学生 '+str(uid))
                flash('学生信息已保存'+('，学号或班级已变更，请让该学生重新登录' if changed_login else ''))
                return redirect(url_for('class_detail',cid=cid))
            except sqlite3.IntegrityError:
                flash('该学号已被其他账号使用，包括回收站中的账号')
            except ValueError as e: flash(str(e))
        return render_template('student_edit.html',student=student,form=request.form if request.method=='POST' else {},classes=available_classes())

    @app.post('/students/<int:uid>/delete')
    def student_delete(uid):
        require_teacher()
        with db():
            db().execute('BEGIN IMMEDIATE');student=managed_student(uid,deleted=True)
            changed=db().execute('UPDATE users SET deleted_at=?,session_version=session_version+1 WHERE id=? AND deleted_at IS NULL',(now(),uid)).rowcount
            if changed: audit('删除学生 '+str(uid))
        flash('学生已移入回收站并停用登录，历史成绩保留')
        return redirect(url_for('class_detail',cid=student['class_id']))

    @app.post('/students/<int:uid>/restore')
    def student_restore(uid):
        require_teacher()
        with db():
            db().execute('BEGIN IMMEDIATE');student=managed_student(uid,deleted=True)
            changed=db().execute('UPDATE users SET deleted_at=NULL WHERE id=? AND deleted_at IS NOT NULL',(uid,)).rowcount
            if changed:
                db().execute('DELETE FROM login_limits WHERE username=?',(student['username'][:100],))
                audit('恢复学生 '+str(uid))
        flash('学生已恢复，仍使用删除前的密码；忘记时可重置为123456')
        return redirect(url_for('class_detail',cid=student['class_id']))

    @app.post('/classes/<int:cid>/students')
    def students_import(cid):
        c=classroom(cid);require_teacher()
        file=request.files.get('file')
        credentials=[]
        try:
            if file:
                if not file.filename.lower().endswith('.xlsx'): raise ValueError('请上传 .xlsx 文件')
                records=parse_students(file, c['name'])
            else:
                records=[dict(number=request.form.get('number','').strip(),name=request.form.get('name','').strip(),**{'class':c['name']})]
            if any(r['class']!=c['name'] for r in records): raise ValueError('名单中班级与当前班级不一致，请按班级分别导入')
            if any(not r['number'] or not r['name'] for r in records): raise ValueError('学号和姓名不能为空')
            with db():
                for r in records:
                    old=one('SELECT * FROM users WHERE username=?',(r['number'],))
                    if old:
                        if old['class_id']!=cid or old['role']!='student': raise ValueError('学号已被其他账号使用：'+r['number'])
                        if old['deleted_at']: raise ValueError('该学号在学生回收站中，请先恢复：'+r['number'])
                        continue
                    password=STUDENT_INITIAL_PASSWORD
                    db().execute('INSERT INTO users(username,name,password,role,class_id,must_change) VALUES(?,?,?,?,?,1)',(r['number'],r['name'],generate_password_hash(password),'student',cid))
                    db().execute('DELETE FROM login_limits WHERE username=?',(r['number'][:100],))
                    credentials.append(dict(username=r['number'],name=r['name'],password=password))
                audit(f'导入学生 {len(credentials)} 人')
        except Exception as e:
            flash('导入失败，未保存任何学生：'+str(e)[:300]);return redirect(url_for('class_detail',cid=cid))
        return render_template('credentials.html',credentials=credentials,back=url_for('class_detail',cid=cid))

    @app.post('/users/<int:uid>/reset')
    def reset(uid):
        require_teacher()
        u=one('SELECT * FROM users WHERE id=? AND deleted_at IS NULL',(uid,))
        if not u: abort(404)
        if g.user['role']!='admin':
            if u['role']!='student' or not u['class_id']: abort(403)
            classroom(u['class_id'])
        chosen=request.form.get('initial_password','').strip() if u['role']=='teacher' else ''
        if chosen and not 10<=len(chosen)<=128:
            flash('班主任初始密码需为10至128位；也可以留空自动生成')
            return redirect('/admin')
        password=STUDENT_INITIAL_PASSWORD if u['role']=='student' else (chosen or secrets.token_urlsafe(10))
        with db():
            db().execute('UPDATE users SET password=?,must_change=1,session_version=session_version+1 WHERE id=?',(generate_password_hash(password),uid))
            db().execute('DELETE FROM login_limits WHERE username=?',(u['username'][:100],))
            audit('重置密码 '+u['username'])
        return render_template('credentials.html',credentials=[dict(username=u['username'],name=u['name'],password=password)],back='/')

    @app.post('/classes/<int:cid>/tasks')
    def publish(cid):
        classroom(cid);require_teacher()
        try:
            settings=task_configuration(request.form)
            db().execute('INSERT INTO tasks(class_id,title,kind,question_ids,starts,ends,duration,threshold) VALUES(?,?,?,?,?,?,?,?)',(cid,settings['title'],settings['kind'],settings['question_ids'],settings['starts'],settings['ends'],settings['duration'],settings['threshold']));audit('发布任务 '+settings['title']);db().commit()
            flash('发布成功，全班使用同一组题目')
        except (ValueError,KeyError) as e: flash(str(e))
        return redirect(url_for('class_detail',cid=cid))

    @app.route('/tasks/<int:tid>/edit',methods=['GET','POST'])
    def task_edit(tid):
        require_teacher();task=task_access(tid)
        if task['kind']!='daily' or task['deleted_at']: abort(400,'仅可修改尚未删除的每日练习')
        locked=one('SELECT id FROM attempts WHERE task_id=? LIMIT 1',(tid,)) is not None
        if request.method=='POST':
            try:
                with db():
                    db().execute('BEGIN IMMEDIATE');task=task_access(tid)
                    if task['deleted_at']: abort(400,'任务已删除，请先恢复')
                    locked=one('SELECT id FROM attempts WHERE task_id=? LIMIT 1',(tid,)) is not None
                    settings=task_configuration(request.form,task,locked)
                    db().execute('UPDATE tasks SET title=?,question_ids=?,starts=?,ends=?,threshold=? WHERE id=?',(settings['title'],settings['question_ids'],settings['starts'],settings['ends'],settings['threshold'],tid))
                    audit('修改每日练习 '+str(tid))
                flash('每日练习已保存');return redirect(url_for('class_detail',cid=task['class_id']))
            except ValueError as e: flash(str(e))
        selected=json.loads(task['question_ids'])
        if request.method=='POST' and not locked and request.form.get('selection')=='manual':
            selected=[int(value) for value in request.form.getlist('questions') if value.isdecimal()]
        return render_template('task_edit.html',task=task,locked=locked,form=request.form if request.method=='POST' else {},selected=selected,current_count=len(json.loads(task['question_ids'])),questions=rows('SELECT id,category,stem FROM questions WHERE deleted_at IS NULL ORDER BY id'),categories=rows('SELECT DISTINCT category FROM questions WHERE deleted_at IS NULL ORDER BY category'))

    @app.post('/tasks/<int:tid>/delete')
    def task_delete(tid):
        require_teacher()
        with db():
            db().execute('BEGIN IMMEDIATE');task=task_access(tid)
            if task['kind']!='daily': abort(400,'该入口仅用于每日练习')
            changed=db().execute('UPDATE tasks SET deleted_at=? WHERE id=? AND deleted_at IS NULL',(now(),tid)).rowcount
            if changed: audit('删除每日练习 '+str(tid))
        flash('每日练习已移入回收站，不再接受新作答；已有答卷保留')
        return redirect(url_for('class_detail',cid=task['class_id']))

    @app.post('/tasks/<int:tid>/restore')
    def task_restore(tid):
        require_teacher()
        with db():
            db().execute('BEGIN IMMEDIATE');task=task_access(tid)
            if task['kind']!='daily': abort(400,'该入口仅用于每日练习')
            changed=db().execute('UPDATE tasks SET deleted_at=NULL WHERE id=? AND deleted_at IS NOT NULL',(tid,)).rowcount
            if changed: audit('恢复每日练习 '+str(tid))
        flash('每日练习已恢复，原有答卷和完成状态保留')
        return redirect(url_for('class_detail',cid=task['class_id']))

    def progress_data(t):
        # Finalize expired exams from saved answers, even when students close the browser.
        for a in rows('SELECT * FROM attempts WHERE task_id=? AND submitted IS NULL AND deadline<=?',(t['id'],time.time())):
            grade(a,json.loads(a['answers']))
        data=[dict(r) for r in rows('SELECT u.id,u.username,u.name,COUNT(a.submitted) attempts,MAX(CASE WHEN a.submitted IS NOT NULL THEN a.score END) best,MAX(CASE WHEN a.submitted IS NOT NULL THEN a.passed ELSE 0 END) completed,MAX(CASE WHEN a.id IS NOT NULL AND a.submitted IS NULL THEN 1 ELSE 0 END) active FROM users u LEFT JOIN attempts a ON a.user_id=u.id AND a.task_id=? WHERE u.class_id=? AND u.role="student" AND u.deleted_at IS NULL GROUP BY u.id ORDER BY u.username',(t['id'],t['class_id']))]
        for student in data: student['status']=task_status(student)
        return data

    @app.route('/tasks/<int:tid>/progress')
    def progress(tid):
        require_teacher();t=task_access(tid)
        data=progress_data(t)
        if request.args.get('format')=='json': return jsonify(data)
        if request.args.get('format')=='csv':
            out=io.StringIO();w=csv.writer(out);w.writerow(['学号','姓名','提交次数','最高正确数','已达标'])
            for r in data: w.writerow(["'"+r['username'],"'"+r['name'],r['attempts'],r['best'],r['completed']])
            return Response('\ufeff'+out.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=progress.csv'})
        return render_template('progress.html',task=t,students=data)

    @app.route('/tasks/<int:tid>/ranking')
    def ranking(tid):
        t=task_access(tid)
        if t['kind']!='exam': abort(400)
        if g.user['role']=='student' and now()<t['ends']: abort(403,'考试结束后开放排名')
        progress_data(t)
        rank=rows('SELECT u.name,u.username,a.score,a.total,CAST(a.submitted-a.started AS INTEGER) elapsed FROM attempts a JOIN users u ON u.id=a.user_id WHERE task_id=? AND submitted IS NOT NULL ORDER BY score DESC,elapsed ASC,u.username',(tid,))
        return render_template('ranking.html',task=t,ranking=rank)

    @app.route('/admin')
    def admin():
        if g.user['role']!='admin': abort(403)
        return render_template('admin.html',users=rows('SELECT id,username,name,role FROM users WHERE deleted_at IS NULL ORDER BY id'),logs=rows('SELECT a.*,u.name FROM audit a LEFT JOIN users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT 100'))

    with app.app_context():
        db().executescript(SCHEMA)
        for table in ('users','tasks'):
            if 'deleted_at' not in {r['name'] for r in rows('PRAGMA table_info('+table+')')}:
                db().execute('ALTER TABLE '+table+' ADD COLUMN deleted_at TEXT')
        if 'deleted_at' not in {r['name'] for r in rows('PRAGMA table_info(questions)')}:
            db().execute('ALTER TABLE questions ADD COLUMN deleted_at TEXT')
        db().execute('CREATE INDEX IF NOT EXISTS active_question_categories ON questions(deleted_at,category)')
        if 'session_version' not in {r['name'] for r in rows('PRAGMA table_info(users)')}:
            db().execute('ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0')
        if 'exam_task_id' not in {r['name'] for r in rows('PRAGMA table_info(attempts)')}:
            db().execute('ALTER TABLE attempts ADD COLUMN exam_task_id INTEGER REFERENCES tasks(id)')
        if 'mistakes_synced' not in {r['name'] for r in rows('PRAGMA table_info(attempts)')}:
            db().execute('ALTER TABLE attempts ADD COLUMN mistakes_synced INTEGER NOT NULL DEFAULT 0')
        db().execute('CREATE UNIQUE INDEX IF NOT EXISTS one_exam_attempt ON attempts(user_id,exam_task_id)')
        db().execute('PRAGMA journal_mode=WAL')
        if not one('SELECT id FROM users WHERE role="admin"'):
            pw=os.environ.get('ADMIN_PASSWORD') or secrets.token_urlsafe(18)
            db().execute('INSERT INTO users(username,name,password,role,must_change) VALUES(?,?,?,?,1)',('admin','管理员',generate_password_hash(pw),'admin'))
            if not os.environ.get('ADMIN_PASSWORD'):
                (data/'initial-admin-password.txt').write_text(pw,encoding='utf-8')
        if not one('SELECT id FROM questions LIMIT 1'):
            for q in json.loads((ROOT/'seed/questions.json').read_text(encoding='utf-8')): insert_question(db(),q)
        db().commit()
    return app


def insert_question(db,q):
    cur=db.execute('INSERT OR IGNORE INTO questions(category,stem,options,answer,explanation) VALUES(?,?,?,?,?)',(q['category'],q['stem'],json.dumps(q['options'],ensure_ascii=False),q['answer'],q['explanation']))
    return cur.lastrowid if cur.rowcount else None


SCHEMA='''
CREATE TABLE IF NOT EXISTS classes(id INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE,teacher_id INTEGER REFERENCES users(id));
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT NOT NULL UNIQUE,name TEXT NOT NULL,password TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('admin','teacher','student')),class_id INTEGER REFERENCES classes(id),must_change INTEGER NOT NULL DEFAULT 1,session_version INTEGER NOT NULL DEFAULT 0,deleted_at TEXT);
CREATE TABLE IF NOT EXISTS questions(id INTEGER PRIMARY KEY,category TEXT NOT NULL,stem TEXT NOT NULL,options TEXT NOT NULL,answer TEXT NOT NULL,explanation TEXT NOT NULL,deleted_at TEXT,UNIQUE(category,stem,options,answer,explanation));
CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY,class_id INTEGER NOT NULL REFERENCES classes(id),title TEXT NOT NULL,kind TEXT NOT NULL,question_ids TEXT NOT NULL,starts TEXT NOT NULL,ends TEXT NOT NULL,duration INTEGER NOT NULL,threshold INTEGER NOT NULL DEFAULT 100,deleted_at TEXT);
CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),task_id INTEGER REFERENCES tasks(id),exam_task_id INTEGER REFERENCES tasks(id),question_ids TEXT NOT NULL,answers TEXT NOT NULL DEFAULT '{}',total INTEGER NOT NULL,score INTEGER,passed INTEGER DEFAULT 0,started REAL NOT NULL,deadline REAL,submitted REAL,mistakes_synced INTEGER NOT NULL DEFAULT 0,UNIQUE(user_id,exam_task_id));
CREATE UNIQUE INDEX IF NOT EXISTS one_active_task ON attempts(user_id,task_id) WHERE submitted IS NULL AND task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS attempt_progress ON attempts(task_id,user_id,submitted);
CREATE TABLE IF NOT EXISTS mistakes(user_id INTEGER NOT NULL REFERENCES users(id),question_id INTEGER NOT NULL REFERENCES questions(id),wrong_count INTEGER NOT NULL,mastered INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(user_id,question_id));
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,user_id INTEGER,action TEXT,created TEXT);
CREATE TABLE IF NOT EXISTS login_limits(username TEXT PRIMARY KEY,failures INTEGER NOT NULL,until REAL NOT NULL);
'''

if __name__=='__main__':
    from waitress import serve
    serve(create_app(),host='127.0.0.1',port=int(os.environ.get('PORT',8080)),threads=8)
