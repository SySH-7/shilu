"""Exercise the actual frozen executable with exclusively synthetic originals."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile


def make_pdf(path):
    stream=b'BT /F1 12 Tf 60 740 Td (Offline archive PDF fixture) Tj ET'
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
             b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
             b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream']
    data=b'%PDF-1.4\n';offsets=[0]
    for i,obj in enumerate(objects,1):
        offsets.append(len(data));data+=str(i).encode()+b' 0 obj\n'+obj+b'\nendobj\n'
    start=len(data);data+=b'xref\n0 6\n0000000000 65535 f \n'
    for off in offsets[1:]:data+=('%010d 00000 n \n'%off).encode()
    data+=b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n'+str(start).encode()+b'\n%%EOF\n'
    path.write_bytes(data)


def fixtures(raw):
    raw.mkdir(parents=True,exist_ok=True)
    (raw/'note.txt').write_text('离线资料紫杉验收记录。\n第二行保留上下文。',encoding='utf8')
    q=raw/'qq message/fixture';(q/'chunks').mkdir(parents=True)
    (q/'manifest.json').write_text(json.dumps({'chatInfo':{'name':'合成QQ会话','selfUin':'self'},'statistics':{'totalMessages':2}}),encoding='utf8')
    rows=[{'time':'2024-01-02 10:00:00','sender':{'uin':'self','name':'甲'},'content':{'text':'今天复习数据库课程'}},
          {'time':'2024-01-02 10:01:00','sender':{'uin':'peer','name':'乙'},'content':{'text':'图书馆已经开门'}}]
    (q/'chunks/part.jsonl').write_text('\n'.join(json.dumps(r,ensure_ascii=False) for r in rows),encoding='utf8')
    w=raw/'wechat message';w.mkdir(parents=True,exist_ok=True)
    (w/'fixture.json').write_text(json.dumps({'session':{'displayName':'合成微信会话'},'messages':[
        {'formattedTime':'2025-03-04 18:00:00','isSend':1,'senderDisplayName':'甲','content':'周末一起讨论毕业设计'},
        {'formattedTime':'2025-03-04 18:01:00','isSend':0,'senderDisplayName':'乙','content':'收到，明天再联系'}]},ensure_ascii=False),encoding='utf8')
    with zipfile.ZipFile(raw/'article.docx','w') as z:
        z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>文稿解析独立验收内容。</w:t></w:r></w:p></w:body></w:document>')
    make_pdf(raw/'article.pdf')


def request(url,path,post=False):
    r=urllib.request.Request(url+path,data=b'{}' if post else None,headers={'X-Shilu':'1','Content-Type':'application/json'})
    with urllib.request.urlopen(r,timeout=30) as response:
        body=response.read()
        return json.loads(body) if response.headers.get_content_type()=='application/json' else body


def main():
    parser=argparse.ArgumentParser();parser.add_argument('executable',type=Path);args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='shilu-smoke-') as temp:
        # Copy the entire onedir bundle; no --root override. This verifies relocation.
        root=Path(temp)/'移动后的资料库'
        shutil.copytree(args.executable.resolve().parent,root)
        exe=root/args.executable.name
        raw=root/'materials';fixtures(raw)
        before={str(p.relative_to(raw)):hashlib.sha256(p.read_bytes()).hexdigest() for p in raw.rglob('*') if p.is_file()}
        for cycle in range(2):
            with (root/'smoke.log').open('a',encoding='utf8') as log:
                process=subprocess.Popen([str(exe),'--no-browser','--port','0'],cwd=root,stdout=log,stderr=log)
                state=root/'data/runtime/shilu-server.json'
                try:
                    deadline=time.monotonic()+90
                    while True:
                        if process.poll() is not None:raise AssertionError('executable exited before becoming ready')
                        try:
                            url=json.loads(state.read_text(encoding='utf-8'))['url'];health=request(url,'api/status')
                            if health['service']=='Shilu':break
                        except (OSError,ValueError):pass
                        if time.monotonic()>deadline:raise AssertionError('executable startup timeout')
                        time.sleep(.15)
                    assert url.startswith('http://127.0.0.1:')
                    assert health['counts']['failed']==0, health['counts']
                    assert health['counts']['messages']==4
                    for query in ['紫杉','数据库','毕业设计','文稿解析','archive PDF','图']:
                        hits=request(url,'api/search?'+urllib.parse.urlencode({'q':query}))
                        assert hits['total']>=1, 'query failed: '+query
                    assert request(url,'api/search?'+urllib.parse.urlencode({'q':'数据库','start':'2024-01-02','end':'2024-01-02','self':'1'}))['total']==1
                    item=request(url,'api/search?'+urllib.parse.urlencode({'q':'数据库'}))['results'][0]
                    detail=request(url,'api/context?id='+str(item['id']))
                    assert len(detail['records'])==2
                    assert request(url,'api/original?record_id='+str(item['id']))==(root/item['origins'][0]['path']).read_bytes()
                    assert '拾录' in request(url,'').decode('utf8')
                    if cycle==0:
                        count=health['counts']['records']
                        request(url,'api/update',True)
                        while request(url,'api/status')['update']['running']:time.sleep(.1)
                        assert request(url,'api/status')['counts']['records']==count
                        (raw/'new.md').write_text('增量更新菩提验收。',encoding='utf8')
                        request(url,'api/update',True)
                        while request(url,'api/status')['update']['running']:time.sleep(.1)
                        assert request(url,'api/search?'+urllib.parse.urlencode({'q':'菩提'}))['total']==1
                        (raw/'new.md').unlink()
                        request(url,'api/update',True)
                        while request(url,'api/status')['update']['running']:time.sleep(.1)
                        assert request(url,'api/search?'+urllib.parse.urlencode({'q':'菩提'}))['total']==0
                    request(url,'api/shutdown',True);process.wait(timeout=15);assert process.returncode==0
                finally:
                    if process.poll() is None:process.terminate();process.wait(timeout=15)
        after={str(p.relative_to(raw)):hashlib.sha256(p.read_bytes()).hexdigest() for p in raw.rglob('*') if p.is_file()}
        assert before==after
        print('PASS: frozen executable, relocated Chinese path, QQ/WeChat/TXT/DOCX/PDF, short search, filters, context, incremental import, restart, original hashes')

if __name__=='__main__':main()
