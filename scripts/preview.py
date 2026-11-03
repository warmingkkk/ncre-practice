"""Disposable local demo. No real students. Bind to localhost only."""
import os, sys, sqlite3, json, tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import create_app
from werkzeug.security import generate_password_hash
from waitress import serve

with tempfile.TemporaryDirectory() as folder:
    os.environ['DATA_DIR']=folder
    app=create_app()
    with sqlite3.connect(app.config['DATABASE']) as db:
        pw=generate_password_hash('DemoPractice2026')
        db.execute('UPDATE users SET password=?,must_change=0',(pw,))
        db.execute('INSERT INTO users(id,username,name,password,role,must_change) VALUES(2,"teacher","演示班主任",?,"teacher",0)',(pw,))
        db.execute('INSERT INTO classes(id,name,teacher_id) VALUES(1,"信息技术 · 演示班",2)')
        db.execute('INSERT INTO users(id,username,name,password,role,class_id,must_change) VALUES(3,"student","演示同学",?,"student",1,0)',(pw,))
        now=datetime.now(timezone(timedelta(hours=8)))
        for title,kind in [('因特网专项 · 每日一练','daily'),('计算机基础 · 模拟自测','exam')]:
            db.execute('INSERT INTO tasks(class_id,title,kind,question_ids,starts,ends,duration,threshold) VALUES(1,?,?,?,?,?,20,100)',(title,kind,json.dumps(list(range(1,11))),(now-timedelta(days=1)).strftime('%Y-%m-%dT%H:%M'),(now+timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')))
    print('Local demo http://127.0.0.1:8081 | admin / teacher / student | DemoPractice2026',flush=True)
    serve(app,host='127.0.0.1',port=8081,threads=8)
