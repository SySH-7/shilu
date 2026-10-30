'use strict';

const $ = (id) => document.getElementById(id);
const state = { page: 1, size: 30, total: 0, searchToken: 0, searched: false, busy: false, updateRunning: false, poll: null, contextId: null, before: 5, after: 5, contextToken: 0 };
const fileStatusNames = { imported: '成功导入', duplicate: '重复文件', excluded: '已排除', failed: '解析失败 / 暂不支持', removed: '已移除' };
const kindNames = { qq_message: 'QQ 聊天', wechat_message: '微信聊天', note: '个人笔记', article: '文章', creative: '创作文本', creative_text: '创作文本', creative_writing: '创作文本', fiction: '小说 / 创作', poem: '诗歌', document: '文档', docx: 'Word 文档', doc: '旧版 Word', pdf: 'PDF 文档', text: '文本资料', markdown: 'Markdown', unknown: '类型未知' };
const number = (v) => Number(v || 0).toLocaleString('zh-CN');
const valueText = (v, fallback = '未知') => v === undefined || v === null || v === '' ? fallback : (typeof v === 'object' ? JSON.stringify(v) : String(v));

function node(tag, className, text) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== undefined) result.textContent = text;
  return result;
}

async function api(path, options = {}) {
  if (options.method && options.method !== 'GET') options.headers = { 'X-拾录': '1', 'Content-Type': 'application/json', ...(options.headers || {}) };
  const response = await fetch(path, { cache: 'no-store', ...options });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.error) throw new Error(typeof data.error === 'string' ? data.error : `请求失败（${response.status}）`);
  return data;
}

function notice(message, error = false) {
  $('notice').textContent = message;
  $('notice').hidden = !message;
  $('notice').classList.toggle('error-state', error);
}

function params() {
  const p = new URLSearchParams();
  for (const id of ['query', 'start', 'end', 'kind', 'source', 'conversation', 'speaker']) {
    if ($(id).value.trim()) p.set(id === 'query' ? 'q' : id, $(id).value.trim());
  }
  if ($('phrase').checked) p.set('phrase', '1');
  if (document.querySelector('input[name="scope"]:checked').value === 'self') p.set('self', '1');
  p.set('page', String(state.page));
  p.set('size', String(state.size));
  return p;
}

function highlights() {
  const q = $('query').value.trim();
  if (!q) return [];
  if ($('phrase').checked) return [q.replace(/^"|"$/g, '')];
  return (q.match(/"([^"]+)"|[^\s"]+/g) || []).map((v) => v.replace(/^"|"$/g, '')).filter(Boolean).sort((a, b) => b.length - a.length);
}

function highlightedText(element, text) {
  text = valueText(text, '');
  const terms = highlights();
  if (!terms.length) { element.textContent = text; return; }
  const escaped = terms.map((v) => v.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  const pattern = new RegExp(`(${escaped.join('|')})`, 'gi');
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    element.append(document.createTextNode(text.slice(last, match.index)));
    element.append(node('mark', '', match[0]));
    last = match.index + match[0].length;
  }
  element.append(document.createTextNode(text.slice(last)));
}

function dateText(record) { return valueText(record.occurred_at, '时间未知').replace('T', ' '); }
function isSelf(record) { return ['self', '本人', 'me'].includes(record.identity); }
function origins(record) { return Array.isArray(record.origins) ? record.origins : []; }
function locationText(origin) {
  const location = origin && origin.location;
  if (!location || typeof location !== 'object') return valueText(location, '原始位置未知');
  const labels = { line: '行', line_start: '起始行', line_end: '结束行', start_line: '起始行', end_line: '结束行', page: '页', page_start: '起始页', page_end: '结束页', message: '消息序号', message_index: '消息序号', message_no: '消息序号', paragraph: '段落', paragraph_index: '段落', paragraph_start: '起始段落', paragraph_end: '结束段落', char_start: '起始字符', char_end: '结束字符', byte_start: '起始字节', byte_end: '结束字节', offset: '字符位置', index: '序号', ordinal: '记录序号', encoding: '编码', sheet: '工作表', row: '行', section: '章节', format: '原件格式', json_path: '消息位置', extraction: '文本提取方式', text_extraction: '文本提取方式', time_raw: '原始时间', time_field: '时间字段' };
  const formats = { qq_jsonl: 'QQ 消息导出', wechat_json: '微信消息导出', text: '文本', docx: 'Word', doc_textutil: '旧版 Word 转文本', pdf: 'PDF' };
  return Object.entries(location).filter(([, v]) => v !== null && v !== '').map(([key, value]) => `${labels[key] || key}：${key === 'format' ? (formats[value] || valueText(value, '')) : valueText(value, '')}`).join(' · ') || '原始位置未知';
}

function recordHeader(record) {
  const header = node('div', 'record-header');
  header.append(node('span', 'kind-tag', kindNames[record.kind] || valueText(record.kind, '类型未知')));
  header.append(node('time', '', dateText(record)));
  header.append(node('span', 'record-speaker', valueText(record.speaker, '说话人未知')));
  if (isSelf(record)) header.append(node('span', 'identity-tag', '本人'));
  if (record.conversation) header.append(node('span', 'conversation', '会话：' + record.conversation));
  return header;
}

function renderResult(record) {
  const card = node('article', 'result-card');
  card.append(recordHeader(record));
  const body = node('div', 'result-text');
  highlightedText(body, record.snippet || record.text);
  card.append(body);
  const footer = node('div', 'record-footer');
  const source = node('div', 'record-source');
  const title = node('span', 'source-title', valueText(record.source_title, '来源未知'));
  const count = origins(record).length || Number(record.duplicate_count || 0) + 1;
  if (count > 1) title.append(node('span', 'duplicate-note', `保留 ${count} 处出处`));
  source.append(title);
  const firstOrigin = origins(record)[0];
  source.append(node('span', '', firstOrigin ? `${firstOrigin.path} · ${locationText(firstOrigin)}` : '原始位置未知'));
  footer.append(source);
  const actions = node('div', 'record-actions');
  const open = node('button', '', '查看原文与上下文');
  open.type = 'button';
  open.addEventListener('click', () => openContext(record.id));
  actions.append(open);
  footer.append(actions);
  card.append(footer);
  return card;
}

async function search(resetPage = true) {
  if ($('start').value && $('end').value && $('start').value > $('end').value) { notice('开始日期不能晚于结束日期。', true); return; }
  if (resetPage) state.page = 1;
  const token = ++state.searchToken;
  state.searched = true;
  state.busy = true;
  $('search-btn').disabled = true;
  $('results').setAttribute('aria-busy', 'true');
  $('result-summary').textContent = '正在检索…';
  notice('');
  try {
    const data = await api('/api/search?' + params());
    if (token !== state.searchToken) return;
    state.total = Number(data.total || 0);
    state.page = Number(data.page || state.page);
    state.size = Number(data.size || state.size);
    $('results').replaceChildren();
    const records = data.results || [];
    if (!records.length) $('results').append(node('div', 'empty-state', '未找到匹配记录。可减少关键词、放宽日期或切换为“全部内容”后重试。'));
    else for (const record of records) $('results').append(renderResult(record));
    $('result-summary').textContent = `共 ${number(state.total)} 条记录${data.elapsed_ms !== undefined ? ` · ${Number(data.elapsed_ms).toFixed(0)} 毫秒` : ''}`;
    renderPages();
  } catch (error) {
    if (token !== state.searchToken) return;
    $('result-summary').textContent = '检索未完成';
    notice(error.message, true);
  } finally {
    if (token === state.searchToken) {
      state.busy = false;
      $('search-btn').disabled = false;
      $('results').setAttribute('aria-busy', 'false');
    }
  }
}

function renderPages() {
  const pages = Math.max(1, Math.ceil(state.total / state.size));
  $('pagination').hidden = state.total <= state.size;
  $('prev-btn').disabled = state.page <= 1;
  $('next-btn').disabled = state.page >= pages;
  $('page-label').textContent = `第 ${state.page} / ${pages} 页`;
}

function setOptions(id, values, initial) {
  const select = $(id);
  const current = select.value;
  select.replaceChildren(new Option(initial, ''));
  for (const value of values || []) {
    const key = typeof value === 'object' ? value.id : value;
    const label = typeof value === 'object' ? (value.title || value.name || value.id) : (id === 'kind' ? (kindNames[value] || value) : value);
    select.add(new Option(valueText(label), valueText(key, '')));
  }
  if ([...select.options].some((o) => o.value === current)) select.value = current;
}

async function loadFilters() {
  try {
    const filters = await api('/api/filters');
    setOptions('kind', filters.kinds, '全部类型');
    setOptions('source', filters.sources, '全部来源');
    setOptions('conversation', filters.conversations, '全部会话');
    setOptions('speaker', filters.speakers, '全部说话人');
  } catch (error) { notice('筛选条件读取失败：' + error.message, true); }
}

async function loadStatus() {
  try {
    const data = await api('/api/status');
    const c = data.counts || {};
    const counts = $('library-counts');
    counts.replaceChildren();
    counts.append(document.createTextNode(`资料文件 ${number(c.files)} · 独立来源 ${number(c.sources)} · `));
    counts.append(node('strong', '', `聊天消息 ${number(c.messages)}`));
    counts.append(document.createTextNode(` · 文本片段 ${number(c.fragments)}`));
    if (c.failed) {
      counts.append(document.createTextNode(' · '));
      const failures = node('button', 'status-link', `${number(c.failed)} 个文件待处理`);
      failures.type = 'button';
      failures.addEventListener('click', () => openFiles('failed'));
      counts.append(failures);
    }
    const update = data.update || {};
    const wasRunning = state.updateRunning;
    state.updateRunning = Boolean(update.running);
    $('update-btn').disabled = state.updateRunning;
    $('update-btn').textContent = state.updateRunning ? '正在更新…' : '更新资料';
    $('update-state').className = update.error ? 'status-error' : (state.updateRunning ? 'status-busy' : '');
    if (update.error) $('update-state').textContent = '上次更新未完成：' + valueText(update.error);
    else if (state.updateRunning) $('update-state').textContent = '正在扫描并导入资料，请稍候…';
    else if (update.finished_at || update.last_finished || data.last_update) $('update-state').textContent = '最近更新：' + valueText(update.finished_at || update.last_finished || data.last_update).replace('T', ' ');
    else $('update-state').textContent = '资料库已就绪';
    if (state.updateRunning && !state.poll) state.poll = window.setInterval(loadStatus, 1600);
    if (!state.updateRunning && state.poll) { window.clearInterval(state.poll); state.poll = null; }
    if (wasRunning && !state.updateRunning) {
      await loadFilters();
      if (state.searched) await search(true);
      if (!update.error) notice('资料更新完成。未变化文件会复用已有解析结果；可在“导入明细”查看本次状态。');
    }
  } catch (error) { $('update-state').textContent = '无法连接本地服务：' + error.message; $('update-state').className = 'status-error'; }
}

async function updateLibrary() {
  $('update-btn').disabled = true;
  notice('');
  try {
    await api('/api/update', { method: 'POST' });
    state.updateRunning = true;
    await loadStatus();
  } catch (error) { notice('启动更新失败：' + error.message, true); $('update-btn').disabled = false; }
}

async function copyText(text, button) {
  try {
    await navigator.clipboard.writeText(text);
    const old = button.textContent;
    button.textContent = '已复制';
    window.setTimeout(() => { button.textContent = old; }, 1500);
  } catch (_) { window.prompt('复制下方路径：', text); }
}

function renderOrigins(record) {
  const panel = $('origin-panel');
  panel.replaceChildren(node('h3', '', '原始文件与位置'));
  const list = origins(record);
  if (!list.length) { panel.append(node('p', '', '该记录的原始文件映射缺失，无法准确定位。')); return; }
  list.forEach((origin, index) => {
    const row = node('div', 'origin-row');
    const path = node('div', 'origin-path', valueText(origin.path));
    path.append(node('span', 'origin-location', locationText(origin)));
    row.append(path);
    const actions = node('div', 'origin-actions');
    const link = node('a', '', '打开 / 下载原件');
    link.href = '/api/original?' + new URLSearchParams({ record_id: record.id, origin: index });
    link.target = '_blank';
    link.rel = 'noopener';
    const reveal = node('button', '', '定位原件');
    reveal.type = 'button';
    reveal.addEventListener('click', async () => {
      reveal.disabled = true;
      try { await api('/api/reveal', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ record_id: record.id, origin: index }) }); }
      catch (error) { window.alert('无法定位原件：' + error.message); }
      finally { reveal.disabled = false; }
    });
    const copy = node('button', '', '复制路径');
    copy.type = 'button';
    copy.addEventListener('click', () => copyText(valueText(origin.path, ''), copy));
    actions.append(link, reveal, copy);
    row.append(actions);
    panel.append(row);
  });
}

async function openContext(id, expanded = false) {
  if (!expanded) { state.contextId = id; state.before = 5; state.after = 5; }
  const token = ++state.contextToken;
  const dialog = $('context-dialog');
  if (!dialog.open) dialog.showModal();
  $('more-before').disabled = true;
  $('more-after').disabled = true;
  $('context-range').textContent = '正在读取上下文…';
  if (!expanded) {
    $('context-meta').textContent = '';
    $('origin-panel').replaceChildren();
    $('context-records').replaceChildren(node('p', '', '正在读取原文…'));
  }
  try {
    const data = await api('/api/context?' + new URLSearchParams({ id: state.contextId, before: state.before, after: state.after }));
    if (token !== state.contextToken) return;
    const record = data.record;
    if (!record) throw new Error('记录不存在，资料库可能已更新。请重新检索。');
    $('context-meta').textContent = [record.source_title, record.conversation ? `会话：${record.conversation}` : '', `命中记录：${dateText(record)}`].filter(Boolean).join(' · ');
    const metadata = record.source_metadata || data.source_metadata || {};
    const limitations = [metadata.limitations, metadata.limitation].flatMap((item) => Array.isArray(item) ? item : [item]).filter((item) => typeof item === 'string' && item.trim());
    if (limitations.length) $('context-meta').append(document.createElement('br'), document.createTextNode('解析说明：' + [...new Set(limitations)].join('；')));
    renderOrigins(record);
    const records = data.records || [record];
    $('context-records').replaceChildren();
    for (const item of records) {
      const selected = String(item.id) === String(record.id);
      const article = node('article', 'context-record' + (selected ? ' selected' : ''));
      const header = recordHeader(item);
      if (selected) header.append(node('span', 'selected-label', '当前命中'));
      article.append(header);
      const content = node('div', 'context-text');
      highlightedText(content, item.text);
      article.append(content);
      const first = origins(item)[0];
      article.append(node('div', 'context-location', first ? `${first.path} · ${locationText(first)}` : '原始位置未知'));
      $('context-records').append(article);
    }
    $('context-range').textContent = `按同一来源的原文顺序显示 ${records.length} 条记录；绿色边线为命中位置。`;
    $('more-before').disabled = state.before >= 100 || data.has_before === false;
    $('more-after').disabled = state.after >= 100 || data.has_after === false;
    if (!expanded) window.requestAnimationFrame(() => { const selected = $('context-records').querySelector('.selected'); if (selected) selected.scrollIntoView({ block: 'nearest' }); });
  } catch (error) {
    if (token !== state.contextToken) return;
    $('context-records').replaceChildren(node('p', 'error-state', '读取失败：' + error.message));
    $('context-range').textContent = '';
  }
}

function openFiles(status = 'failed') {
  $('file-status').value = status;
  if (!$('files-dialog').open) $('files-dialog').showModal();
  loadFiles();
}

async function loadFiles() {
  $('file-list').replaceChildren();
  $('files-summary').textContent = '正在读取文件清单…';
  try {
    const data = await api('/api/files?' + new URLSearchParams({ status: $('file-status').value }));
    const files = data.files || [];
    $('files-summary').textContent = `共 ${number(data.total === undefined ? files.length : data.total)} 个文件${data.total > files.length ? `；当前显示 ${number(files.length)} 个` : ''}。`;
    if (!files.length) $('file-list').append(node('div', 'empty-state', '该状态下没有文件。'));
    for (const file of files) {
      const entry = node('article', 'file-entry');
      const header = node('div', 'file-entry-header');
      header.append(node('span', 'file-entry-path', valueText(file.path)));
      header.append(node('span', 'file-status ' + valueText(file.status, ''), fileStatusNames[file.status] || valueText(file.status)));
      entry.append(header);
      if (file.reason) entry.append(node('p', 'file-entry-reason', valueText(file.reason)));
      if (file.records !== undefined || file.record_count !== undefined) entry.append(node('p', 'file-entry-reason', `记录数：${number(file.record_count === undefined ? file.records : file.record_count)}`));
      $('file-list').append(entry);
    }
  } catch (error) { $('files-summary').textContent = '清单读取失败：' + error.message; }
}

$('search-form').addEventListener('submit', (event) => { event.preventDefault(); search(true); });
$('reset-btn').addEventListener('click', () => { $('search-form').reset(); notice(''); $('query').focus(); if (state.searched) search(true); });
$('prev-btn').addEventListener('click', () => { if (state.busy || state.page <= 1) return; state.page -= 1; search(false); window.scrollTo({ top: 0 }); });
$('next-btn').addEventListener('click', () => { if (state.busy) return; state.page += 1; search(false); window.scrollTo({ top: 0 }); });
$('update-btn').addEventListener('click', updateLibrary);
$('files-btn').addEventListener('click', () => openFiles('failed'));
$('file-status').addEventListener('change', loadFiles);
$('more-before').addEventListener('click', () => { state.before = Math.min(100, state.before + 5); openContext(state.contextId, true); });
$('more-after').addEventListener('click', () => { state.after = Math.min(100, state.after + 5); openContext(state.contextId, true); });
for (const button of document.querySelectorAll('.close-dialog')) button.addEventListener('click', () => button.closest('dialog').close());
for (const dialog of document.querySelectorAll('dialog')) dialog.addEventListener('click', (event) => { if (event.target === dialog) { const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close(); } });
$('quit-btn').addEventListener('click', async () => {
  $('quit-btn').disabled = true;
  try {
    await api('/api/shutdown', { method: 'POST' });
    if (state.poll) window.clearInterval(state.poll);
    document.querySelectorAll('button,input,select').forEach((element) => { element.disabled = true; });
    notice('本地服务已关闭，可以关闭此网页。下次双击启动文件即可继续使用。');
    $('update-state').textContent = '服务已关闭';
  } catch (error) { notice('关闭请求失败：' + error.message, true); $('quit-btn').disabled = false; }
});

loadFilters();
loadStatus();
