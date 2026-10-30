"""Build a self-contained portable archive. Never copy user-generated folders."""
from pathlib import Path
import argparse
import os
import platform
import shutil
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--target',default=('windows-x64' if os.name=='nt' else 'macos-'+('arm64' if platform.machine()=='arm64' else 'x64')))
    args=parser.parse_args()
    subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onedir','--name','Shilu',
                    '--paths',str(ROOT/'src'),'--collect-data','shilu','--hidden-import','pypdf',str(ROOT/'launch.py')],cwd=ROOT,check=True)
    bundle=ROOT/'dist/Shilu'
    for name in ('README.md','LICENSE'):
        shutil.copy2(ROOT/name,bundle/name)
    for folder in ('qq message','wechat message'):
        (bundle/'materials'/folder).mkdir(parents=True,exist_ok=True)
    if os.name=='nt':
        for name,flag in [('备份索引.cmd','--backup'),('重建索引.cmd','--rebuild')]:
            # ASCII names are used inside the command body; the file itself may be Chinese.
            (bundle/name).write_bytes(('@echo off\r\ncd /d "%~dp0"\r\nShilu.exe '+flag+'\r\npause\r\n').encode('ascii'))
    else:
        for name,flag in [('启动拾录.command',''),('备份索引.command','--backup'),('重建索引.command','--rebuild')]:
            p=bundle/name
            p.write_text('#!/bin/zsh\ncd "$(dirname "$0")" || exit 1\n./Shilu '+flag+'\nVAN_RESULT=$?\nif [[ $VAN_RESULT != 0 ]]; then\n  read "reply?按回车关闭…"\nfi\nexit $VAN_RESULT\n')
            p.chmod(0o755)
    release=ROOT/'release';release.mkdir(exist_ok=True)
    output=release/('Shilu-'+args.target+'.zip')
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as z:
        # Symlinks are dereferenced inside the build bundle, so Windows ZIP extraction
        # and macOS Archive Utility both receive ordinary files with executable bits.
        for p in sorted(bundle.rglob('*')):
            rel=Path('Shilu')/p.relative_to(bundle)
            if p.is_file():
                z.write(p,rel.as_posix())
            elif p.is_dir() and not any(p.iterdir()):
                z.writestr(rel.as_posix()+'/',b'')
    print('Portable package:',output.name)

if __name__=='__main__':main()
