"""Reject generated archives, credentials and machine paths in tracked source."""
from pathlib import Path
import re
import subprocess

ROOT=Path(__file__).resolve().parents[1]
TOP={'README.md','RELEASE.md','LICENSE','pyproject.toml','requirements-build.txt','launch.py','.gitignore'}
PREFIXES=('src/shilu/','tests/','scripts/','.github/workflows/','licenses/')
PATTERNS=[re.compile(rb'gh[pousr]_[A-Za-z0-9]{20,}'),re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
          re.compile(rb'/Users/[A-Za-z0-9_.-]+/'),re.compile(rb'[A-Z]:\\Users\\[A-Za-z0-9_.-]+\\')]


def main():
    tracked=subprocess.check_output(['git','ls-files','-z'],cwd=ROOT).decode().split('\0')
    failures=[]
    for rel in filter(None,tracked):
        p=ROOT/rel
        if rel not in TOP and not rel.startswith(PREFIXES):failures.append((rel,'outside public source allowlist'));continue
        if p.is_symlink():failures.append((rel,'symlink'));continue
        if p.suffix.lower() in {'.sqlite','.sqlite3','.db','.jsonl','.pdf','.docx','.doc','.png','.jpg'}:
            failures.append((rel,'archive or personal-media file'));continue
        data=p.read_bytes()
        if len(data)>500000:failures.append((rel,'unexpected large source file'))
        if any(pattern.search(data) for pattern in PATTERNS):failures.append((rel,'credential or local home path'))
    if failures:
        for rel,reason in failures:print('REJECT',rel,reason)
        raise SystemExit(1)
    print('PASS: tracked source allowlist; no archive, personal media, credentials or local home paths')

if __name__=='__main__':main()
