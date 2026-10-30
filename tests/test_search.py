"""Independent offline search contract checks; all fixtures are synthetic."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile
from shilu import ingest

from shilu import search as searcher


class ArchiveSearchContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        db = self.root / 'data/index.sqlite3'
        db.parent.mkdir(parents=True)
        with sqlite3.connect(db) as c:
            c.executescript('''
            CREATE TABLE archive_sources(id TEXT PRIMARY KEY,title TEXT,metadata_json TEXT DEFAULT '{}');
            CREATE TABLE archive_records(id INTEGER PRIMARY KEY,source_id TEXT,ordinal INTEGER,text TEXT,
              occurred_at TEXT,speaker TEXT,identity TEXT,conversation TEXT,kind TEXT,location TEXT,content_hash TEXT);
            CREATE TABLE archive_origins(record_id INTEGER,path TEXT,location TEXT,ordinal INTEGER);
            INSERT INTO archive_sources(id,title) VALUES('a','虚构会话甲'),('b','虚构文章乙');
            ''')
            fixture = [
                (1,'a',0,'今天准备去图书馆复习数据库','2022-01-01 08:00:00','甲','self','会话甲','qq_message'),
                (2,'a',1,'图书馆今天开门吗','2022-01-01 09:00:00','乙','peer','会话甲','qq_message'),
                (3,'a',2,'明天复习算法，数据库下周再看','2023-01-02 09:00:00','甲','self','会话甲','qq_message'),
                (4,'b',0,'小说人物说今天准备去图书馆复习数据库','2024-01-01 11:00:00','未知','unknown','','creative_writing'),
                (5,'b',1,'离线 ＡＢＣ 文本：字符与双字符搜索','2024-02-02T23:59:59','未知','unknown','','article'),
                (6,'a',3,'数据库 和 算法 都需认真复习','2024-02-03T00:00:00','甲','self','会话甲','wechat_message'),
                (7,'a',4,"字面引号'以及 OR 语法不会执行",None,'乙','peer','会话甲','wechat_message'),
            ]
            c.executemany('INSERT INTO archive_records VALUES(?,?,?,?,?,?,?,?,?,\'{}\',\'fixture\')', fixture)
            c.executemany('INSERT INTO archive_origins VALUES(?,\'materials/fixture.txt\',\'{}\',?)', [(r[0], r[2]) for r in fixture])
            c.execute("INSERT INTO archive_origins VALUES(1,'materials/duplicate.txt','{}',0)")
        searcher.sync_index(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def ids(self, **kwargs):
        return [r['id'] for r in searcher.search(self.root, size=100, **kwargs)['results']]

    def test_chinese_single_and_double_char(self):
        self.assertEqual(set(self.ids(q='图')), {1,2,4})
        self.assertEqual(set(self.ids(q='算法')), {3,6})

    def test_long_contiguous_literal_and_quoted_phrase(self):
        self.assertEqual(set(self.ids(q='准备去图书馆复习数据')), {1,4})
        self.assertEqual(self.ids(q='"数据库 和 算法"'), [6])
        self.assertEqual(self.ids(q='数据库 和 算法', phrase=True), [6])

    def test_multi_term_and_checks_actual_substrings(self):
        self.assertEqual(set(self.ids(q='数据库 算法')), {3,6})
        self.assertEqual(self.ids(q='算法 图书馆'), [])
        self.assertEqual(self.ids(q='图书馆 算法 不存在'), [])

    def test_filters_and_self_identity_are_separate_from_authorship(self):
        self.assertEqual(self.ids(q='图书馆', self_only=True), [1])
        self.assertEqual(self.ids(q='数据库', start='2023-01-01',end='2023-12-31',speaker='甲',conversation='会话甲'), [3])
        self.assertEqual(self.ids(q='图书馆',source='b'), [4])
        self.assertEqual(self.ids(q='图书馆',kind='creative_writing'), [4])

    def test_date_is_inclusive_to_end_of_day(self):
        self.assertEqual(self.ids(q='字符',start='2024-02-02',end='2024-02-02'), [5])
        self.assertEqual(self.ids(q='复习',end='2022-01-01'), [1])

    def test_unicode_normalization_and_literal_sql(self):
        self.assertEqual(self.ids(q='abc'), [5])
        self.assertEqual(self.ids(q="字面引号'"), [7])
        self.assertEqual(self.ids(q="' OR 1=1 --"), [])

    def test_context_does_not_cross_source_and_preserves_duplicate_origins(self):
        detail = searcher.context(self.root, 1, before=5, after=2)
        self.assertEqual([r['id'] for r in detail['records']], [1,2,3])
        self.assertEqual(len(detail['record']['origins']), 2)
        self.assertEqual(len(self.ids(q='今天准备去图书馆复习数据库')),2)

    def test_idempotent_index_and_rebuild(self):
        self.assertEqual(searcher.sync_index(self.root)['changed'],0)
        original = self.ids(q='数据库')
        searcher.sync_index(self.root,rebuild=True)
        self.assertEqual(self.ids(q='数据库'),original)

    def test_replaced_same_id_removes_stale_fts_tokens(self):
        with searcher.connect(self.root) as c:
            c.execute("UPDATE archive_records SET text='全新唯一内容' WHERE id=1")
        searcher.sync_index(self.root)
        self.assertEqual(self.ids(q='全新唯一内容'),[1])
        self.assertNotIn(1,self.ids(q='图书馆'))
        with searcher.connect(self.root) as c:
            gram=searcher.query_grams(searcher.normalize('图书'))[0]
            candidates=[r[0] for r in c.execute('SELECT rowid FROM archive_fts WHERE archive_fts MATCH ?',(gram,))]
            self.assertNotIn(1,candidates)

    def test_missing_fts_derived_table_rebuilds_from_records(self):
        before=self.ids(q='数据库')
        with searcher.connect(self.root) as c: c.execute('DROP TABLE archive_fts')
        searcher.sync_index(self.root,rebuild=True)
        self.assertEqual(self.ids(q='数据库'),before)

    def test_input_limits_and_unknown_result(self):
        self.assertEqual(self.ids(q='完全不存在的关键词'), [])
        with self.assertRaises(ValueError): searcher.search(self.root,q='x'*1001)
        with self.assertRaises(ValueError): searcher.search(self.root,start='not-a-date')
        with self.assertRaises(ValueError): searcher.context(self.root,9999)


class ArchiveParserBoundaryContract(unittest.TestCase):
    def test_literal_crossing_txt_slice_is_found_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); originals=root/'materials'; originals.mkdir()
            phrase='跨越切片边界的连续中文短语'
            (originals/'ordinary.txt').write_text('甲'*1598+phrase+'乙'*1700,encoding='utf-8')
            ingest.update(root)
            searcher.sync_index(root)
            result=searcher.search(root,q=phrase)
            self.assertEqual(result['total'],1)
            self.assertIn(phrase,result['results'][0]['text'])
            detail=searcher.context(root,result['results'][0]['id'])
            self.assertIn('source_metadata',detail['record'])
            self.assertEqual((originals/'ordinary.txt').read_text(encoding='utf-8'),'甲'*1598+phrase+'乙'*1700)

    def test_docx_paragraph_boundary_keeps_exact_phrase_and_location(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); originals=root/'materials'; originals.mkdir()
            paragraphs=['甲'*1598+'前段结尾','后段开头'+'乙'*1600]
            with zipfile.ZipFile(originals/'ordinary.docx','w') as archive:
                archive.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'+''.join('<w:p><w:r><w:t>'+p+'</w:t></w:r></w:p>' for p in paragraphs)+'</w:body></w:document>')
            ingest.update(root)
            searcher.sync_index(root)
            phrase='前段结尾\n\n后段开头'
            result=searcher.search(root,q=phrase,phrase=True)
            self.assertEqual(result['total'],1)
            row=result['results'][0]; loc=row['origins'][0]['location']
            self.assertEqual('\n\n'.join(paragraphs)[loc['doc_char_start']:loc['doc_char_end']],row['text'])
            self.assertIn(phrase,row['text'])


if __name__ == '__main__':
    unittest.main()
