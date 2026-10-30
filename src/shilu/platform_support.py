"""Small OS adapters; the archive and search code remain platform independent."""
import errno
import os
from pathlib import Path
import subprocess
import sys


def default_root():
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


class FileLock:
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                if self.path.stat().st_size == 0:
                    handle.write(b'0'); handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise BlockingIOError('another process holds the library lock') from None
            raise
        self.handle = handle
        return self

    def close(self):
        if self.handle:
            self.handle.close()
            self.handle = None

    def __del__(self):
        self.close()


def reveal_file(path):
    path = Path(path).resolve()
    if sys.platform == 'darwin':
        args = ['/usr/bin/open', '-R', str(path)]
    elif os.name == 'nt':
        args = ['explorer.exe', '/select,' + str(path)]
    else:
        args = ['xdg-open', str(path.parent)]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
