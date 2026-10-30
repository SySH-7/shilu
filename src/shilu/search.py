"""Offline literal search. FTS narrows Unicode grams; exact text checks decide hits."""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
import time
import unicodedata
from contextlib import closing
from pathlib import Path


def db_path(root):
    return Path(root) / 'data/index.sqlite3'


def connect(root):
    c = sqlite3.connect(db_path(root), timeout=120)
    c.row_factory = sqlite3.Row
    c.create_function('archive_normalize', 1, normalize, deterministic=True)
    return c


def normalize(text):
    return unicodedata.normalize('NFKC', text or '').casefold()


def gram_tokens(text):
    # Encoded grams avoid tokenizer punctuation rules. Unique grams bound index size.
    chars = set(text)
    pairs = set(zip(text, text[1:]))
    return ' '.join(['u%x' % ord(x) for x in sorted(chars)] +
                    ['b%xz%x' % (ord(a), ord(b)) for a, b in sorted(pairs)])


def query_grams(term):
    if len(term) == 1:
        return ['u%x' % ord(term)]
    return sorted(set('b%xz%x' % (ord(a), ord(b)) for a, b in zip(term, term[1:])))


def setup(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS search_docs(id INTEGER PRIMARY KEY, normalized TEXT NOT NULL, tokens TEXT NOT NULL, digest TEXT NOT NULL);
    CREATE VIRTUAL TABLE IF NOT EXISTS archive_fts USING fts5(tokens,content='search_docs',content_rowid='id');
    CREATE TRIGGER IF NOT EXISTS search_ai AFTER INSERT ON search_docs BEGIN
      INSERT INTO archive_fts(rowid,tokens) VALUES(new.id,new.tokens); END;
    CREATE TRIGGER IF NOT EXISTS search_ad AFTER DELETE ON search_docs BEGIN
      INSERT INTO archive_fts(archive_fts,rowid,tokens) VALUES('delete',old.id,old.tokens); END;
    CREATE TRIGGER IF NOT EXISTS search_au AFTER UPDATE ON search_docs BEGIN
      INSERT INTO archive_fts(archive_fts,rowid,tokens) VALUES('delete',old.id,old.tokens);
      INSERT INTO archive_fts(rowid,tokens) VALUES(new.id,new.tokens); END;
    CREATE INDEX IF NOT EXISTS archive_records_filter ON archive_records(kind,occurred_at);
    CREATE INDEX IF NOT EXISTS archive_records_context ON archive_records(source_id,ordinal);
    CREATE INDEX IF NOT EXISTS archive_origins_record ON archive_origins(record_id);
    ''')


def sync_index(root, rebuild=False):
    with closing(connect(root)) as c:
        if rebuild:
            # Derived tables can be missing or inconsistent after disk damage.
            c.executescript('DROP TRIGGER IF EXISTS search_ai; DROP TRIGGER IF EXISTS search_ad; DROP TRIGGER IF EXISTS search_au; DROP TABLE IF EXISTS archive_fts; DROP TABLE IF EXISTS search_docs;')
        setup(c)
        c.execute('DELETE FROM search_docs WHERE id NOT IN (SELECT id FROM archive_records)')
        # SQL compares normalized text so an unchanged update does no tokenization.
        cursor = c.execute('''SELECT r.id,r.text FROM archive_records r LEFT JOIN search_docs d ON d.id=r.id
                           WHERE d.id IS NULL OR d.normalized != archive_normalize(r.text)''')
        changed = 0
        while True:
            rows = cursor.fetchmany(1500)
            if not rows:
                break
            batch = []
            for r in rows:
                normalized = normalize(r['text'])
                batch.append((r['id'], normalized, gram_tokens(normalized), hashlib.sha256(normalized.encode()).hexdigest()))
            c.executemany('INSERT INTO search_docs VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET normalized=excluded.normalized,tokens=excluded.tokens,digest=excluded.digest', batch)
            changed += len(rows)
        c.commit()
        return {'indexed': c.execute('SELECT count(*) FROM search_docs').fetchone()[0], 'changed': changed}


def terms_from_query(q, phrase=False):
    if len(q) > 1000:
        raise ValueError('搜索内容请控制在 1000 字以内')
    if phrase:
        return [normalize(q.strip())] if q.strip() else []
    # Quoted phrases retain their spaces; unquoted whitespace means AND.
    pieces = re.findall(r'"([^"]+)"|“([^”]+)”|(\S+)', q)
    terms = [normalize(next(x for x in p if x).strip()) for p in pieces]
    if len(terms) > 30:
        raise ValueError('一次最多输入 30 个关键词')
    return list(dict.fromkeys(t for t in terms if t))


def _origins(c, record_id):
    return [dict(r) | {'location': json.loads(r['location'] or '{}')} for r in c.execute(
        'SELECT path,location,ordinal FROM archive_origins WHERE record_id=? ORDER BY path,ordinal', (record_id,))]


def serialize(c, row, terms=()):
    item = dict(row)
    item.pop('normalized', None)
    item.pop('tokens', None)
    item.pop('hit_key',None); item.pop('hit_rank',None)
    item['location'] = json.loads(item.get('location') or '{}')
    item['origins'] = _origins(c, item['id'])
    item['duplicate_count'] = max(0, len(item['origins']) - 1)
    text = item['text']
    # NFKC can change offsets; excerpt merely previews, full original is in context.
    pos = min((normalize(text).find(t) for t in terms if normalize(text).find(t) >= 0), default=0)
    start = max(0, pos - 60)
    item['snippet'] = ('…' if start else '') + text[start:start+280] + ('…' if len(text)>start+280 else '')
    return item


def search(root, q='', start='', end='', kind='', source='', conversation='', speaker='', self_only=False,
           phrase=False, page=1, size=30):
    started = time.perf_counter()
    page = max(1, int(page)); size = min(100, max(1, int(size)))
    terms = terms_from_query(q, phrase)
    clauses, values = [], []
    for column, value in [('r.kind',kind), ('r.source_id',source), ('r.conversation',conversation), ('r.speaker',speaker)]:
        if value:
            clauses.append(column+'=?'); values.append(value)
    for v in [start, end]:
        if v and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', v):
            raise ValueError('日期格式应为 YYYY-MM-DD')
    if start:
        clauses.append('r.occurred_at>=?'); values.append(start)
    if end:
        clauses.append('substr(r.occurred_at,1,10)<=?'); values.append(end)
    if self_only:
        clauses.append("r.identity='self' AND r.kind IN ('qq_message','wechat_message')")
    if terms:
        grams = sorted(set(g for t in terms for g in query_grams(t)))
        # Bound FTS expression depth for long literal queries; final instr checks
        # still verify every complete term, so this cannot introduce false hits.
        if len(grams)>96: grams=grams[::(len(grams)+95)//96]
        clauses.append('r.id IN (SELECT rowid FROM archive_fts WHERE archive_fts MATCH ?)')
        values.append(' AND '.join('"'+g+'"' for g in grams))
        for term in terms:
            clauses.append('instr(d.normalized,?)>0'); values.append(term)
    where = ' AND '.join(clauses) or '1'
    with closing(connect(root)) as c:
        join = 'archive_records r JOIN archive_sources s ON s.id=r.source_id JOIN search_docs d ON d.id=r.id'
        # Overlapping document windows are one hit when they point at the same
        # literal occurrence. Distinct chat messages always retain their context.
        if terms:
            hit = "CASE WHEN r.kind IN ('qq_message','wechat_message') THEN 'r:'||r.id "
            hit += "WHEN coalesce(json_extract(r.location,'$.doc_char_start'),json_extract(r.location,'$.char_start')) IS NOT NULL THEN "
            hit += "'p:'||coalesce(json_extract(r.location,'$.page'),0)||':'||(coalesce(json_extract(r.location,'$.doc_char_start'),json_extract(r.location,'$.char_start'))+instr(d.normalized,?)) "
            hit += "ELSE 'r:'||r.id END"
            cte = ('WITH candidates AS (SELECT r.*,s.title source_title,d.normalized,'+hit+' hit_key FROM '+join+' WHERE '+where+'), '
                   'ranked AS (SELECT *,row_number() OVER (PARTITION BY source_id,hit_key ORDER BY length(text),ordinal) hit_rank FROM candidates) ')
            base_values = [terms[0]]+values
            total = c.execute(cte+'SELECT count(*) FROM ranked WHERE hit_rank=1',base_values).fetchone()[0]
            rows = c.execute(cte+'SELECT * FROM ranked WHERE hit_rank=1 ORDER BY CASE WHEN normalized=? THEN 0 ELSE 1 END, '
                             'length(text),occurred_at DESC,source_id,ordinal LIMIT ? OFFSET ?',
                             base_values+[' '.join(terms),size,(page-1)*size]).fetchall()
        else:
            total = c.execute('SELECT count(*) FROM '+join+' WHERE '+where,values).fetchone()[0]
            rows = c.execute('SELECT r.*,s.title source_title FROM '+join+' WHERE '+where+
                             ' ORDER BY r.occurred_at DESC,r.source_id,r.ordinal LIMIT ? OFFSET ?',
                             values+[size,(page-1)*size]).fetchall()
        result = [serialize(c, r, terms) for r in rows]
    return {'results':result,'total':total,'page':page,'size':size,
            'elapsed_ms':round((time.perf_counter()-started)*1000,1),'terms':terms,
            'method':'Unicode 字符/双字符索引 + 原文逐词校验'}


def context(root, record_id, before=5, after=5):
    before=min(100,max(0,int(before)));after=min(100,max(0,int(after)))
    with closing(connect(root)) as c:
        row=c.execute('SELECT r.*,s.title source_title FROM archive_records r JOIN archive_sources s ON s.id=r.source_id WHERE r.id=?',(int(record_id),)).fetchone()
        if row is None:
            raise ValueError('记录不存在，资料可能已更新，请重新搜索')
        rows=c.execute('''SELECT r.*,s.title source_title FROM archive_records r JOIN archive_sources s ON s.id=r.source_id
                WHERE r.source_id=? AND r.ordinal BETWEEN ? AND ? ORDER BY r.ordinal''',
                (row['source_id'],row['ordinal']-before,row['ordinal']+after)).fetchall()
        record=serialize(c,row)
        meta=c.execute('SELECT metadata_json FROM archive_sources WHERE id=?',(row['source_id'],)).fetchone()
        record['source_metadata']=json.loads(meta[0]) if meta else {}
        bounds=c.execute('SELECT min(ordinal),max(ordinal) FROM archive_records WHERE source_id=?',(row['source_id'],)).fetchone()
        return {'record':record,'records':[serialize(c,r) for r in rows], 'before':before,'after':after,
                'has_before': row['ordinal']-before>bounds[0], 'has_after': row['ordinal']+after<bounds[1]}


def filters(root):
    with closing(connect(root)) as c:
        return {'kinds':[r[0] for r in c.execute('SELECT DISTINCT kind FROM archive_records ORDER BY kind')],
                'sources':[dict(r) for r in c.execute('SELECT id,title FROM archive_sources WHERE id IN (SELECT source_id FROM archive_records) ORDER BY title')],
                'conversations':[r[0] for r in c.execute("SELECT DISTINCT conversation FROM archive_records WHERE conversation IS NOT NULL AND conversation!='' ORDER BY conversation")],
                'speakers':[r[0] for r in c.execute("SELECT DISTINCT speaker FROM archive_records WHERE speaker IS NOT NULL AND speaker!='' ORDER BY speaker")]}
