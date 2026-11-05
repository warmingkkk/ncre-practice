"""Inspect or recover an existing administrator from the server console.

docker compose exec web python scripts/admin_access.py status
docker compose exec web python scripts/admin_access.py reset
docker compose exec web python scripts/admin_access.py reset --from-env
No web endpoint; never creates a new database or account and never prints passwords.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone, timedelta
import getpass
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid
from werkzeug.security import check_password_hash, generate_password_hash


def connect_existing(path, readonly=False):
    path=Path(path).resolve()
    if not path.is_file():
        raise ValueError('未找到现有数据库，请检查数据卷挂载；工具不会新建数据库')
    conn=sqlite3.connect(path.as_uri()+('?mode=ro' if readonly else '?mode=rw'),uri=True,timeout=20)
    conn.row_factory=sqlite3.Row
    return conn


def find_admin(conn,username):
    user=conn.execute('SELECT * FROM users WHERE username=? AND role="admin"',(username,)).fetchone()
    if not user:
        raise ValueError('未找到该管理员，请核对账号和数据卷；工具不会创建或提升账号权限')
    return dict(user)


def status(path,username='admin',environment_password=None):
    with closing(connect_existing(path,readonly=True)) as conn:
        user=find_admin(conn,username)
        tables={r['name'] for r in conn.execute('SELECT name FROM sqlite_master WHERE type="table"')}
        lock=conn.execute('SELECT * FROM login_limits WHERE username=?',(username[:100],)).fetchone() if 'login_limits' in tables else None
        remaining=max(0,int(lock['until']-time.time())+1) if lock else 0
        counts={name:conn.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] for name in ('classes','users','attempts') if name in tables}
        matches=None
        if environment_password:
            try: matches=check_password_hash(user['password'],environment_password)
            except (ValueError,TypeError): matches=False
        return dict(database=str(Path(path).resolve()),username=user['username'],disabled=bool(user.get('deleted_at')),must_change=bool(user['must_change']),locked=remaining>0,lock_remaining_seconds=remaining,matches_environment_password=matches,record_counts=counts)


def reset_admin(path,username,password):
    if not 10<=len(password)<=128:
        raise ValueError('恢复密码需为10至128位；未更改数据库')
    password_hash=generate_password_hash(password)
    with closing(connect_existing(path)) as conn:
        find_admin(conn,username)
        backup=Path(path).resolve().with_name('admin-recovery-backup-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]+'.sqlite3')
        with closing(sqlite3.connect(backup)) as target:
            conn.backup(target)
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            user=find_admin(conn,username)
            columns=set(user)
            updates=['password=?','must_change=1']
            if 'session_version' in columns: updates.append('session_version=session_version+1')
            if 'deleted_at' in columns: updates.append('deleted_at=NULL')
            conn.execute('UPDATE users SET '+','.join(updates)+' WHERE id=? AND role="admin"',(password_hash,user['id']))
            tables={r['name'] for r in conn.execute('SELECT name FROM sqlite_master WHERE type="table"')}
            if 'login_limits' in tables: conn.execute('DELETE FROM login_limits WHERE username=?',(username[:100],))
            if 'audit' in tables:
                conn.execute('INSERT INTO audit(user_id,action,created) VALUES(?,?,?)',(user['id'],'服务器控制台恢复管理员登录',datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%dT%H:%M')))
        return dict(result='管理员密码已重设，请登录后修改密码',username=username,backup=str(backup),must_change=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description='只操作现有管理员；恢复前自动备份，不输出密码')
    parser.add_argument('--username',default='admin')
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('status',help='只读检查账号、登录锁定和记录数量')
    reset=commands.add_parser('reset',help='备份并重设管理员密码')
    reset.add_argument('--from-env',action='store_true',help='使用容器当前 ADMIN_PASSWORD；不回显密码')
    args=parser.parse_args(argv)
    path=Path(os.environ.get('DATA_DIR',Path(__file__).resolve().parents[1]/'data'))/'practice.db'
    try:
        if args.command=='status':
            result=status(path,args.username,os.environ.get('ADMIN_PASSWORD'))
        else:
            if args.from_env:
                password=os.environ.get('ADMIN_PASSWORD','')
                if not password: raise ValueError('容器中没有配置 ADMIN_PASSWORD，请改用交互式 reset')
            else:
                password=getpass.getpass('新管理员临时密码（至少10位，不回显）：')
                if password!=getpass.getpass('再次输入：'): raise ValueError('两次输入不一致；未更改数据库')
            result=reset_admin(path,args.username,password)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    except (ValueError,sqlite3.Error,OSError,EOFError) as e:
        print('操作失败：'+str(e),file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
