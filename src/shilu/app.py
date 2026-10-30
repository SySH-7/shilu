"""Shilu: one process, loopback-only offline archive browser and maintenance CLI."""
from __future__ import annotations
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from contextlib import closing
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, quote
from . import search as retrieval

from .platform_support import FileLock, default_root, reveal_file
ROOT = default_root()
UPDATE = {'running':False,'error':None,'last_result':None}
LOCK=threading.Lock()
STOPPING=threading.Event()
UPDATE_THREAD=None


def status(root):
    counts = dict.fromkeys(['files','sources','records','messages','fragments','imported','duplicate','excluded','failed'], 0)
    path=retrieval.db_path(root)
    if path.exists():
        with closing(retrieval.connect(root)) as c:
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='archive_records'").fetchone():
                counts.update(files=c.execute('SELECT count(*) FROM archive_files').fetchone()[0],
                              sources=c.execute('SELECT count(*) FROM archive_sources').fetchone()[0],
                              records=c.execute('SELECT count(*) FROM archive_records').fetchone()[0],
                              messages=c.execute("SELECT count(*) FROM archive_records WHERE kind IN ('qq_message','wechat_message')").fetchone()[0])
                counts['fragments']=counts['records']-counts['messages']
                for row in c.execute('SELECT status,count(*) FROM archive_files GROUP BY status'):
                    counts[row[0]]=row[1]
                counts['imported']=counts.get('imported',0)+counts.get('success',0)
                last=c.execute("SELECT value FROM archive_meta WHERE key='updated_at'").fetchone()
                if last and not UPDATE.get('finished_at'): UPDATE['finished_at']=last[0]
    return {'service':'Shilu','version':'1.0','local_only':True,'counts':counts,'update':dict(UPDATE)}


def _perform_update(root, force=False):
    try:
        from .ingest import update as ingest
        result=ingest(root,force=force)
        if 'search_index' not in result: result['search_index']=retrieval.sync_index(root)
        UPDATE['last_result']=result
        UPDATE['finished_at']=datetime.now().isoformat(timespec='seconds')
        return result
    except Exception as e:
        UPDATE['error']='更新失败（%s），请查看本地报告并保留原件。' % type(e).__name__
        raise
    finally:
        UPDATE['running']=False
        LOCK.release()


def update(root, force=False):
    if not LOCK.acquire(blocking=False): raise ValueError('已有更新任务运行中')
    UPDATE.update(running=True,error=None)
    return _perform_update(root,force)


def start_update(root):
    global UPDATE_THREAD
    if not LOCK.acquire(blocking=False): return False
    if STOPPING.is_set(): LOCK.release(); return False
    UPDATE.update(running=True,error=None)
    def run():
        try: _perform_update(root)
        except Exception: pass
    UPDATE_THREAD=threading.Thread(target=run,daemon=False)
    UPDATE_THREAD.start()
    return True


def backup(root, destination=None):
    destination=Path(destination) if destination else Path(root)/'backups'/('shilu-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
    destination.mkdir(parents=True,exist_ok=False)
    source=retrieval.db_path(root)
    mode='sqlite_online_backup'
    if source.exists():
        try:
            with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(destination/'index.sqlite3')) as dst:
                src.backup(dst)
        except sqlite3.DatabaseError:
            # Preserve damaged bytes before recovery; they are never labelled a healthy index.
            mode='raw_copy_unverified'
            shutil.copy2(source,destination/'index.sqlite3')
            for suffix in ('-wal','-shm'):
                side=Path(str(source)+suffix)
                if side.exists(): shutil.copy2(side,destination/('index.sqlite3'+suffix))
    for name in ['archive_normalized']:
        p=Path(root)/'data'/name
        if p.exists(): shutil.copytree(p,destination/name)
    (destination/'backup.json').write_text(json.dumps({'format':'shilu-backup-1','created':datetime.now().isoformat(),'raw_included':False,'database_backup_mode':mode},ensure_ascii=False,indent=2),encoding='utf-8')
    return destination


def restore(root, source):
    source=Path(source).resolve()
    if not (source/'index.sqlite3').is_file(): raise ValueError('备份缺少数据库')
    # This command must be run while the server is stopped; acquire the same lock in main.
    check=sqlite3.connect((source/'index.sqlite3').as_uri()+'?mode=ro',uri=True)
    if check.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
        check.close(); raise ValueError('备份数据库完整性检查失败')
    check.close()
    saved=backup(root)
    target=retrieval.db_path(root);target.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(source/'index.sqlite3')) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
    normalized=source/'archive_normalized'
    if normalized.exists():
        target_norm=Path(root)/'data/archive_normalized'
        shutil.rmtree(target_norm,ignore_errors=True); shutil.copytree(normalized,target_norm)
    # Derive search again if a backup was made before an indexing interruption.
    retrieval.sync_index(root,rebuild=True)
    return saved


class Server(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass

    def valid_host(self):
        return self.headers.get('Host','') in ('127.0.0.1:%d'%self.server.server_port,'localhost:%d'%self.server.server_port)

    def respond(self,data,code=200):
        body=json.dumps(data,ensure_ascii=False).encode('utf8')
        self.send_response(code);self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(body)));self.headers_safe();self.end_headers();self.wfile.write(body)

    def headers_safe(self):
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'")
        self.send_header('Referrer-Policy','no-referrer')

    def original(self,record_id,origin=0):
        item=retrieval.context(self.server.root,record_id,0,0)['record']
        origins=item['origins'];index=int(origin)
        if index < 0 or index>=len(origins): raise ValueError('原文映射不存在')
        raw=(self.server.root/'materials').resolve()
        p=(self.server.root/origins[index]['path']).resolve()
        if not p.is_relative_to(raw) or not p.is_file(): raise ValueError('原件已移动或不可用')
        return p

    def do_GET(self):
        if not self.valid_host(): return self.respond({'error':'Host 不受信任'},403)
        u=urlparse(self.path);p={k:v[-1] for k,v in parse_qs(u.query).items()}
        if UPDATE['running'] and u.path in ('/api/search','/api/context','/api/filters','/api/original'):
            return self.respond({'error':'资料正在更新，请稍候再检索。'},503)
        try:
            if u.path=='/api/status': return self.respond(status(self.server.root))
            if u.path=='/api/filters': return self.respond(retrieval.filters(self.server.root))
            if u.path=='/api/search':
                args={k:p.get(k,'') for k in ['q','start','end','kind','source','conversation','speaker']}
                args.update(self_only=p.get('self')=='1',phrase=p.get('phrase')=='1',page=p.get('page',1),size=p.get('size',30))
                return self.respond(retrieval.search(self.server.root,**args))
            if u.path=='/api/context': return self.respond(retrieval.context(self.server.root,p.get('id'),p.get('before',5),p.get('after',5)))
            if u.path=='/api/files':
                with closing(retrieval.connect(self.server.root)) as c:
                    sql='SELECT * FROM archive_files';v=[]
                    if p.get('status'):sql+=' WHERE status=?';v=[p['status']]
                    rows=[dict(r) for r in c.execute(sql+' ORDER BY status,path',v)]
                return self.respond({'files':rows})
            if u.path=='/api/original':
                path=self.original(p.get('record_id'),p.get('origin',0))
                self.send_response(200);self.send_header('Content-Type','application/octet-stream')
                self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+quote(path.name))
                self.send_header('Content-Length',str(path.stat().st_size));self.headers_safe();self.end_headers()
                with path.open('rb') as f:shutil.copyfileobj(f,self.wfile)
                return
            name={'/':'index.html','/app.js':'app.js','/app.css':'app.css'}.get(u.path)
            if name:
                data=(Path(__file__).parent/'web'/name).read_bytes()
                self.send_response(200);self.send_header('Content-Type',{'html':'text/html; charset=utf-8','css':'text/css; charset=utf-8','js':'application/javascript; charset=utf-8'}[name.split('.')[-1]])
                self.send_header('Content-Length',str(len(data)));self.headers_safe();self.end_headers();self.wfile.write(data);return
            self.respond({'error':'地址不存在'},404)
        except (ValueError,TypeError): self.respond({'error':'参数无效，或记录已更新。请重新搜索。'},400)
        except sqlite3.Error: self.respond({'error':'索引暂不可用，请更新资料或运行重建索引。'},503)
        except (BrokenPipeError,ConnectionResetError): pass
        except Exception: self.respond({'error':'请求失败，请检查原文件是否仍存在。'},500)

    def do_POST(self):
        if not self.valid_host(): return self.respond({'error':'Host 不受信任'},403)
        allowed=('http://127.0.0.1:%d'%self.server.server_port,'http://localhost:%d'%self.server.server_port)
        if self.headers.get('X-Shilu')!='1' or self.headers.get('Origin',allowed[0]) not in allowed:
            return self.respond({'error':'只接受本机页面操作'},403)
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length>4096: return self.respond({'error':'请求过大'},413)
            body=json.loads(self.rfile.read(length) or b'{}')
            if self.path=='/api/update':
                if UPDATE['running']:return self.respond({'running':True})
                if not start_update(self.server.root): return self.respond({'error':'程序正在退出或更新，请稍后重试'},409)
                return self.respond({'running':True},202)
            if self.path=='/api/reveal':
                path=self.original(body.get('record_id'),body.get('origin',0))
                reveal_file(path)
                return self.respond({'ok':True})
            if self.path=='/api/shutdown':
                if not LOCK.acquire(blocking=False): return self.respond({'error':'资料更新中，请完成后退出'},409)
                STOPPING.set();LOCK.release()
                self.respond({'ok':True});threading.Thread(target=self.server.shutdown,daemon=True).start();return
            self.respond({'error':'地址不存在'},404)
        except Exception: self.respond({'error':'操作失败，请检查参数和原件'},400)


def main(argv=None):
    for output in (sys.stdout,sys.stderr):
        if output is not None and hasattr(output,'reconfigure'): output.reconfigure(encoding='utf-8',errors='replace')
    parser=argparse.ArgumentParser(description='Shilu 离线个人档案检索')
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--no-browser',action='store_true')
    parser.add_argument('--update',action='store_true')
    parser.add_argument('--rebuild',action='store_true')
    parser.add_argument('--rebuild-search',action='store_true')
    parser.add_argument('--backup',action='store_true')
    parser.add_argument('--restore',type=Path)
    args=parser.parse_args(argv);root=args.root.resolve()
    (root/'materials').mkdir(parents=True,exist_ok=True)
    runtime=root/'data/runtime';runtime.mkdir(parents=True,exist_ok=True)
    lock=FileLock(runtime/'shilu.lock')
    try: lock.acquire()
    except BlockingIOError:
        if not any([args.update,args.rebuild,args.rebuild_search,args.backup,args.restore]):
            try:
                state=json.loads((runtime/'shilu-server.json').read_text(encoding='utf-8'));url=state['url']
                health=json.load(urllib.request.urlopen(url+'api/status',timeout=3))
                if health.get('service')=='Shilu':
                    if not args.no_browser:webbrowser.open(url)
                    print('Shilu 已运行：'+url);return
            except Exception: pass
        raise SystemExit('Shilu 正在运行，请在页面点击“退出程序”后进行维护。')
    if args.backup: print('备份完成：'+str(backup(root)));return
    if args.restore: print('已恢复索引；恢复前备份：'+str(restore(root,args.restore)));return
    if args.rebuild_search:
        backup(root);print(json.dumps(retrieval.sync_index(root,rebuild=True)));return
    if args.update or args.rebuild:
        if args.rebuild:
            saved=backup(root)
            path=retrieval.db_path(root)
            if path.exists():
                try:
                    with closing(sqlite3.connect(path)) as check:
                        healthy=check.execute('PRAGMA quick_check').fetchone()[0]=='ok'
                except sqlite3.DatabaseError: healthy=False
                if not healthy:
                    # Full corruption cannot be repaired by SQL; original inputs remain authoritative.
                    path.unlink()
                    for suffix in ('-wal','-shm'): Path(str(path)+suffix).unlink(missing_ok=True)
                    print('损坏索引已保留在备份中，正在从原件重建。',flush=True)
        result=update(root,force=args.rebuild)
        print(json.dumps({k:v for k,v in result.items() if k not in ('failed','excluded','duplicate_files')},ensure_ascii=False));return
    path=retrieval.db_path(root)
    initialized=False
    if path.exists():
        with closing(sqlite3.connect(path)) as c:initialized=bool(c.execute("SELECT 1 FROM sqlite_master WHERE name='archive_records'").fetchone())
    if not initialized: update(root)
    try: server=Server(('127.0.0.1',args.port),Handler)
    except OSError: server=Server(('127.0.0.1',0),Handler)
    server.root=root
    url='http://127.0.0.1:%d/'%server.server_port
    state=runtime/'shilu-server.json';state.write_text(json.dumps({'url':url,'pid':os.getpid()}),encoding='utf-8')
    print('Shilu 已启动：'+url,flush=True)
    if not args.no_browser:webbrowser.open(url)
    try: server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt: pass
    finally:
        STOPPING.set()
        if UPDATE_THREAD and UPDATE_THREAD.is_alive():
            print('等待当前更新安全完成…',flush=True);UPDATE_THREAD.join()
        server.server_close();state.unlink(missing_ok=True)

if __name__=='__main__':main()
