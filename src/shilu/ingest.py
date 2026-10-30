"""Shilu's local, loss-aware archive importer (no network or AI dependency).

Original files remain authoritative. Per-source JSONL and its manifest are a
portable second copy from which the archive tables can be restored.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone, timedelta
import hashlib
from html import unescape
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
import zipfile
from xml.etree import ElementTree

VERSION = "shilu-0.1.1"
from .platform_support import FileLock, default_root
TZ = timezone(timedelta(hours=8))
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
SCHEMA = """
CREATE TABLE IF NOT EXISTS archive_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS archive_files(
 path TEXT PRIMARY KEY,sha256 TEXT NOT NULL,size INTEGER NOT NULL,mtime_ns INTEGER,
 status TEXT NOT NULL,reason TEXT,source_id TEXT,parser_version TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS archive_sources(
 id TEXT PRIMARY KEY,title TEXT NOT NULL,kind TEXT NOT NULL,conversation TEXT,
 content_hash TEXT NOT NULL,metadata_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS archive_records(
 id INTEGER PRIMARY KEY,source_id TEXT NOT NULL,ordinal INTEGER NOT NULL,
 text TEXT NOT NULL,occurred_at TEXT,speaker TEXT NOT NULL,identity TEXT NOT NULL,
 conversation TEXT,kind TEXT NOT NULL,location TEXT NOT NULL,content_hash TEXT NOT NULL,
 UNIQUE(source_id,ordinal));
CREATE TABLE IF NOT EXISTS archive_origins(
 record_id INTEGER NOT NULL,path TEXT NOT NULL,location TEXT NOT NULL,ordinal INTEGER NOT NULL,
 PRIMARY KEY(record_id,path,ordinal));
CREATE INDEX IF NOT EXISTS archive_records_source ON archive_records(source_id,ordinal);
CREATE INDEX IF NOT EXISTS archive_records_date ON archive_records(occurred_at);
CREATE INDEX IF NOT EXISTS archive_records_identity ON archive_records(identity);
CREATE INDEX IF NOT EXISTS archive_origins_path ON archive_origins(path);
"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1048576), b""):
            h.update(block)
    return h.hexdigest()


def _now():
    return datetime.now(TZ).isoformat(timespec="seconds")


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))


def _read_text(path):
    # Same reliable encoding fallback as the existing ingest.read_text helper,
    # isolated here so the importer also runs with macOS system Python 3.9.
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeError:
            pass
    raise ValueError("无法可靠解码文本（UTF-8 / UTF-16 / GB18030）")


def _date(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit() and len(value) >= 10):
        try:
            number = float(value)
            if number > 100000000000:
                number /= 1000
            return datetime.fromtimestamp(number, TZ).isoformat(timespec="seconds")[:19]
        except (ValueError, OSError, OverflowError):
            return None
    value = str(value).strip()
    value = value.replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            d = datetime.strptime(value[:19] if "T" in value else value, fmt)
            return d.strftime("%Y-%m-%d") if fmt == "%Y-%m-%d" else d.strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            pass
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if d.tzinfo:
            d = d.astimezone(TZ)
        return d.strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def _clean(value):
    return value.strip() if isinstance(value, str) else ""


def _record(text, kind, location, path, occurred_at=None, speaker="未知", identity="unknown", conversation=None):
    return {"text": text, "kind": kind, "location": location, "path": path,
            "occurred_at": occurred_at, "speaker": speaker or "未知", "identity": identity,
            "conversation": conversation}


def _spans(text, limit=1600, overlap=1000):
    """Exact overlapping slices preserve phrases up to the search length limit.

    Natural boundaries are only selected near the end so an overlap cannot
    cause tiny advances or an infinite loop. Whitespace outside fragments is
    omitted; whitespace inside each exact original slice is preserved.
    """
    start = 0
    while start < len(text):
        while start < len(text) and text[start].isspace():
            start += 1
        if start >= len(text):
            break
        end = min(start + limit, len(text))
        if end < len(text):
            lower = start + max(limit - 200, overlap + 1)
            split = max(text.rfind("\n", lower, end), text.rfind("。", lower, end))
            if split >= 0:
                end = split + 1
        while end > start and text[end - 1].isspace():
            end -= 1
        yield start, end, text[start:end]
        if end >= len(text) or not text[end:].strip():
            break
        start = max(start + 1, end - overlap)


def scan(root):
    """Hash every regular original, returning metadata only."""
    vault = root / "materials"
    if not vault.is_dir():
        raise FileNotFoundError("资料目录不存在：materials")
    files = {}
    for p in sorted(vault.rglob("*")):
        if p.is_symlink():
            if p.is_file():
                rel = p.relative_to(root).as_posix()
                files[rel] = {"path": rel, "sha256": "", "size": 0, "mtime_ns": 0,
                              "status": "excluded", "reason": "符号链接：避免越过指定原件目录", "source_id": None,
                              "parser_version": VERSION}
            continue
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        try:
            stat = p.stat()
            files[rel] = {"path": rel, "sha256": _sha(p), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                          "status": "pending", "reason": "", "source_id": None, "parser_version": VERSION}
        except OSError as exc:
            files[rel] = {"path": rel, "sha256": "", "size": 0, "mtime_ns": 0,
                          "status": "failed", "reason": "无法读取原件：" + type(exc).__name__,
                          "source_id": None, "parser_version": VERSION}
    return files


def _units(root, files):
    units, used = [], set()
    for rel in files:
        p = Path(rel)
        if files[rel]["status"] == "pending" and p.name == "manifest.json" and "qq message" in p.parts:
            chunks = sorted(q for q in files if Path(q).parent == p.parent / "chunks" and q.endswith(".jsonl"))
            if any(files[q]["status"] == "failed" for q in chunks):
                files[rel].update(status="failed", reason="QQ 分块文件无法读取，不能将不完整会话视为成功")
                continue
            paths = [rel] + chunks
            units.append({"key": rel, "paths": paths, "parser": "qq"})
            used.update(paths)
    for rel, info in files.items():
        if rel in used:
            continue
        p = Path(rel)
        if info["status"] in ("excluded", "failed"):
            continue
        suffix = p.suffix.lower()
        if p.name.startswith("~$") or p.name.startswith(".") or suffix in {".db", ".sqlite", ".sqlite3", ".py", ".pyc", ".js", ".ts", ".command", ".sh"}:
            info.update(status="excluded", reason="系统元数据、编辑锁文件或程序/索引文件，不作个人资料")
        elif suffix in {".txt", ".md", ".markdown", ".docx", ".doc", ".pdf"}:
            units.append({"key": rel, "paths": [rel], "parser": suffix[1:]})
        elif suffix == ".json" and "wechat message" in p.parts:
            units.append({"key": rel, "paths": [rel], "parser": "wechat"})
        else:
            info.update(status="failed", reason="暂不支持或未识别的资料格式：" + (suffix or "无扩展名"))
    for unit in units:
        unit["fingerprint"] = hashlib.sha256(_json([VERSION, unit["parser"], [(p, files[p]["sha256"]) for p in unit["paths"]]]).encode()).hexdigest()
        # Immutable cache names keep the old manifest restorable if publication
        # is interrupted after a new normalized file has been written.
        unit["cache"] = hashlib.sha256(unit["key"].encode()).hexdigest()[:16] + "-" + unit["fingerprint"][:20] + ".jsonl"
    return units


def _parse_qq(root, unit):
    manifest = json.loads((root / unit["key"]).read_text(encoding="utf-8-sig"))
    chat = manifest.get("chatInfo") or {}
    conversation = _clean(chat.get("name")) or "未知会话"
    self_ids = {str(chat[k]) for k in ("selfUin", "selfUid") if chat.get(k) not in (None, "", 0, "0")}
    rows = []
    for rel in unit["paths"][1:]:
        with (root / rel).open(encoding="utf-8-sig") as f:
            for line_no, line in enumerate(f, 1):
                if not line.strip():
                    continue
                m = json.loads(line)
                if not isinstance(m, dict):
                    raise ValueError("QQ 消息行不是对象，行号 " + str(line_no))
                sender = m.get("sender") or {}
                ids = {str(sender[k]) for k in ("uin", "uid") if sender.get(k) not in (None, "", 0, "0")}
                identity = "system" if m.get("system") or m.get("type") == "system" else ("self" if ids & self_ids else ("peer" if ids else "unknown"))
                speaker = next((_clean(sender.get(k)) for k in ("name", "nickname", "remark") if _clean(sender.get(k))), None)
                speaker = speaker or next(iter(sorted(ids)), "系统" if identity == "system" else "未知")
                content = m.get("content") or {}
                text = content.get("text") if isinstance(content, dict) else str(content)
                extraction = "content.text"
                if not isinstance(text, str) or not text.strip():
                    html = content.get("html", "") if isinstance(content, dict) else ""
                    text = unescape(re.sub(r"<[^>]+>", " ", html)).strip() if html else ""
                    extraction = "content.html_text"
                if not text:
                    text = "[非文本消息：" + str(m.get("type") or "未知类型") + "]"
                    extraction = "placeholder"
                location = {"format": "qq_jsonl", "line": line_no, "message": len(rows),
                            "message_id": str(m.get("id") or ""), "message_type": str(m.get("type") or ""),
                            "extraction": extraction}
                rows.append(_record(text, "qq_message", location, rel, _date(m.get("time")) or _date(m.get("timestamp")), speaker, identity, conversation))
    declared = (manifest.get("statistics") or {}).get("totalMessages")
    if declared is not None and int(declared) != len(rows):
        raise ValueError("QQ manifest 消息数与实际行数不一致：声明 %s，实际 %s" % (declared, len(rows)))
    return {"title": conversation, "kind": "qq", "conversation": conversation,
            "metadata": {"declared_messages": declared, "identity_basis": "nonempty selfUin/selfUid exact match", "primary_path": unit["key"]}}, rows


def _parse_wechat(root, unit):
    payload = json.loads((root / unit["key"]).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        raise ValueError("未识别的微信 JSON 结构：缺少 messages 数组")
    session = payload.get("session") or {}
    conversation = next((_clean(session.get(k)) for k in ("displayName", "remark", "nickname", "wxid", "username") if _clean(session.get(k))), "未知会话")
    rows = []
    for ordinal, m in enumerate(payload["messages"]):
        if not isinstance(m, dict):
            raise ValueError("微信 messages[%s] 不是对象" % ordinal)
        sent = m.get("isSend")
        identity = "self" if sent in (1, True, "1") else ("peer" if sent in (0, False, "0") else "unknown")
        if m.get("type") in ("系统消息", "system"):
            identity = "system"
        speaker = _clean(m.get("senderDisplayName")) or _clean(m.get("senderUsername")) or ("本人" if identity == "self" else "未知")
        content = m.get("content")
        extraction = "content"
        if content is None or content == "":
            content = "[非文本消息：" + str(m.get("type") or m.get("localType") or "未知类型") + "]"
            extraction = "placeholder"
        elif not isinstance(content, str):
            content = _json(content)
            extraction = "content_json"
        location = {"format": "wechat_json", "message": ordinal, "json_path": "messages[%s]" % ordinal,
                    "message_id": str(m.get("localId") or ""), "message_type": str(m.get("type") or ""), "extraction": extraction}
        rows.append(_record(content, "wechat_message", location, unit["key"], _date(m.get("formattedTime")) or _date(m.get("createTime")), speaker, identity, conversation))
    return {"title": conversation, "kind": "wechat", "conversation": conversation,
            "metadata": {"declared_messages": session.get("messageCount"), "identity_basis": "explicit isSend", "primary_path": unit["key"]}}, rows


def _docx_paragraphs(path):
    # Retain empty paragraphs so source locations are independently verifiable.
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    result = []
    for paragraph in root.findall(".//{%s}p" % W_NS):
        parts = []
        for node in paragraph.iter():
            if node.tag == "{%s}t" % W_NS and node.text:
                parts.append(node.text)
            elif node.tag == "{%s}tab" % W_NS:
                parts.append("\t")
            elif node.tag in ("{%s}br" % W_NS, "{%s}cr" % W_NS):
                parts.append("\n")
        result.append("".join(parts))
    return result


def _pdfkit_pages(path):
    """Use macOS's built-in PDFKit through JXA, requiring no SDK/compiler."""
    code = """ObjC.import('Foundation'); ObjC.import('PDFKit');
function run(argv) {
 var doc = $.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(argv[0]));
 if (doc.isNil()) { throw new Error('PDFKit cannot open document'); }
 var pages = [];
 for (var i = 0; i < Number(doc.pageCount); i++) {
  var s = doc.pageAtIndex(i).string;
  pages.push(s.isNil() ? '' : ObjC.unwrap(s));
 }
 return JSON.stringify(pages);
}"""
    result = subprocess.run(["osascript", "-l", "JavaScript", "-e", code, str(path)],
                            capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise ValueError("macOS PDFKit 无法打开或解析 PDF（返回码 %s）" % result.returncode)
    return json.loads(result.stdout)


def _pdf_pages(path):
    # PDFKit preserves Chinese line layout substantially better for the real
    # archive's PDF. Its exact extracted character offsets remain in location.
    # A PDFKit failure must still allow the independent local reader to try.
    if sys.platform == "darwin" and shutil.which("osascript"):
        try:
            pages = _pdfkit_pages(path)
            if any(p.strip() for p in pages):
                return pages, "macOS_PDFKit"
        except Exception:
            pass
    if importlib.util.find_spec("pypdf"):
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise ValueError("PDF 已加密，无法用空密码读取")
        return [p.extract_text() or "" for p in reader.pages], "pypdf"
    raise ValueError("没有可用的本地 PDF 文本解析器（pypdf / macOS PDFKit）")


def _parse_document(root, unit):
    rel, parser = unit["key"], unit["parser"]
    path = root / rel
    parts = Path(rel).parts
    kind = "creative_writing" if any(p in ("小说", "诗歌", "散文") for p in parts) else ("note" if any(p in ("日记",) for p in parts) or "便签" in path.name or "自述" in path.name else "article")
    rows, metadata = [], {"primary_path": rel, "authorship": "unknown; never inferred as self", "semantic_claims": "not interpreted"}
    if parser in ("txt", "md", "markdown"):
        text = _read_text(path)
        # Mobile note headers carry explicit dates; filenames are never used to guess dates.
        headers = list(re.finditer(r"(?m)^日期:\s*([^\n]+)\n标题:", text))
        metadata.update(dated_headers=len(headers), dated_headers_recognized=sum(_date(m.group(1)) is not None for m in headers),
                        unmatched_date_headers=max(0, len(re.findall(r"(?m)^日期:", text)) - len(headers)),
                        text_coverage="all non-whitespace text retained, including unmatched headers and blocks")
        if headers:
            blocks = [(m.start(), headers[i + 1].start() if i + 1 < len(headers) else len(text), _date(m.group(1))) for i, m in enumerate(headers)]
            if headers[0].start() and text[:headers[0].start()].strip():
                blocks.insert(0, (0, headers[0].start(), None))
        else:
            blocks = [(0, len(text), None)]
        for block_start, block_end, occurred in blocks:
            for start, end, fragment in _spans(text[block_start:block_end]):
                start, end = start + block_start, end + block_start
                loc = {"format": "text", "char_start": start, "char_end": end,
                       "line_start": text.count("\n", 0, start) + 1, "line_end": text.count("\n", 0, end) + 1,
                       "offset_basis": "decoded text with universal newlines; utf-8-sig/utf-16/gb18030"}
                rows.append(_record(fragment, kind, loc, rel, occurred))
    elif parser == "docx":
        paras = _docx_paragraphs(path)
        text = "\n\n".join(paras)
        starts, cursor = [], 0
        for paragraph in paras:
            starts.append(cursor)
            cursor += len(paragraph) + 2
        for start, end, fragment in _spans(text):
            first = max(0, bisect_right(starts, start) - 1)
            last = max(0, bisect_right(starts, max(start, end - 1)) - 1)
            rows.append(_record(fragment, kind, {"format": "docx", "paragraph_start": first, "paragraph_end": last,
                                                 "doc_char_start": start, "doc_char_end": end,
                                                 "char_start": start - starts[first], "char_end": end - starts[first],
                                                 "paragraph_char_start": start - starts[first],
                                                 "paragraph_char_end": end - starts[last],
                                                 "offset_basis": "char offsets relative to inclusive paragraph range joined by two newlines; doc_char offsets relative to all w:p joined by two newlines",
                                                 "index_basis": "zero-based all document.xml w:p"}, rel))
        metadata["limitations"] = "正文、表格段落已提取；页眉页脚、批注、图片内文字、嵌入对象未提取"
    elif parser == "pdf":
        pages, engine = _pdf_pages(path)
        empty_pages = []
        for page_no, text in enumerate(pages, 1):
            if not text.strip():
                empty_pages.append(page_no)
            for start, end, fragment in _spans(text):
                rows.append(_record(fragment, "pdf" if kind == "article" else kind,
                                    {"format": "pdf", "page": page_no, "char_start": start, "char_end": end, "extraction": engine}, rel))
        metadata.update(extractor=engine, pages=len(pages), empty_pages=empty_pages,
                        limitations="仅提取可复制文本，不执行 OCR；空白/扫描页未检索" if empty_pages else "PDF 文本顺序由解析器决定；未执行 OCR")
    elif parser == "doc":
        if not shutil.which("textutil"):
            raise ValueError("旧 DOC 暂不支持：未安装 macOS textutil")
        result = subprocess.run(["textutil", "-convert", "txt", "-stdout", str(path)], capture_output=True, timeout=120)
        if result.returncode:
            raise ValueError("textutil 无法解析旧 DOC（返回码 %s）" % result.returncode)
        text = result.stdout.decode("utf-8")
        for start, end, fragment in _spans(text):
            rows.append(_record(fragment, kind, {"format": "doc_textutil", "char_start": start, "char_end": end,
                                                "limitation": "本地转换文本偏移，不能直接映射 Word 页码"}, rel))
        metadata["limitations"] = "DOC 转换文本可检索，定位为转换字符范围，无可靠原始页码"
    return {"title": path.stem, "kind": kind if parser != "pdf" or kind != "article" else "pdf", "conversation": None, "metadata": metadata}, rows


def _parse(root, unit):
    if unit["parser"] == "qq":
        source, rows = _parse_qq(root, unit)
    elif unit["parser"] == "wechat":
        source, rows = _parse_wechat(root, unit)
    else:
        source, rows = _parse_document(root, unit)
    if not rows:
        raise ValueError("空解析：没有可检索文本或消息（零消息导出/空文档/扫描 PDF）")
    digest = hashlib.sha256()
    digest.update(_json([source["kind"], source["conversation"]]).encode())
    for ordinal, row in enumerate(rows):
        row["ordinal"] = ordinal
        row["content_hash"] = hashlib.sha256(row["text"].encode()).hexdigest()
        digest.update(_json([row[k] for k in ("text", "occurred_at", "speaker", "identity", "kind")]).encode())
        digest.update(b"\n")
    source["content_hash"] = digest.hexdigest()
    source["id"] = "arc_" + source["content_hash"][:24]
    return source, rows


def _write_cache(path, source, rows):
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(_json({"type": "source", **source}) + "\n")
        for row in rows:
            f.write(_json({"type": "record", **row}) + "\n")
    os.replace(str(tmp), str(path))


def _load_cache(path):
    with path.open(encoding="utf-8") as f:
        source = json.loads(next(f))
        rows = [json.loads(line) for line in f if line.strip()]
    return source, rows


def _insert_unit(connection, source, rows):
    sid = source["id"]
    connection.execute("INSERT OR IGNORE INTO archive_sources VALUES (?,?,?,?,?,?)",
                       (sid, source["title"], source["kind"], source["conversation"], source["content_hash"], _json(source["metadata"])))
    exists = connection.execute("SELECT 1 FROM archive_records WHERE source_id=? LIMIT 1", (sid,)).fetchone()
    if not exists:
        connection.executemany("INSERT INTO archive_records(source_id,ordinal,text,occurred_at,speaker,identity,conversation,kind,location,content_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                               ((sid, r["ordinal"], r["text"], r["occurred_at"], r["speaker"], r["identity"], r["conversation"], r["kind"], _json(r["location"]), r["content_hash"]) for r in rows))
    ids = dict(connection.execute("SELECT ordinal,id FROM archive_records WHERE source_id=?", (sid,)))
    connection.executemany("INSERT OR IGNORE INTO archive_origins VALUES (?,?,?,?)",
                           ((ids[r["ordinal"]], r["path"], _json(r["location"]), r["ordinal"]) for r in rows))


def _counts(connection, files):
    status = Counter(f["status"] for f in files.values())
    return {"files_total": len(files), "bytes_total": sum(f["size"] for f in files.values()),
            "files_imported": status["imported"], "files_duplicate": status["duplicate"],
            "files_excluded": status["excluded"], "files_failed": status["failed"],
            "independent_sources": connection.execute("SELECT COUNT(*) FROM archive_sources").fetchone()[0],
            "message_records": connection.execute("SELECT COUNT(*) FROM archive_records WHERE kind IN ('qq_message','wechat_message')").fetchone()[0],
            "text_fragments": connection.execute("SELECT COUNT(*) FROM archive_records WHERE kind NOT IN ('qq_message','wechat_message')").fetchone()[0],
            "records_total": connection.execute("SELECT COUNT(*) FROM archive_records").fetchone()[0],
            "origin_mappings": connection.execute("SELECT COUNT(*) FROM archive_origins").fetchone()[0]}


def update(root: Path, force=False):
    """Incrementally reconcile all originals; only changed logical units are parsed.

    The exclusive lock prevents overlapping UI/CLI updates.  A generation marker
    detects an interrupted database/manifest publication and forces reconciliation.
    """
    root = Path(root).resolve()
    base = root
    normalized = base / "data/archive_normalized"
    normalized.mkdir(parents=True, exist_ok=True)
    lock_path = normalized / "update.lock"
    lock = FileLock(lock_path)
    try:
        try:
            lock.acquire()
        except BlockingIOError:
            raise RuntimeError("已有资料更新正在运行，请稍后重试")
        return _update_locked(root, force, normalized)
    finally:
        lock.close()


def _update_locked(root, force, normalized):
    base = root
    db = base / "data/index.sqlite3"
    db.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = normalized / "manifest.json"
    old = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {}
    files = scan(root)
    units = _units(root, files)
    old_units = {u["key"]: u for u in old.get("units", [])}
    generation = uuid.uuid4().hex
    parsed_count = unchanged_count = 0
    with closing(sqlite3.connect(str(db), timeout=60)) as c:
        c.executescript(SCHEMA)
        current = c.execute("SELECT value FROM archive_meta WHERE key='generation'").fetchone()
        if old.get("version") != VERSION or not current or current[0] != old.get("generation"):
            force = True
        live_keys = {u["key"] for u in units}
        changed = []
        for unit in units:
            prior = old_units.get(unit["key"])
            if not force and prior and prior.get("fingerprint") == unit["fingerprint"] and (prior.get("status") == "failed" or (normalized / prior["cache"]).exists()):
                unit.update({k: prior[k] for k in ("status", "reason", "source_id", "record_count") if k in prior})
                unchanged_count += 1
            else:
                parsed_count += 1
                try:
                    source, rows = _parse(root, unit)
                    _write_cache(normalized / unit["cache"], source, rows)
                    unit.update(status="imported", reason="", source_id=source["id"], record_count=len(rows))
                    changed.append((unit, source, rows))
                except Exception as exc:
                    # Exception text never includes message contents or full parser tracebacks.
                    reason = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else type(exc).__name__
                    unit.update(status="failed", reason=reason[:240], source_id=None, record_count=0)
                    changed.append((unit, None, None))
        c.execute("BEGIN IMMEDIATE")
        if force:
            c.execute("DELETE FROM archive_origins")
            c.execute("DELETE FROM archive_records")
            c.execute("DELETE FROM archive_sources")
        else:
            changed_keys = {u[0]["key"] for u in changed}
            touched_sources = {u.get("source_id") for key, u in old_units.items() if key not in live_keys or key in changed_keys}
            removed_paths = {p for key, u in old_units.items() if key not in live_keys or key in changed_keys for p in u["paths"]}
            c.executemany("DELETE FROM archive_origins WHERE path=?", ((p,) for p in removed_paths))
            c.execute("DELETE FROM archive_records WHERE NOT EXISTS (SELECT 1 FROM archive_origins WHERE archive_origins.record_id=archive_records.id)")
            c.execute("DELETE FROM archive_sources WHERE NOT EXISTS (SELECT 1 FROM archive_records WHERE archive_records.source_id=archive_sources.id)")
        seen_sources = set()
        for unit in units:
            if unit["status"] == "failed":
                for path in unit["paths"]:
                    files[path].update(status="failed", reason=unit["reason"], source_id=None)
                continue
            duplicate = unit["source_id"] in seen_sources
            seen_sources.add(unit["source_id"])
            for path in unit["paths"]:
                files[path].update(status="duplicate" if duplicate else "imported", reason="完整有序内容与另一来源一致，保留原始映射" if duplicate else "", source_id=unit["source_id"])
        if force:
            for unit in units:
                if unit["status"] != "failed":
                    source, rows = _load_cache(normalized / unit["cache"])
                    _insert_unit(c, source, rows)
        else:
            for unit, source, rows in changed:
                if source:
                    _insert_unit(c, source, rows)
                    touched_sources.add(source["id"])
            # A duplicate's primary export can disappear while its other origins
            # survive. Canonical locations must then follow a surviving origin.
            for sid in touched_sources - {None}:
                c.execute("""UPDATE archive_records SET location=(SELECT o.location FROM archive_origins o
                             WHERE o.record_id=archive_records.id ORDER BY o.path,o.ordinal LIMIT 1)
                             WHERE source_id=?""", (sid,))
        canonical = set()
        for unit in units:
            if unit["status"] == "failed" or unit["source_id"] in canonical:
                continue
            canonical.add(unit["source_id"])
            with (normalized / unit["cache"]).open(encoding="utf-8") as handle:
                source = json.loads(next(handle))
            c.execute("UPDATE archive_sources SET title=?,kind=?,conversation=?,metadata_json=? WHERE id=?",
                      (source["title"], source["kind"], source["conversation"], _json(source["metadata"]), source["id"]))
        c.execute("DELETE FROM archive_files")
        c.executemany("INSERT INTO archive_files VALUES (:path,:sha256,:size,:mtime_ns,:status,:reason,:source_id,:parser_version)", files.values())
        c.execute("INSERT OR REPLACE INTO archive_meta VALUES ('generation',?)", (generation,))
        c.execute("INSERT OR REPLACE INTO archive_meta VALUES ('parser_version',?)", (VERSION,))
        c.execute("INSERT OR REPLACE INTO archive_meta VALUES ('updated_at',?)", (_now(),))
        counts = _counts(c, files)
        c.commit()
    manifest = {"version": VERSION, "generation": generation, "updated_at": _now(), "path_basis": "project relative", "files": list(files.values()), "units": units, "counts": counts}
    _atomic_json(manifest_path, manifest)
    report = {"updated_at": manifest["updated_at"], **counts, "units_parsed": parsed_count, "units_unchanged": unchanged_count,
              "extensions": dict(Counter(Path(p).suffix.lower() or "[none]" for p in files)),
              "directories": dict(Counter(Path(p).parts[1] if len(Path(p).parts) > 2 else "[root]" for p in files)),
              "failed": [{"path": p, "reason": f["reason"]} for p, f in files.items() if f["status"] == "failed"],
              "excluded": [{"path": p, "reason": f["reason"]} for p, f in files.items() if f["status"] == "excluded"],
              "duplicate_files": [{"path": p, "source_id": f["source_id"], "reason": f["reason"]} for p, f in files.items() if f["status"] == "duplicate"],
              "limitations": ["微信/QQ 的媒体本体、图片内文字、语音内容不转录；原导出文字及空消息占位保留", "不从文件名推断日期，不把创作、文章或所有聊天默认视为本人发言", "来源仅按整份有序内容相同合并；重叠导出的部分重复可能仍保留", "DOCX 页眉页脚、批注、图片和嵌入对象未提取；PDF 不执行 OCR"]}
    by_hash = {}
    for path, info in files.items():
        if info["sha256"]:
            by_hash.setdefault(info["sha256"], []).append(path)
    report["identical_file_groups"] = [{"sha256": digest, "paths": paths} for digest, paths in by_hash.items() if len(paths) > 1]
    reports = base / "reports"
    _atomic_json(reports / "import-report.json", report)
    _atomic_json(reports / "original-hashes-current.json", {"generated_at": _now(), "files": [{k: f[k] for k in ("path", "sha256", "size")} for f in files.values()]})
    baseline = reports / "original-hashes-baseline.json"
    if not baseline.exists():
        _atomic_json(baseline, {"generated_at": _now(), "files": [{k: f[k] for k in ("path", "sha256", "size")} for f in files.values()]})
    try:
        from .search import sync_index
        report["search_index"] = sync_index(root)
    except ImportError:
        report["search_index"] = "pending: search module unavailable"
    # Only generated cache names are pruned, after successful publication. The
    # active manifest and all of its JSONL files remain a complete second copy.
    active = {u["cache"] for u in units if u["status"] != "failed"}
    removed_caches = 0
    for cached in normalized.glob("*.jsonl"):
        if cached.name not in active and re.fullmatch(r"(?:[0-9a-f]{24}|[0-9a-f]{16}-[0-9a-f]{20})\.jsonl", cached.name):
            cached.unlink()
            removed_caches += 1
    report["obsolete_normalized_files_removed"] = removed_caches
    _atomic_json(reports / "import-report.json", report)
    return report


def restore_normalized(root: Path):
    """Restore archive-only tables from JSONL, without parsing source documents."""
    root = Path(root).resolve()
    base = root
    normalized = base / "data/archive_normalized"
    manifest = json.loads((normalized / "manifest.json").read_text(encoding='utf-8'))
    db = base / "data/index.sqlite3"
    db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(db), timeout=60)) as c:
        c.executescript(SCHEMA)
        c.execute("BEGIN IMMEDIATE")
        c.execute("DELETE FROM archive_origins")
        c.execute("DELETE FROM archive_records")
        c.execute("DELETE FROM archive_sources")
        c.execute("DELETE FROM archive_files")
        for unit in manifest["units"]:
            if unit["status"] != "failed":
                source, rows = _load_cache(normalized / unit["cache"])
                _insert_unit(c, source, rows)
        c.executemany("INSERT INTO archive_files VALUES (:path,:sha256,:size,:mtime_ns,:status,:reason,:source_id,:parser_version)", manifest["files"])
        c.execute("INSERT OR REPLACE INTO archive_meta VALUES ('generation',?)", (manifest["generation"],))
        c.execute("INSERT OR REPLACE INTO archive_meta VALUES ('parser_version',?)", (VERSION,))
        counts = _counts(c, {f["path"]: f for f in manifest["files"]})
        c.commit()
    from .search import sync_index
    sync_index(root)
    return counts


def main():
    parser = argparse.ArgumentParser(description="Shilu 本地资料更新")
    parser.add_argument("--root", type=Path, default=default_root())
    parser.add_argument("--force", action="store_true", help="从原件重新解析全部资料")
    parser.add_argument("--restore-normalized", action="store_true", help="用标准化 JSONL 重建检索索引")
    args = parser.parse_args()
    result = restore_normalized(args.root) if args.restore_normalized else update(args.root, args.force)
    # Safe summary only: no private messages or document excerpts in stdout.
    print(json.dumps({k: v for k, v in result.items() if k not in ("failed", "excluded", "duplicate_files")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
