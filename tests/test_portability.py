"""Native platform checks run unchanged on Windows and both macOS architectures."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from shilu.platform_support import FileLock
from shilu import app,ingest,search


class PortabilityTests(unittest.TestCase):
    def test_lock_excludes_other_process_and_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'锁文件';lock=FileLock(path).acquire()
            code='from shilu.platform_support import FileLock; from pathlib import Path; import sys; lock=FileLock(Path(sys.argv[1])); lock.acquire()'
            try:
                result=subprocess.run([sys.executable,'-c',code,str(path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                self.assertNotEqual(result.returncode,0)
            finally:lock.close()
            result=subprocess.run([sys.executable,'-c',code,str(path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))

    def test_changed_file_duplicate_delete_and_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'中文资料库';raw=root/'materials';raw.mkdir(parents=True)
            original=raw/'a.txt';original.write_text('原始合成测试词条',encoding='utf8')
            first=ingest.update(root);second=ingest.update(root)
            self.assertEqual(first['records_total'],second['records_total']);self.assertEqual(second['units_parsed'],0)
            original.write_text('更改后的合成唯一词条',encoding='utf8');ingest.update(root)
            self.assertEqual(search.search(root,'原始合成')['total'],0)
            copy=raw/'b.txt';copy.write_bytes(original.read_bytes());ingest.update(root)
            hit=search.search(root,'唯一词条');self.assertEqual(hit['total'],1);self.assertEqual(len(hit['results'][0]['origins']),2)
            original.unlink();ingest.update(root);self.assertEqual(len(search.search(root,'唯一词条')['results'][0]['origins']),1)
            saved=app.backup(root,root/'backups/test')
            copy.write_text('备份后临时修改的词条',encoding='utf8');ingest.update(root)
            app.restore(root,saved)
            self.assertEqual(search.search(root,'唯一词条')['total'],1)
            self.assertEqual(copy.read_text(encoding='utf8'),'备份后临时修改的词条')

if __name__=='__main__':unittest.main()
