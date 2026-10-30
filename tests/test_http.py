"""Local HTTP boundary checks using synthetic temporary data only."""
from contextlib import closing
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from shilu import app, search
from shilu.ingest import SCHEMA


class ArchiveHTTPContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'project'
        raw = self.root / 'materials'
        raw.mkdir(parents=True)
        self.original = '虚构中文界面与本地接口测试。'.encode('utf8')
        (raw / 'fixture.txt').write_bytes(self.original)
        (Path(self.temp.name) / 'outside.txt').write_text('synthetic-outside-marker')
        db = search.db_path(self.root)
        db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(db)) as c, c:
            c.executescript(SCHEMA)
            c.execute("INSERT INTO archive_sources VALUES ('a','合成资料','qq_message','合成会话','fixture','{}')")
            fixture = [
                (1, 'a', 0, '虚构中文界面与本地接口测试。', '2020-01-01T12:00:00', '甲', 'self', '合成会话', 'qq_message', '{}', 'fixture1'),
                (2, 'a', 1, '另一条合成文本。', None, '未知', 'unknown', '', 'article', '{}', 'fixture2'),
            ]
            c.executemany('INSERT INTO archive_records VALUES (?,?,?,?,?,?,?,?,?,?,?)', fixture)
            c.execute("INSERT INTO archive_origins VALUES (1,'materials/fixture.txt','{\"line\":1}',0)")
            c.execute("INSERT INTO archive_origins VALUES (2,'../outside.txt','{}',1)")
            c.execute("INSERT INTO archive_files VALUES ('materials/fixture.txt','fixture',1,0,'imported','','a','test')")
        search.sync_index(self.root)
        self.server = app.Server(('127.0.0.1', 0), app.Handler)
        self.server.root = self.root
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, path, method='GET', headers=None, body=None):
        with closing(http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)) as conn:
            h = {'Host': '127.0.0.1:%s' % self.server.server_port}
            h.update(headers or {})
            conn.request(method, path, body=body, headers=h)
            result = conn.getresponse()
            return result.status, dict(result.getheaders()), result.read()

    def test_host_and_browser_write_origin_boundaries(self):
        self.assertEqual(self.request('/api/status', headers={'Host': 'archive.example'})[0], 403)
        self.assertEqual(self.request('/not-an-api', method='POST')[0], 403)
        write_headers = {'X-Shilu': '1', 'Content-Type': 'application/json', 'Origin': 'https://example.invalid'}
        self.assertEqual(self.request('/not-an-api', method='POST', headers=write_headers, body='{}')[0], 403)
        write_headers['Origin'] = 'http://127.0.0.1:%s' % self.server.server_port
        self.assertEqual(self.request('/not-an-api', method='POST', headers=write_headers, body='{}')[0], 404)

    def test_original_bytes_and_outside_path_rejection(self):
        code, headers, body = self.request('/api/original?record_id=1&origin=0')
        self.assertEqual(code, 200)
        self.assertEqual(body, self.original)
        self.assertIn('attachment', headers['Content-Disposition'])
        code, _, body = self.request('/api/original?record_id=2&origin=0')
        self.assertEqual(code, 400)
        self.assertNotIn(b'synthetic-outside-marker', body)
        self.assertEqual(self.request('/api/original?record_id=1&origin=-1')[0], 400)

    def test_status_filters_context_and_invalid_arguments(self):
        code, headers, body = self.request('/api/status')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['counts']['messages'], 1)
        self.assertEqual(json.loads(body)['counts']['fragments'], 1)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertEqual(json.loads(self.request('/api/filters')[2])['sources'][0]['id'], 'a')
        context = json.loads(self.request('/api/context?id=1&before=0&after=1')[2])
        self.assertEqual([r['id'] for r in context['records']], [1, 2])
        self.assertEqual(context['record']['identity'], 'self')
        self.assertEqual(context['record']['origins'][0]['path'], 'materials/fixture.txt')
        self.assertEqual(self.request('/api/search?page=not-a-number')[0], 400)
        self.assertEqual(self.request('/api/context?id=missing')[0], 400)
        self.assertEqual(self.request('/api/search?start=not-a-date')[0], 400)

    def test_changed_record_updates_fts_without_stale_postings(self):
        with closing(sqlite3.connect(search.db_path(self.root))) as c, c:
            c.execute("UPDATE archive_records SET text='更改后的独立新词测试。' WHERE id=1")
        self.assertEqual(search.sync_index(self.root)['changed'], 1)
        self.assertEqual(search.search(self.root, q='独立新词')['total'], 1)
        self.assertEqual(search.search(self.root, q='虚构中文')['total'], 0)
        with closing(sqlite3.connect(search.db_path(self.root))) as c, c:
            c.execute("INSERT INTO archive_fts(archive_fts,rank) VALUES('integrity-check',1)")

    def test_rebuild_recovers_missing_fts_table(self):
        with closing(sqlite3.connect(search.db_path(self.root))) as c, c:
            c.execute('DROP TABLE archive_fts')
        self.assertEqual(search.sync_index(self.root, rebuild=True)['indexed'], 2)
        self.assertEqual(search.search(self.root, q='虚构中文')['total'], 1)
        with closing(sqlite3.connect(search.db_path(self.root))) as c, c:
            c.execute("INSERT INTO archive_fts(archive_fts,rank) VALUES('integrity-check',1)")

    def test_update_reserves_state_before_worker_runs_and_blocks_shutdown(self):
        # Freeze the worker before its first instruction. This deterministically
        # reproduces the old window between spawning and marking UPDATE.running.
        class SuspendedThread:
            def __init__(self, target=None, daemon=None):
                self.target = target
                self.daemon = daemon
            def start(self):
                pass
        saved = dict(app.UPDATE)
        previous_thread = app.UPDATE_THREAD
        stopping = app.STOPPING.is_set()
        try:
            app.STOPPING.clear()
            app.UPDATE.update(running=False, error=None)
            with mock.patch.object(app.threading, 'Thread', SuspendedThread):
                self.assertTrue(app.start_update(self.root))
            self.assertTrue(app.UPDATE['running'])
            self.assertTrue(app.LOCK.locked())
            self.assertFalse(app.UPDATE_THREAD.daemon)
            write_headers = {'X-Shilu': '1', 'Content-Type': 'application/json'}
            self.assertEqual(self.request('/api/shutdown', method='POST', headers=write_headers, body='{}')[0], 409)
        finally:
            if app.LOCK.locked():
                app.LOCK.release()
            app.UPDATE.clear()
            app.UPDATE.update(saved)
            app.UPDATE_THREAD = previous_thread
            if stopping:
                app.STOPPING.set()
            else:
                app.STOPPING.clear()

    def test_shutdown_prevents_a_later_update_from_starting(self):
        stopping = app.STOPPING.is_set()
        try:
            app.STOPPING.clear()
            write_headers = {'X-Shilu': '1', 'Content-Type': 'application/json'}
            self.assertEqual(self.request('/api/shutdown', method='POST', headers=write_headers, body='{}')[0], 200)
            self.assertTrue(app.STOPPING.is_set())
            self.assertFalse(app.start_update(self.root))
            self.assertFalse(app.LOCK.locked())
        finally:
            if stopping:
                app.STOPPING.set()
            else:
                app.STOPPING.clear()


if __name__ == '__main__':
    unittest.main()
