"""Isolated real HTTP load test. Never touches production users or database.
python scripts/load_test.py --users 300
"""
import argparse
import concurrent.futures
import http.client
import json
import os
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
import logging
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import create_app
from waitress.server import create_server
from werkzeug.security import generate_password_hash

parser=argparse.ArgumentParser()
parser.add_argument('--users',type=int,default=300)
parser.add_argument('--login',action='store_true',help='Include real password verification for every student')
args=parser.parse_args()
logging.getLogger('waitress.queue').setLevel(logging.ERROR)
with tempfile.TemporaryDirectory() as folder:
    os.environ['DATA_DIR']=folder
    app=create_app({'DATABASE':str(Path(folder)/'load.db')})
    db=sqlite3.connect(app.config['DATABASE'])
    password=generate_password_hash('LoadTestPassword')
    db.execute('INSERT INTO classes(id,name,teacher_id) VALUES(1,"Load test",1)')
    for i in range(2,args.users+2):
        db.execute('INSERT INTO users(id,username,name,password,role,class_id,must_change) VALUES(?,?,?,?,"student",1,0)',(i,str(i),'Synthetic Student',password))
    ids=list(range(1,21))
    db.execute('INSERT INTO tasks(id,class_id,title,kind,question_ids,starts,ends,duration,threshold) VALUES(1,1,"Load test","daily",?,"2000-01-01T00:00","2099-01-01T00:00",40,100)',(json.dumps(ids),))
    answers={str(r[0]):r[1] for r in db.execute('SELECT id,answer FROM questions WHERE id<=20')}
    db.commit()
    server=create_server(app,host='127.0.0.1',port=0,threads=8,connection_limit=1000,channel_timeout=60)
    port=server.effective_port
    thread=threading.Thread(target=server.run,daemon=True);thread.start()
    barrier=threading.Barrier(args.users)
    serializer=app.session_interface.get_signing_serializer(app)
    timings=[]
    from urllib.parse import urlencode
    def user(uid):
        cookie=serializer.dumps({'csrf':'load-token'} if args.login else {'uid':uid,'csrf':'load-token'})
        conn=http.client.HTTPConnection('127.0.0.1',port,timeout=90)
        headers={'Cookie':app.config['SESSION_COOKIE_NAME']+'='+cookie,'Content-Type':'application/x-www-form-urlencoded'}
        def call(method,path,data=None):
            started=time.perf_counter()
            conn.request(method,path,urlencode(data) if data else None,headers)
            r=conn.getresponse();r.read()
            elapsed=time.perf_counter()-started
            if r.status not in (200,302):raise RuntimeError(f'{method} {path}: {r.status}')
            return r,elapsed
        barrier.wait(timeout=60)
        started=time.perf_counter();local=[]
        token='load-token'
        if args.login:
            r,t=call('POST','/login',{'csrf':token,'username':str(uid),'password':'LoadTestPassword'});local.append(t)
            headers['Cookie']=r.getheader('Set-Cookie').split(';',1)[0]
            r,t=call('GET','/');local.append(t)
            headers['Cookie']=r.getheader('Set-Cookie').split(';',1)[0]
            token=serializer.loads(headers['Cookie'].split('=',1)[1])['csrf']
        r,t=call('POST','/tasks/1/start',{'csrf':token});local.append(t)
        path=r.getheader('Location')
        for _ in range(2):
            _,t=call('GET',path);local.append(t)
        _,t=call('POST',path,{'csrf':token,'save':'1',**answers});local.append(t)
        _,t=call('POST',path,{'csrf':token,**answers});local.append(t)
        _,t=call('GET','/');local.append(t)
        conn.close()
        return time.perf_counter()-started,local
    start=time.perf_counter();errors=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.users) as pool:
        futures=[pool.submit(user,uid) for uid in range(2,args.users+2)]
        for f in concurrent.futures.as_completed(futures):
            try:_,ts=f.result();timings.extend(ts)
            except Exception as e:errors.append(str(e))
    elapsed=time.perf_counter()-start
    result=dict(users=args.users,requests=len(timings),elapsed_seconds=round(elapsed,2),requests_per_second=round(len(timings)/elapsed,2),p50_ms=round(statistics.median(timings)*1000,1) if timings else None,p95_ms=round(sorted(timings)[int(len(timings)*.95)]*1000,1) if timings else None,errors=errors,submitted=db.execute('SELECT COUNT(*) FROM attempts WHERE submitted IS NOT NULL AND score=20').fetchone()[0],real_password_login=args.login,environment='Local Windows, Waitress 8 threads, SQLite WAL; '+('includes real password hashing; ' if args.login else 'synthetic signed sessions; excludes password hashing; ')+'excludes TLS; not a 2GB server benchmark')
    print(json.dumps(result,indent=2))
    Path('load-test-login-result.json' if args.login else 'load-test-result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    server.close();db.close()
    if errors or result['submitted']!=args.users:sys.exit(1)
