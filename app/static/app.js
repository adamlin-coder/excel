const $ = id => document.getElementById(id);
let datasets = [], selected = null, page = null, offset = 0, sort = null, direction = 'asc', editing = null, undo = null, loadId = 0, timer = null;
const status = text => $('status').textContent = text;
async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (response.status === 401 && path !== '/api/login') showLogin();
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === 'string' ? body.detail : '操作失敗，請檢查輸入或稍後再試');
  }
  return response.json();
}
function showLogin() { clearTimeout(timer); $('workspace').hidden = true; $('login-view').hidden = false; }
async function enter(username) { $('login-view').hidden = true; $('workspace').hidden = false; $('username').textContent = username; await refresh(); }
function node(tag, text, cls) { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (cls) el.className = cls; return el; }
async function refresh() {
  clearTimeout(timer);
  datasets = await api('/api/datasets');
  $('dataset-list').replaceChildren();
  for (const d of datasets) {
    const b = node('button', d.name, 'dataset-button' + (selected?.id === d.id ? ' active' : ''));
    b.append(node('small', d.state === 'ready' ? `${d.row_count.toLocaleString()} 列 · ${(d.bytes / 1048576).toFixed(1)} MB` : d.state === 'failed' ? '匯入失敗' : `匯入中 · ${d.row_count.toLocaleString()} 列`));
    b.onclick = () => selectDataset(d);
    $('dataset-list').append(b);
  }
  if (selected) {
    selected = datasets.find(d => d.id === selected.id) || null;
    if (selected && selected.state !== 'ready') renderSelected();
    else if (selected && !page) await selectDataset(selected);
  }
  if (datasets.some(d => d.state === 'importing')) timer = setTimeout(() => refresh().catch(e => status(e.message)), 2000);
}
function renderSelected() {
  const d = selected;
  $('dataset-name').textContent = d.name;
  $('breadcrumb').textContent = d.name;
  $('dataset-meta').textContent = `${(d.bytes / 1048576).toFixed(1)} MB · ${d.row_count.toLocaleString()} 列 · 由 ${d.created_by} 匯入`;
  $('empty').hidden = true;
  const isReady = d.state === 'ready';
  $('data-view').hidden = !isReady;
  $('export').hidden = $('history').hidden = !isReady;
  status(isReady ? '' : d.state === 'failed' ? `匯入失敗：${d.error}。請確認編碼與分隔符號後重新上傳。` : '正在分批匯入，完成後會自動顯示資料。');
}
async function selectDataset(d) {
  ++loadId; selected = d; page = null; offset = 0; sort = null; undo = null;
  $('filters').reset();
  $('filter-column').replaceChildren(node('option', '所有欄位'));
  $('filter-column').firstChild.value = '';
  d.columns.forEach((name, i) => { const opt = node('option', name || `欄位 ${i + 1}`); opt.value = i; $('filter-column').append(opt); });
  renderSelected();
  for (const b of $('dataset-list').children) b.classList.toggle('active', b.firstChild?.textContent === d.name);
  if (d.state === 'ready') await loadRows();
}
function query() {
  const p = new URLSearchParams({ q: $('search').value, direction });
  if ($('filter-column').value !== '') { p.set('column', $('filter-column').value); p.set('op', $('filter-op').value); p.set('value', $('filter-value').value); }
  if (sort !== null) p.set('sort', sort);
  return p;
}
async function loadRows() {
  const id = ++loadId;
  const p = query(); p.set('offset', offset); p.set('limit', 100);
  status('正在查詢資料…');
  try {
    const data = await api(`/api/datasets/${selected.id}/rows?${p}`);
    if (id !== loadId) return;
    page = data;
    $('table-head').replaceChildren(); $('table-body').replaceChildren();
    const tr = node('tr'); tr.append(node('th', '#'));
    data.columns.forEach((name, i) => { const th = node('th'); const b = node('button', `${name || `欄位 ${i + 1}`} ${sort === i ? direction === 'asc' ? '↑' : '↓' : '↕'}`); b.onclick = () => { direction = sort === i && direction === 'asc' ? 'desc' : 'asc'; sort = i; offset = 0; loadRows(); }; th.append(b); tr.append(th); });
    $('table-head').append(tr);
    for (const row of data.rows) {
      const tr = node('tr'); tr.append(node('td', row.id));
      row.values.forEach((value, column) => { const td = node('td'); const b = node('button', value || '\u00a0'); b.title = value; b.setAttribute('aria-label', `編輯第 ${row.id} 列，${data.columns[column]}`); b.onclick = () => openEdit(row, column); td.append(b); tr.append(td); });
      $('table-body').append(tr);
    }
    if (!data.rows.length) { const tr = node('tr'); const td = node('td', '沒有符合條件的資料'); td.colSpan = data.columns.length + 1; tr.append(td); $('table-body').append(tr); }
    $('result-count').textContent = `${data.total.toLocaleString()} 筆符合條件`;
    $('page-info').textContent = data.total ? `顯示 ${(offset + 1).toLocaleString()}–${(offset + data.rows.length).toLocaleString()} 筆 / 共 ${data.total.toLocaleString()} 筆` : '0 筆資料';
    $('prev').disabled = offset === 0; $('next').disabled = offset + 100 >= data.total;
    status('');
  } catch (e) { if (id === loadId) status(e.message); }
}
function openEdit(row, column) {
  editing = { dataset: selected.id, row, column };
  $('edit-label').textContent = `第 ${row.id} 列 / ${page.columns[column]}`;
  $('edit-value').value = row.values[column]; $('edit-error').textContent = '';
  $('undo').disabled = !undo || undo.dataset !== selected.id;
  $('edit-dialog').showModal(); $('edit-value').focus();
}
$('login').onsubmit = async e => {
  e.preventDefault(); $('login-error').textContent = '';
  try { const data = await api('/api/login', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(Object.fromEntries(new FormData(e.target)))}); e.target.reset(); await enter(data.username); } catch (e) { $('login-error').textContent = e.message; }
};
$('logout').onclick = async () => { try { await api('/api/logout', {method: 'POST'}); showLogin(); } catch (e) { status(e.message); } };
for (const id of ['new-upload', 'empty-upload']) $(id).onclick = () => $('upload-dialog').showModal();
for (const b of document.querySelectorAll('[data-close]')) b.onclick = () => $(b.dataset.close).close();
$('upload-form').onsubmit = e => {
  e.preventDefault(); const file = $('file').files[0]; if (!file) return;
  $('upload-submit').disabled = true; $('upload-progress').hidden = false; $('upload-progress').value = 0;
  $('upload-status').textContent = '正在上傳，請保持此頁面開啟…';
  const p = new URLSearchParams({encoding: $('encoding').value, delimiter: $('delimiter').value === 'tab' ? '\t' : $('delimiter').value});
  const xhr = new XMLHttpRequest(); xhr.open('POST', `/api/datasets?${p}`);
  xhr.upload.onprogress = e => { if (e.lengthComputable) $('upload-progress').value = e.loaded / e.total * 100; };
  xhr.onload = async () => {
    $('upload-submit').disabled = false;
    let data; try { data = JSON.parse(xhr.responseText); } catch { $('upload-status').textContent = '伺服器未接受上傳，請檢查檔案大小及連線。'; return; }
    if (xhr.status !== 202) { $('upload-status').textContent = typeof data.detail === 'string' ? data.detail : '上傳失敗'; return; }
    $('upload-dialog').close(); $('upload-form').reset(); $('upload-status').textContent = ''; $('upload-progress').hidden = true;
    try { await selectDataset(data); await refresh(); } catch (e) { status(e.message); }
  };
  xhr.onerror = () => { $('upload-submit').disabled = false; $('upload-status').textContent = '連線中斷，請重新上傳。'; };
  const form = new FormData(); form.append('file', file); xhr.send(form);
};
$('filters').onsubmit = e => { e.preventDefault(); offset = 0; loadRows(); };
$('clear-filters').onclick = () => { $('filters').reset(); sort = null; offset = 0; loadRows(); };
$('prev').onclick = () => { offset = Math.max(0, offset - 100); loadRows(); };
$('next').onclick = () => { offset += 100; loadRows(); };
$('export').onclick = () => { window.location.href = `/api/datasets/${selected.id}/export?${query()}`; };
$('edit-form').onsubmit = async e => {
  e.preventDefault(); $('save-edit').disabled = true;
  const {dataset, row, column} = editing;
  try {
    const result = await api(`/api/datasets/${dataset}/rows/${row.id}`, {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({column, value: $('edit-value').value, version: row.version})});
    undo = {dataset, rowId: row.id, column, value: result.old_value, version: result.version};
    $('edit-dialog').close(); await loadRows(); status('修改已儲存。可在編輯視窗復原上一筆修改。');
  } catch (e) { $('edit-error').textContent = e.message; if (e.message.includes('其他人')) loadRows(); }
  finally { $('save-edit').disabled = false; }
};
$('undo').onclick = async () => {
  if (!undo) return;
  $('undo').disabled = true;
  try { await api(`/api/datasets/${undo.dataset}/rows/${undo.rowId}`, {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({column: undo.column, value: undo.value, version: undo.version})}); undo = null; $('edit-dialog').close(); await loadRows(); status('已復原上一筆修改。'); } catch (e) { $('edit-error').textContent = e.message; }
};
$('history').onclick = async () => {
  try {
    const records = await api(`/api/datasets/${selected.id}/audit`);
    $('history-list').replaceChildren();
    if (!records.length) $('history-list').append(node('p', '尚無修改紀錄'));
    for (const r of records) { const div = node('div', undefined, 'audit-item'); div.append(node('small', `${r.username} · ${new Date(r.changed_at * 1000).toLocaleString('zh-TW')} · 第 ${r.row_id} 列 / ${selected.columns[r.column_index]}`), node('p', `${r.old_value || '（空白）'} → ${r.new_value || '（空白）'}`)); $('history-list').append(div); }
    $('history-dialog').showModal();
  } catch (e) { status(e.message); }
};
api('/api/me').then(data => enter(data.username)).catch(() => showLogin());
