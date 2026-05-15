class ChipInput {
  constructor(wrapEl, inputEl, datalistEl) {
    this._wrap = wrapEl;
    this._input = inputEl;
    this._datalist = datalistEl;
    this._vals = [];
    this._input.addEventListener('keydown', e => {
      if ((e.key === 'Enter' || e.key === ',') && this._input.value.trim()) {
        e.preventDefault();
        this.add(this._input.value.trim().replace(/,$/, ''));
        this._input.value = '';
      }
      if (e.key === 'Backspace' && !this._input.value && this._vals.length) {
        this.remove(this._vals[this._vals.length - 1]);
      }
    });
    this._wrap.addEventListener('click', () => this._input.focus());
  }

  add(val) {
    val = val.trim();
    if (!val || this._vals.includes(val)) return;
    this._vals.push(val);
    this._renderChips();
  }

  remove(val) {
    this._vals = this._vals.filter(v => v !== val);
    this._renderChips();
  }

  _renderChips() {
    this._wrap.querySelectorAll('.chip').forEach(el => el.remove());
    this._vals.forEach(v => {
      const chip = document.createElement('span');
      chip.className = 'chip';
      chip.textContent = v;
      const x = document.createElement('span');
      x.className = 'chip-x';
      x.textContent = '×';
      x.addEventListener('click', ev => { ev.stopPropagation(); this.remove(v); });
      chip.appendChild(x);
      this._wrap.insertBefore(chip, this._input);
    });
  }

  values() { return [...this._vals]; }

  setOptions(arr) {
    if (!this._datalist) return;
    this._datalist.replaceChildren(
      ...arr.map(v => {
        const o = document.createElement('option');
        o.value = String(v);
        return o;
      })
    );
  }

  setValues(arr) {
    this._vals = [];
    arr.forEach(v => this.add(v));
  }

  clear() { this.setValues([]); }
}

let token = localStorage.getItem('documentSearchToken');

async function api(path, method = 'GET', body = null) {
  const headers = { 'X-Auth-Token': token ?? '' };
  if (body !== null) headers['Content-Type'] = 'application/json';
  const res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : null });
  if (res.status === 401 && path !== '/api/login') {
    showToast('Session expired — please sign in again', 'err');
    token = null;
    localStorage.removeItem('documentSearchToken');
    localStorage.removeItem('documentSearchRole');
    document.getElementById('configPanel')?.classList.add('hidden');
    document.getElementById('appPanel')?.classList.add('hidden');
    document.getElementById('statusPanel')?.classList.add('hidden');
    document.getElementById('navSignout')?.classList.add('hidden');
    document.getElementById('navSep')?.classList.add('hidden');
    document.getElementById('authGate')?.classList.remove('hidden');
    throw new Error('Unauthorized');
  }
  if (!res.ok) {
    const text = await res.text();
    let msg = text;
    try {
      const detail = JSON.parse(text)?.detail;
      if (Array.isArray(detail)) {
        msg = detail.map(e => e.msg || String(e)).join('; ');
      } else {
        msg = String(detail ?? text);
      }
    } catch (_) {}
    throw new Error(msg || `Request failed (${res.status})`);
  }
  return await res.json();
}

function escHtml(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function setText(id, message, type = '') {
  const node = document.getElementById(id);
  if (!node) return;
  node.textContent = message;
  node.className = node.className.replace(/\b(ok|err|info)\b/g, '').trim();
  if (type) node.classList.add(type);
}

// ── Toast notifications ────────────────────────────────────────────
function showToast(msg, type = 'info', duration = 3500) {
  const wrap = document.getElementById('toastWrap');
  if (!wrap) return;
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  t.innerHTML = `<div class="toast-dot"></div><span>${escHtml(msg)}</span>`;
  wrap.appendChild(t);
  setTimeout(() => {
    t.style.opacity = '0'; t.style.transition = 'opacity .25s';
    setTimeout(() => t.remove(), 280);
  }, duration);
}

// ── Auth ───────────────────────────────────────────────────────────
function showAuthedPanels() {
  document.getElementById('authGate')?.classList.add('hidden');
  document.getElementById('appPanel')?.classList.remove('hidden');
  document.getElementById('configPanel')?.classList.remove('hidden');
  document.getElementById('statusPanel')?.classList.remove('hidden');
  document.getElementById('navSignout')?.classList.remove('hidden');
  document.getElementById('navSep')?.classList.remove('hidden');
}

function signOut() {
  localStorage.removeItem('documentSearchToken');
  localStorage.removeItem('documentSearchRole');
  token = null;
  location.reload();
}

function formatBytes(bytes) {
  const size = Number(bytes || 0);
  if (size <= 0) return '0 MB';
  return `${(size / (1024 * 1024)).toFixed(2)} MB`;
}

async function loadStatus() {
  try {
    const status = await api('/api/status');
    const wt = document.getElementById('welcomeText');
    if (wt) wt.textContent = 'System is running. Here is the current index status.';
    if (document.getElementById('statDocuments'))
      statDocuments.textContent = String(status.documents ?? 0);
    if (document.getElementById('statBlocks'))
      statBlocks.textContent = String(status.content_blocks ?? 0);
    if (document.getElementById('statStorage'))
      statStorage.textContent = formatBytes(status.total_file_size_bytes ?? 0);
  } catch (_) {}
}

async function login() {
  try {
    const data = await api('/api/login', 'POST', { username: username.value, password: password.value });
    token = data.token;
    localStorage.setItem('documentSearchToken', token);
    localStorage.setItem('documentSearchRole', data.role || 'user');
    setText('loginResult', '', '');
    showToast(`Signed in as ${data.username}`, 'ok');
    showAuthedPanels();
    if (data.role === 'admin') showAdminUI();
    await loadStatus();
    if (document.getElementById('configPanel')) await loadConfig();
    if (document.querySelector('[data-page="search"]')) await loadTagCloud();
  } catch (error) {
    setText('loginResult', `Login failed: ${error.message}`, 'err');
  }
}

// ── Recent searches ────────────────────────────────────────────────
function saveRecentSearch(q) {
  if (!q?.trim()) return;
  const searches = JSON.parse(localStorage.getItem('seekr_recent') || '[]');
  const updated = [q.trim(), ...searches.filter(s => s !== q.trim())].slice(0, 5);
  localStorage.setItem('seekr_recent', JSON.stringify(updated));
  renderRecentSearches();
}

function renderRecentSearches() {
  const el = document.getElementById('recentSearches');
  if (!el) return;
  const searches = JSON.parse(localStorage.getItem('seekr_recent') || '[]');
  if (!searches.length) {
    el.innerHTML = '<p class="muted" style="font-size:.82rem;">No recent searches yet.</p>';
    return;
  }
  el.innerHTML = `<div class="recent-list">${searches.map(s => `
    <a href="/search?q=${encodeURIComponent(s)}" class="recent-item">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="13" height="13"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
      ${escHtml(s)}
    </a>`).join('')}</div>`;
}

// ── Search ─────────────────────────────────────────────────────────
function toggleFilters() {
  const body = document.getElementById('filterBody');
  const btn = document.getElementById('filterToggle');
  if (!body || !btn) return;
  const willOpen = body.classList.contains('hidden');
  body.classList.toggle('hidden', !willOpen);
  btn.classList.toggle('open', willOpen);
  btn.querySelector('.ft-lbl').textContent = willOpen ? 'Hide filters' : 'Show filters';
}

function clearSearch() {
  const q = document.getElementById('query');
  if (q) { q.value = ''; q.focus(); }
  const metaEl = document.getElementById('resultsMeta');
  if (metaEl) metaEl.textContent = '';
  const resultsEl = document.getElementById('results');
  if (resultsEl) resultsEl.innerHTML = '';
}

async function runSearch() {
  const resultsEl = document.getElementById('results');
  try {
    const payload = {
      query: query.value, limit: 25,
      filetype: filetype.value || null,
      path: pathFilter.value || null,
      block_type: blockType.value || null,
      modified_from: modifiedFrom.value || null,
      modified_to: modifiedTo.value || null,
      tag: document.getElementById('tagFilterInput')?.value?.trim() || null,
    };
    const data = await api('/api/search', 'POST', payload);
    if (payload.query?.trim()) saveRecentSearch(payload.query);

    const metaEl = document.getElementById('resultsMeta');
    if (metaEl) {
      metaEl.textContent = !data.length
        ? ''
        : data.length === 25 ? '25+ results' : `${data.length} result${data.length !== 1 ? 's' : ''}`;
    }

    if (!data.length) {
      resultsEl.innerHTML = `
        <div class="empty">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
          </svg>
          <p>No results found for this query.</p>
        </div>`;
      return;
    }

    resultsEl.innerHTML = data.map(r => `
      <div class="rc">
        <div class="rc-name">${escHtml(r.filename)}${r.is_marked ? ' ⭐' : ''}</div>
        <div class="rc-badges">
          <span class="badge badge-n">${escHtml(r.block_type)}</span>
          <span class="badge badge-n">Block ${r.block_number}</span>
          ${r.tags?.length ? `<span class="badge badge-b">${escHtml(r.tags.join(', '))}</span>` : ''}
        </div>
        <div class="rc-path">${escHtml(r.path)}</div>
        <div class="rc-snippet">${r.snippet_html ?? ''}</div>
        <div class="rc-foot">
          <input id="tags-${r.document_id}" value="${escHtml((r.tags || []).join(', '))}" placeholder="tag1, tag2" />
          <button class="btn btn-g btn-sm" onclick="saveTags(${r.document_id})">Save tags</button>
          <button class="btn btn-g btn-sm" onclick="toggleMark(${r.document_id}, ${!!r.is_marked})">
            ${r.is_marked ? 'Unmark' : 'Mark'}
          </button>
          <button class="btn btn-g btn-sm" onclick="reindexDocumentFromSearch(${r.document_id})">Reindex</button>
          <a href="${r.open_url}" target="_blank" class="btn btn-g btn-sm">Open file</a>
        </div>
      </div>`).join('');
  } catch (e) {
    if (resultsEl) resultsEl.textContent = e.message;
  }
}

async function saveTags(documentId) {
  const input = document.getElementById(`tags-${documentId}`);
  const tags = input.value.split(',').map(s => s.trim()).filter(Boolean);
  await api('/api/documents/tags', 'POST', { document_id: documentId, tags });
  showToast('Tags saved', 'ok');
}

async function toggleMark(documentId, current) {
  await api('/api/documents/mark', 'POST', { document_id: documentId, is_marked: !current });
  await runSearch();
}

// ── Ingest ─────────────────────────────────────────────────────────
function initDropZone() {
  const zone = document.getElementById('dropZone');
  const input = document.getElementById('uploadFile');
  const nameEl = document.getElementById('dropFileName');
  if (!zone || !input) return;

  zone.addEventListener('click', () => input.click());
  input.addEventListener('change', () => {
    const f = input.files[0];
    if (f && nameEl) nameEl.textContent = f.name;
  });
  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('over'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('over'));
  zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('over');
    const f = e.dataTransfer.files[0];
    if (f) {
      const dt = new DataTransfer();
      dt.items.add(f);
      input.files = dt.files;
      if (nameEl) nameEl.textContent = f.name;
    }
  });
}

let _lastUploadDocId = null;
let _lastAiSuggestion = null;

async function uploadDocument() {
  const f = uploadFile.files[0];
  if (!f) { showToast('Select a file first', 'err'); return; }
  const btn = document.getElementById('uploadBtn');
  if (btn) btn.classList.add('loading');
  dismissAiSuggestion();
  const fd = new FormData();
  fd.append('file', f);
  fd.append('target_subpath', uploadPath.value || '');
  fd.append('tags', uploadTags.value || '');
  fd.append('metadata_json', uploadMeta.value || '{}');
  try {
    const res = await fetch('/api/upload', { method: 'POST', headers: { 'X-Auth-Token': token ?? '' }, body: fd });
    const json = await res.json();
    uploadResult.textContent = JSON.stringify(json, null, 2);
    showToast('File uploaded successfully', 'ok');
    const nameEl = document.getElementById('dropFileName');
    if (nameEl) nameEl.textContent = '';
    uploadFile.value = '';

    // Show AI suggestion if present
    if (json.document_id && json.ai_suggestion?.suggested_subpath) {
      _lastUploadDocId = json.document_id;
      _lastAiSuggestion = json.ai_suggestion;
      renderAiSuggestion(json.ai_suggestion, json.document_id);
    }
  } catch (e) {
    showToast(e.message, 'err');
    uploadResult.textContent = e.message;
  } finally {
    if (btn) btn.classList.remove('loading');
  }
}

// ── AI suggestion (post-upload) ────────────────────────────────────
function renderAiSuggestion(sug, docId) {
  const card = document.getElementById('aiSuggestionCard');
  const content = document.getElementById('aiSuggestionContent');
  const modelEl = document.getElementById('aiSuggestionModel');
  if (!card || !content) return;

  if (modelEl) modelEl.textContent = sug.model ? `Model: ${sug.model}` : 'Ollama';
  content.innerHTML = `
    <div class="path-test-item">
      <span class="path-test-lbl">Suggested path</span>
      <span class="badge badge-b" style="font-size:.85rem;">${escHtml(sug.suggested_subpath || '—')}</span>
    </div>
    <div class="path-test-item">
      <span class="path-test-lbl">Suggested tags</span>
      <span class="badge badge-n">${sug.suggested_tags?.length ? escHtml(sug.suggested_tags.join(', ')) : '—'}</span>
    </div>
    <div class="path-test-item" style="grid-column:1/-1;">
      <span class="path-test-lbl">Reason</span>
      <span style="font-size:.85rem;color:var(--txt-2);">${escHtml(sug.reason || '—')}</span>
    </div>`;
  card.classList.remove('hidden');
  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function dismissAiSuggestion() {
  const card = document.getElementById('aiSuggestionCard');
  if (card) card.classList.add('hidden');
  _lastUploadDocId = null;
  _lastAiSuggestion = null;
  setText('aiApplyResult', '', '');
}

async function applyAiSuggestion() {
  if (!_lastUploadDocId || !_lastAiSuggestion?.suggested_subpath) {
    showToast('No suggestion to apply', 'err'); return;
  }
  const btn = document.getElementById('aiApplyBtn');
  if (btn) btn.classList.add('loading');
  try {
    const results = await api('/api/ai/reorganize/apply', 'POST', {
      moves: [{ document_id: _lastUploadDocId, new_subpath: _lastAiSuggestion.suggested_subpath }]
    });
    const r = results[0];
    if (r?.status === 'moved') {
      showToast(`File moved to ${_lastAiSuggestion.suggested_subpath}`, 'ok');
      setText('aiApplyResult', `Moved to: ${r.new_path}`, 'ok');
    } else {
      setText('aiApplyResult', r?.detail || r?.status || 'Unchanged', 'info');
    }
  } catch (e) {
    showToast(e.message, 'err');
    setText('aiApplyResult', e.message, 'err');
  } finally {
    if (btn) btn.classList.remove('loading');
  }
}

// ── AI Reorganizer ─────────────────────────────────────────────────
let _reorganizeResults = [];

async function startAiReorganize() {
  const btn = document.getElementById('reorganizeStartBtn');
  const progressWrap = document.getElementById('reorganizeProgress');
  const fill = document.getElementById('reorganizeProgressFill');
  const status = document.getElementById('reorganizeProgressStatus');
  const resultsEl = document.getElementById('reorganizeResults');

  if (btn) btn.classList.add('loading');
  if (progressWrap) progressWrap.classList.remove('hidden');
  if (resultsEl) resultsEl.classList.add('hidden');
  if (fill) fill.style.width = '4%';
  if (status) status.textContent = 'Starting analysis…';

  const limit = Number(document.getElementById('reorganizeLimit')?.value || 10);
  try {
    const { job_id } = await api(`/api/ai/reorganize/start?limit=${limit}`, 'POST', {});

    const poll = setInterval(async () => {
      try {
        const job = await api(`/api/ai/jobs/${job_id}`);
        const pct = job.total > 0 ? Math.round((job.done / job.total) * 90) + 5 : 10;
        if (fill) fill.style.width = `${pct}%`;
        if (status) status.textContent = `Analysed ${job.done} / ${job.total || '?'} documents…`;

        if (job.status === 'finished') {
          clearInterval(poll);
          if (btn) btn.classList.remove('loading');
          if (fill) { fill.style.width = '100%'; fill.style.background = 'var(--green)'; }
          if (status) status.textContent = `Done — ${job.results.length} suggestions`;
          _reorganizeResults = job.results;
          renderReorganizeTable(job.results);
          if (resultsEl) resultsEl.classList.remove('hidden');
          showToast(`${job.results.length} suggestions ready`, 'ok');
        }
      } catch (_) {}
    }, 1500);
  } catch (e) {
    showToast(e.message, 'err');
    if (btn) btn.classList.remove('loading');
  }
}

function renderReorganizeTable(results) {
  const tbody = document.getElementById('reorganizeTableBody');
  if (!tbody) return;

  if (!results.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="muted" style="text-align:center;padding:1rem;">No uploaded documents found in the upload root.</td></tr>';
    return;
  }
  tbody.innerHTML = results.map((r, i) => {
    const curShort = r.current_path.split('/').slice(-2).join('/');
    const hasSuggestion = !!r.suggested_subpath;
    return `<tr>
      <td><input type="checkbox" class="reorg-check" data-idx="${i}" ${hasSuggestion ? '' : 'disabled'} /></td>
      <td><strong>${escHtml(r.filename)}</strong></td>
      <td><code style="font-size:.75rem;">${escHtml(curShort)}</code></td>
      <td>${hasSuggestion ? `<span class="badge badge-b">${escHtml(r.suggested_subpath)}</span>` : '<span class="muted">—</span>'}</td>
      <td>${r.suggested_tags?.length ? `<span class="badge badge-n">${escHtml(r.suggested_tags.join(', '))}</span>` : '<span class="muted">—</span>'}</td>
      <td style="font-size:.78rem;color:var(--txt-3);">${escHtml(r.reason || '—')}</td>
    </tr>`;
  }).join('');
}

function toggleSelectAll(cb) {
  document.querySelectorAll('.reorg-check:not([disabled])').forEach(c => { c.checked = cb.checked; });
}

async function applySelectedMoves() {
  const checked = [...document.querySelectorAll('.reorg-check:checked')];
  if (!checked.length) { showToast('Select at least one document', 'err'); return; }

  const moves = checked.map(c => {
    const r = _reorganizeResults[Number(c.dataset.idx)];
    return { document_id: r.document_id, new_subpath: r.suggested_subpath };
  }).filter(m => m.new_subpath);

  const btn = document.getElementById('reorganizeApplyBtn');
  if (btn) btn.classList.add('loading');
  try {
    const results = await api('/api/ai/reorganize/apply', 'POST', { moves });
    const moved = results.filter(r => r.status === 'moved').length;
    const errors = results.filter(r => r.status === 'error').length;
    showToast(`Moved ${moved} file${moved !== 1 ? 's' : ''}${errors ? `, ${errors} error(s)` : ''}`, moved > 0 ? 'ok' : 'err');
    setText('reorganizeApplyResult', `Moved: ${moved} · Errors: ${errors} · Unchanged: ${results.length - moved - errors}`, moved > 0 ? 'ok' : 'info');
  } catch (e) {
    showToast(e.message, 'err');
    setText('reorganizeApplyResult', e.message, 'err');
  } finally {
    if (btn) btn.classList.remove('loading');
  }
}

async function startIndex() {
  const btn = document.getElementById('startIndexBtn');
  const progressWrap = document.getElementById('indexProgress');
  const progressFill = document.getElementById('indexProgressFill');
  const progressStatus = document.getElementById('indexProgressStatus');
  try {
    if (btn) btn.classList.add('loading');
    if (progressWrap) progressWrap.classList.remove('hidden');
    if (progressFill) { progressFill.style.width = '5%'; progressFill.style.background = ''; }
    if (progressStatus) progressStatus.textContent = 'Starting job…';

    const data = await api('/api/index/start', 'POST', {
      paths: paths.value.split(',').map(s => s.trim()).filter(Boolean),
    });
    const id = data.job_id;
    if (progressStatus) progressStatus.textContent = `Job ${id} started`;
    let pct = 10;

    const interval = setInterval(async () => {
      try {
        const j = await api(`/api/index/jobs/${id}`);
        if (progressStatus) progressStatus.textContent = `${j.status} — processed: ${j.processed ?? 0}, errors: ${j.errors ?? 0}`;
        if (j.status === 'finished' || j.status === 'error') {
          clearInterval(interval);
          if (btn) btn.classList.remove('loading');
          if (progressFill) {
            progressFill.style.width = '100%';
            progressFill.style.background = j.status === 'finished' ? 'var(--green)' : 'var(--red)';
          }
          showToast(j.status === 'finished' ? 'Indexing complete' : 'Indexing failed', j.status === 'finished' ? 'ok' : 'err');
        } else {
          pct = Math.min(pct + 7, 88);
          if (progressFill) progressFill.style.width = `${pct}%`;
        }
      } catch (_) {}
    }, 1200);
  } catch (e) {
    showToast(e.message, 'err');
    if (btn) btn.classList.remove('loading');
  }
}

async function checkForUpdates() {
  const btn = document.getElementById('checkUpdateBtn');
  const statusEl = document.getElementById('updateStatus');
  if (btn) btn.classList.add('loading');
  if (statusEl) { statusEl.textContent = 'Checking GitHub…'; statusEl.className = 'feedback info'; statusEl.classList.remove('hidden'); }
  try {
    const data = await api('/api/update/check');
    if (!statusEl) return;
    const cur = escHtml((data.current_commit || '?').slice(0, 7));
    const lat = escHtml((data.latest_commit  || '?').slice(0, 7));
    if (data.error && !data.latest_commit) {
      statusEl.textContent = `Could not reach GitHub: ${data.error}`;
      statusEl.className = 'feedback err';
    } else if (data.update_available === true) {
      statusEl.textContent = `Update available — current ${cur} → latest ${lat}`;
      statusEl.className = 'feedback ok';
    } else if (data.update_available === false) {
      statusEl.textContent = `Up to date (${cur})`;
      statusEl.className = 'feedback ok';
    } else {
      statusEl.textContent = `Current commit: ${cur} — GitHub unreachable`;
      statusEl.className = 'feedback info';
    }
  } catch (e) {
    if (statusEl) { statusEl.textContent = e.message; statusEl.className = 'feedback err'; }
  } finally {
    if (btn) btn.classList.remove('loading');
  }
}

let _updatePollInterval = null;

async function runUpdate() {
  if (!confirm('Run system update? The app will rebuild and briefly go offline.')) return;
  const btn = document.getElementById('runUpdateBtn');
  const statusEl = document.getElementById('updateStatus');
  if (btn) btn.classList.add('loading');
  if (statusEl) { statusEl.textContent = 'Starting update…'; statusEl.className = 'feedback info'; statusEl.classList.remove('hidden'); }
  if (_updatePollInterval) { clearInterval(_updatePollInterval); _updatePollInterval = null; }

  try {
    await api('/api/update/run', 'POST', {});
    if (statusEl) statusEl.textContent = 'Update running — the app may restart…';

    _updatePollInterval = setInterval(async () => {
      try {
        const s = await api('/api/update/status');
        if (s.status === 'done') {
          clearInterval(_updatePollInterval); _updatePollInterval = null;
          if (btn) btn.classList.remove('loading');
          if (statusEl) { statusEl.textContent = 'Update complete — reloading…'; statusEl.className = 'feedback ok'; }
          showToast('Update complete', 'ok');
          setTimeout(() => location.reload(), 2500);
        } else if (s.status === 'error') {
          clearInterval(_updatePollInterval); _updatePollInterval = null;
          if (btn) btn.classList.remove('loading');
          const detail = escHtml((s.stderr || s.stdout || 'unknown error').slice(0, 200));
          if (statusEl) { statusEl.textContent = `Update failed: ${detail}`; statusEl.className = 'feedback err'; }
          showToast('Update failed', 'err');
        }
      } catch (_) {
        if (statusEl) statusEl.textContent = 'App restarting, reconnecting…';
      }
    }, 2500);
  } catch (e) {
    if (statusEl) { statusEl.textContent = e.message; statusEl.className = 'feedback err'; }
    if (btn) btn.classList.remove('loading');
  }
}

// ── Config ─────────────────────────────────────────────────────────
async function loadConfig() {
  try {
    const c = await api('/api/config');
    if (document.getElementById('cfgDb')) cfgDb.value = c.database_path ?? '';
    if (document.getElementById('cfgExt')) cfgExt.value = (c.supported_extensions || []).join(', ');
    if (document.getElementById('cfgExcludeDirs')) cfgExcludeDirs.value = (c.exclude_dirs || []).join(', ');
    if (document.getElementById('cfgExcludePatterns')) cfgExcludePatterns.value = (c.exclude_patterns || []).join(', ');
    if (document.getElementById('cfgMaxSize')) cfgMaxSize.value = c.max_file_size_mb ?? 100;
    _sourcePaths = Array.isArray(c.source_paths) ? c.source_paths : [];
    renderPathList(_sourcePaths);
  } catch (e) {
    if (document.getElementById('configResult')) setText('configResult', e.message, 'err');
  }
}

async function saveConfig() {
  try {
    const payload = {
      database_path: cfgDb.value,
      supported_extensions: cfgExt.value.split(',').map(s => s.trim()).filter(Boolean),
      exclude_dirs: cfgExcludeDirs.value.split(',').map(s => s.trim()).filter(Boolean),
      exclude_patterns: cfgExcludePatterns.value.split(',').map(s => s.trim()).filter(Boolean),
      max_file_size_mb: Number(cfgMaxSize.value || 100),
      source_paths: _sourcePaths,
    };
    await api('/api/config', 'POST', payload);
    showToast('Configuration saved', 'ok');
    setText('configResult', 'Saved successfully', 'ok');
  } catch (e) {
    showToast(e.message, 'err');
    setText('configResult', e.message, 'err');
  }
}

// ── Tab switching ──────────────────────────────────────────────────
function switchTab(name) {
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.add('hidden'));
  document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
  const panel = document.getElementById(`tab-${name}`);
  if (panel) panel.classList.remove('hidden');
  const btn = document.querySelector(`.tab[data-tab="${name}"]`);
  if (btn) btn.classList.add('active');

  if (name === 'users') loadUsers();
  if (name === 'ssl') loadSslStatus();
  if (name === 'ai') loadAiTabData();
  if (name === 'ha') loadHaKeys();
  if (name === 'system') { loadDeps(); loadAiStatus(); }
}

function showAdminUI() {
  document.querySelectorAll('.admin-only').forEach(el => el.classList.remove('hidden'));
}

// ── Paths ──────────────────────────────────────────────────────────
let _sourcePaths = [];

function renderPathList(paths) {
  const el = document.getElementById('pathList');
  if (!el) return;
  if (!paths.length) {
    el.innerHTML = '<p class="muted" style="font-size:.82rem;margin-bottom:.75rem;">No source paths configured yet.</p>';
    return;
  }
  el.innerHTML = `<div class="u-table-wrap"><table class="u-table"><thead><tr><th>Path</th><th>Label</th><th>Type</th><th></th></tr></thead><tbody>${
    paths.map((p, i) => `<tr>
      <td><code style="font-size:.8rem;">${escHtml(p.path)}</code></td>
      <td>${escHtml(p.label || '—')}</td>
      <td><span class="badge badge-n">${escHtml(p.type || 'local')}</span></td>
      <td>
        <button class="btn btn-g btn-sm" onclick="testPathQuick(${i})">Test</button>
        <button class="btn btn-g btn-sm" style="color:var(--red)" onclick="removeSourcePath(${i})">Remove</button>
      </td>
    </tr>`).join('')
  }</tbody></table></div>`;
}

function addSourcePath() {
  const path = document.getElementById('newPathValue')?.value?.trim();
  const label = document.getElementById('newPathLabel')?.value?.trim();
  const type = document.getElementById('newPathType')?.value || 'local';
  if (!path) { showToast('Enter a path first', 'err'); return; }
  _sourcePaths.push({ path, label, type });
  renderPathList(_sourcePaths);
  if (document.getElementById('newPathValue')) document.getElementById('newPathValue').value = '';
  if (document.getElementById('newPathLabel')) document.getElementById('newPathLabel').value = '';
}

function removeSourcePath(idx) {
  _sourcePaths.splice(idx, 1);
  renderPathList(_sourcePaths);
}

async function testPathQuick(idx) {
  const p = _sourcePaths[idx];
  if (!p) return;
  document.getElementById('testPathInput').value = p.path;
  switchTab('paths');
  await runPathTest();
}

async function savePathsConfig() {
  try {
    const current = await api('/api/config');
    const payload = { ...current, source_paths: _sourcePaths };
    await api('/api/config', 'POST', payload);
    showToast('Paths saved', 'ok');
    setText('pathsResult', 'Saved successfully', 'ok');
  } catch (e) {
    showToast(e.message, 'err');
    setText('pathsResult', e.message, 'err');
  }
}

async function runPathTest() {
  const path = document.getElementById('testPathInput')?.value?.trim();
  if (!path) { showToast('Enter a path to test', 'err'); return; }
  const btn = document.getElementById('testPathBtn');
  if (btn) btn.classList.add('loading');
  try {
    const r = await api('/api/paths/test', 'POST', { path });
    const el = document.getElementById('pathTestResult');
    if (el) el.classList.remove('hidden');
    function ptBadge(id, ok, text) {
      const node = document.getElementById(id);
      if (!node) return;
      node.textContent = text !== undefined ? text : (ok ? 'Yes' : 'No');
      node.className = `badge ${ok ? 'badge-g' : 'badge-n'}`;
      node.style.color = ok ? 'var(--green)' : 'var(--red)';
    }
    ptBadge('ptExists',   r.exists,   undefined);
    ptBadge('ptIsDir',    r.is_dir,   undefined);
    ptBadge('ptReadable', r.readable, undefined);
    ptBadge('ptWritable', r.writable, undefined);
    const ec = document.getElementById('ptEntries');
    if (ec) { ec.textContent = r.entry_count != null ? String(r.entry_count) : '—'; ec.className = 'badge badge-n'; ec.style.color = ''; }
  } catch (e) {
    showToast(e.message, 'err');
  } finally {
    if (btn) btn.classList.remove('loading');
  }
}

function onMountTypeChange() {
  const t = document.getElementById('mountType')?.value;
  const show = t === 'smb';
  ['mountCredUser', 'mountCredPass', 'mountCredDomain'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.toggle('hidden', !show);
  });
}

async function mountShare() {
  const btn = document.getElementById('mountBtn');
  if (btn) btn.classList.add('loading');
  const resultEl = document.getElementById('mountResult');
  try {
    const r = await api('/api/paths/mount', 'POST', {
      remote_path: document.getElementById('mountRemote')?.value?.trim(),
      mount_point: document.getElementById('mountPoint')?.value?.trim(),
      share_type:  document.getElementById('mountType')?.value || 'smb',
      username:    document.getElementById('mountUser')?.value || null,
      password:    document.getElementById('mountPass')?.value || null,
      domain:      document.getElementById('mountDomain')?.value || null,
    });
    if (resultEl) { resultEl.classList.remove('hidden'); resultEl.textContent = r.mounted ? 'Mounted successfully.' : (r.stderr || r.stdout || 'Failed'); }
    showToast(r.mounted ? 'Share mounted' : 'Mount failed', r.mounted ? 'ok' : 'err');
  } catch (e) {
    if (resultEl) { resultEl.classList.remove('hidden'); resultEl.textContent = e.message; }
    showToast(e.message, 'err');
  } finally {
    if (btn) btn.classList.remove('loading');
  }
}

async function unmountShare() {
  const path = document.getElementById('mountPoint')?.value?.trim();
  if (!path) { showToast('Enter mount point first', 'err'); return; }
  const btn = document.getElementById('unmountBtn');
  if (btn) btn.classList.add('loading');
  const resultEl = document.getElementById('mountResult');
  try {
    const r = await api('/api/paths/unmount', 'POST', { path });
    if (resultEl) { resultEl.classList.remove('hidden'); resultEl.textContent = r.unmounted ? 'Unmounted.' : (r.stderr || 'Failed'); }
    showToast(r.unmounted ? 'Unmounted' : 'Unmount failed', r.unmounted ? 'ok' : 'err');
  } catch (e) {
    if (resultEl) { resultEl.classList.remove('hidden'); resultEl.textContent = e.message; }
    showToast(e.message, 'err');
  } finally {
    if (btn) btn.classList.remove('loading');
  }
}

// ── User management ────────────────────────────────────────────────
async function loadUsers() {
  try {
    const users = await api('/api/users');
    renderUserTable(users);
  } catch (e) {
    setText('usersResult', e.message, 'err');
  }
}

function renderUserTable(users) {
  const el = document.getElementById('userTable');
  if (!el) return;
  if (!users.length) {
    el.innerHTML = '<p class="muted" style="font-size:.82rem;">No users found.</p>';
    return;
  }
  el.innerHTML = `<table class="u-table"><thead><tr><th>ID</th><th>Username</th><th>Role</th><th>Created</th><th>Actions</th></tr></thead><tbody>${
    users.map(u => `<tr>
      <td class="muted">${u.id}</td>
      <td><strong>${escHtml(u.username)}</strong></td>
      <td>
        <select class="u-role-select" onchange="updateUserRole(${u.id}, this.value)">
          <option value="user" ${u.role === 'user' ? 'selected' : ''}>User</option>
          <option value="admin" ${u.role === 'admin' ? 'selected' : ''}>Admin</option>
        </select>
      </td>
      <td class="muted" style="font-size:.78rem;">${(u.created_at || '').slice(0, 10)}</td>
      <td>
        <button class="btn btn-g btn-sm" onclick="openChangePassword(${u.id}, '${escHtml(u.username)}')">Password</button>
        <button class="btn btn-g btn-sm" style="color:var(--red)" onclick="deleteUser(${u.id})">Delete</button>
      </td>
    </tr>`).join('')
  }</tbody></table>`;
}

async function createUser() {
  const username = document.getElementById('newUsername')?.value?.trim();
  const password = document.getElementById('newUserPassword')?.value;
  const role = document.getElementById('newUserRole')?.value || 'user';
  if (!username || !password) { showToast('Username and password required', 'err'); return; }
  try {
    await api('/api/users', 'POST', { username, password, role });
    showToast(`User "${username}" created`, 'ok');
    setText('createUserResult', `User "${username}" created successfully`, 'ok');
    if (document.getElementById('newUsername')) document.getElementById('newUsername').value = '';
    if (document.getElementById('newUserPassword')) document.getElementById('newUserPassword').value = '';
    await loadUsers();
  } catch (e) {
    showToast(e.message, 'err');
    setText('createUserResult', e.message, 'err');
  }
}

async function updateUserRole(userId, role) {
  try {
    await api(`/api/users/${userId}`, 'PUT', { role });
    showToast('Role updated', 'ok');
    setText('usersResult', 'Role updated', 'ok');
  } catch (e) {
    showToast(e.message, 'err');
    setText('usersResult', e.message, 'err');
    await loadUsers();
  }
}

async function deleteUser(userId) {
  if (!confirm('Delete this user? This cannot be undone.')) return;
  try {
    await api(`/api/users/${userId}`, 'DELETE');
    showToast('User deleted', 'ok');
    await loadUsers();
  } catch (e) {
    showToast(e.message, 'err');
  }
}

function openChangePassword(userId, username) {
  const card = document.getElementById('changePwCard');
  const label = document.getElementById('changePwLabel');
  const idInput = document.getElementById('changePwUserId');
  if (card) card.classList.remove('hidden');
  if (label) label.textContent = `Set new password for "${username}"`;
  if (idInput) idInput.value = String(userId);
  if (document.getElementById('changePwInput')) document.getElementById('changePwInput').value = '';
  card?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function cancelChangePassword() {
  const card = document.getElementById('changePwCard');
  if (card) card.classList.add('hidden');
}

async function submitChangePassword() {
  const userId = document.getElementById('changePwUserId')?.value;
  const pw = document.getElementById('changePwInput')?.value;
  if (!pw) { showToast('Enter a new password', 'err'); return; }
  try {
    await api(`/api/users/${userId}/change-password`, 'POST', { new_password: pw });
    showToast('Password changed', 'ok');
    setText('changePwResult', 'Password changed successfully', 'ok');
    setTimeout(cancelChangePassword, 1500);
  } catch (e) {
    showToast(e.message, 'err');
    setText('changePwResult', e.message, 'err');
  }
}

// ── DB Test ────────────────────────────────────────────────────────
async function runDbTest() {
  const btn = document.getElementById('dbTestBtn');
  if (btn) btn.classList.add('loading');
  try {
    const r = await api('/api/system/db-test');
    const el = document.getElementById('dbTestResult');
    const grid = document.getElementById('dbTestGrid');
    if (el) el.classList.remove('hidden');
    if (grid) {
      const ok = r.ok;
      grid.innerHTML = [
        { label: 'Status',       val: ok ? 'OK' : 'Error',  color: ok ? 'var(--green)' : 'var(--red)' },
        { label: 'Documents',    val: String(r.documents ?? '—') },
        { label: 'Content blocks', val: String(r.content_blocks ?? '—') },
        { label: 'Users',        val: String(r.users ?? '—') },
        { label: 'Integrity',    val: r.integrity ?? '—', color: r.integrity === 'ok' ? 'var(--green)' : 'var(--amber)' },
        { label: 'Journal mode', val: r.journal_mode ?? '—' },
        { label: 'DB size',      val: r.db_size_bytes != null ? `${(r.db_size_bytes / 1024 / 1024).toFixed(2)} MB` : '—' },
        { label: 'DB path',      val: r.db_path ?? '—' },
      ].map(i => `<div class="path-test-item"><span class="path-test-lbl">${escHtml(i.label)}</span><span class="badge badge-n" style="${i.color ? `color:${i.color}` : ''}">${escHtml(i.val)}</span></div>`).join('');
      if (r.error) setText('dbTestFeedback', r.error, 'err');
    }
    showToast(r.ok ? 'Database OK' : `DB error: ${r.error}`, r.ok ? 'ok' : 'err');
  } catch (e) {
    showToast(e.message, 'err');
    setText('dbTestFeedback', e.message, 'err');
  } finally {
    if (btn) btn.classList.remove('loading');
  }
}

async function loadDeps() {
  try {
    const deps = await api('/api/system/dependencies');
    const grid = document.getElementById('depsGrid');
    if (!grid) return;
    grid.innerHTML = Object.entries(deps).map(([tool, ok]) =>
      `<div class="path-test-item">
        <span class="path-test-lbl">${escHtml(tool)}</span>
        <span class="badge" style="color:${ok ? 'var(--green)' : 'var(--txt-3)'}">${ok ? 'Installed' : 'Missing'}</span>
      </div>`
    ).join('');
  } catch (_) {}
}

async function loadAiStatus() {
  try {
    const s = await api('/api/ai/status');
    const grid = document.getElementById('aiStatusGrid');
    if (grid) {
      grid.innerHTML = [
        { label: 'Status',  val: s.available ? 'Connected' : 'Unavailable', color: s.available ? 'var(--green)' : 'var(--red)' },
        { label: 'URL',     val: s.base_url || '—' },
        { label: 'Model',   val: s.configured_model || '—' },
        { label: 'Models available', val: String(s.models?.length ?? 0) },
      ].map(i => `<div class="path-test-item"><span class="path-test-lbl">${escHtml(i.label)}</span><span class="badge badge-n" style="${i.color ? `color:${i.color}` : ''}">${escHtml(i.val)}</span></div>`).join('');
    }
    const modelListEl = document.getElementById('aiModelList');
    const modelTagsEl = document.getElementById('aiModelTags');
    if (s.models?.length && modelListEl && modelTagsEl) {
      modelListEl.classList.remove('hidden');
      modelTagsEl.innerHTML = s.models.map(m => `<span class="badge badge-n">${escHtml(m)}</span>`).join('');
    }
  } catch (e) {
    const grid = document.getElementById('aiStatusGrid');
    if (grid) grid.innerHTML = `<div class="path-test-item"><span class="path-test-lbl">Error</span><span class="badge badge-n" style="color:var(--red)">${escHtml(e.message)}</span></div>`;
  }
}

// ── AI config tab ──────────────────────────────────────────────────

async function loadAiTabData() {
  // Populate URL + model fields from live config
  try {
    const cfg = await api('/api/config');
    const urlEl = document.getElementById('aiCfgUrl');
    const txtEl = document.getElementById('aiCfgModelText');
    if (urlEl) urlEl.value = cfg.ollama_url || '';
    if (txtEl) txtEl.value = cfg.ollama_model || '';
  } catch (_) {}
  await loadAiSystemInfo();
}

async function loadAiSystemInfo() {
  try {
    const info = await api('/api/ai/system-info');
    renderSystemResources(info);
    renderModelLibrary(info);
    populateModelDropdown(info.models || [], info.configured_model);
  } catch (e) {
    const g = document.getElementById('sysResGrid');
    if (g) g.innerHTML = `<div class="path-test-item"><span class="path-test-lbl">Error</span><span class="badge badge-n" style="color:var(--red)">${escHtml(e.message)}</span></div>`;
  }
}

function renderSystemResources(info) {
  const g = document.getElementById('sysResGrid');
  if (!g) return;

  const rows = [
    { label: 'RAM total',     val: info.ram_total_gb != null     ? `${info.ram_total_gb} GB`     : 'N/A' },
    { label: 'RAM available', val: info.ram_available_gb != null ? `${info.ram_available_gb} GB` : 'N/A',
      color: info.ram_available_gb != null ? (info.ram_available_gb < 4 ? 'var(--red)' : info.ram_available_gb < 8 ? 'var(--amber)' : 'var(--green)') : undefined },
    { label: 'CPU cores',     val: info.cpu_cores != null ? String(info.cpu_cores) : 'N/A' },
    { label: 'Ollama',        val: info.ollama_available ? 'Connected' : 'Not reachable',
      color: info.ollama_available ? 'var(--green)' : 'var(--red)' },
  ];

  if (info.gpu?.length) {
    info.gpu.forEach(gpu => {
      rows.push({ label: gpu.name, val: gpu.vram_free_mb != null ? `${Math.round(gpu.vram_free_mb / 1024)} GB free / ${Math.round(gpu.vram_total_mb / 1024)} GB` : 'GPU' });
    });
  }

  g.innerHTML = rows.map(r =>
    `<div class="path-test-item"><span class="path-test-lbl">${escHtml(r.label)}</span><span class="badge badge-n" style="${r.color ? `color:${r.color}` : ''}">${escHtml(r.val)}</span></div>`
  ).join('');

  const rec = info.recommendation;
  const recEl = document.getElementById('aiRecommendation');
  if (rec && recEl) {
    recEl.classList.remove('hidden');
    const tierColors = { tiny: 'var(--txt-3)', small: 'var(--blue)', medium: 'var(--green)', large: 'var(--amber)', xlarge: 'var(--red)' };
    document.getElementById('aiRecTier').textContent  = `${rec.tier.toUpperCase()} tier — up to ${rec.max_size_gb} GB`;
    document.getElementById('aiRecTier').style.color  = tierColors[rec.tier] || '';
    document.getElementById('aiRecDesc').textContent  = ` (${rec.description})`;
    document.getElementById('aiRecExamples').textContent = `  Recommended: ${rec.examples.join(', ')}`;
  }
}

function populateModelDropdown(models, currentModel) {
  const sel = document.getElementById('aiCfgModelSelect');
  const txt = document.getElementById('aiCfgModelText');
  if (!sel || !models.length) return;

  sel.innerHTML = models.map(m =>
    `<option value="${escHtml(m.name)}" ${m.name === currentModel ? 'selected' : ''}>${escHtml(m.name)} (${m.size_gb} GB)</option>`
  ).join('');
  sel.style.display = '';
  if (txt) txt.style.display = 'none';
}

function onAiModelSelectChange() {
  const sel = document.getElementById('aiCfgModelSelect');
  const txt = document.getElementById('aiCfgModelText');
  if (sel && txt) txt.value = sel.value;
}

function renderModelLibrary(info) {
  const wrap = document.getElementById('modelLibraryWrap');
  if (!wrap) return;
  const models = info.models || [];
  if (!models.length) {
    wrap.innerHTML = '<p class="muted" style="font-size:.85rem;padding:.25rem 0;">No models pulled yet. Use the form below to pull one.</p>';
    return;
  }
  const fitIcon = { ok: '✓', warn: '⚠', 'too-large': '✗' };
  const fitColor = { ok: 'var(--green)', warn: 'var(--amber)', 'too-large': 'var(--red)' };
  const running = info.running_models || [];

  wrap.innerHTML = `<table class="u-table"><thead>
    <tr><th>Model</th><th>Size</th><th>Fit</th><th>Status</th><th>Actions</th></tr>
  </thead><tbody>${models.map(m => {
    const fit = m.fit || 'ok';
    const isRunning = running.includes(m.name);
    const isCurrent = m.name === info.configured_model;
    return `<tr>
      <td><strong>${escHtml(m.name)}</strong>${isCurrent ? ' <span class="badge badge-b" style="font-size:.65rem;">active</span>' : ''}</td>
      <td><span class="badge badge-n">${m.size_gb} GB</span></td>
      <td><span class="badge" style="color:${fitColor[fit] || ''};background:transparent;">${fitIcon[fit] || ''} ${fit}</span></td>
      <td>${isRunning ? '<span class="badge badge-g" style="font-size:.7rem;">loaded</span>' : '<span class="muted" style="font-size:.78rem;">—</span>'}</td>
      <td style="display:flex;gap:.3rem;flex-wrap:wrap;">
        <button class="btn btn-g btn-sm" onclick="selectAiModel('${escHtml(m.name)}')">Use</button>
        <button class="btn btn-g btn-sm" style="color:var(--red)" onclick="deleteAiModel('${escHtml(m.name)}')">Delete</button>
      </td>
    </tr>`;
  }).join('')}</tbody></table>`;
}

async function selectAiModel(name) {
  const txtEl = document.getElementById('aiCfgModelText');
  const selEl = document.getElementById('aiCfgModelSelect');
  if (txtEl) txtEl.value = name;
  if (selEl) { for (const opt of selEl.options) { if (opt.value === name) opt.selected = true; } }
  showToast(`Model set to "${name}" — click Save & Apply`, 'info');
}

async function deleteAiModel(name) {
  if (!confirm(`Delete model "${name}"? This removes it from disk and cannot be undone.`)) return;
  try {
    const r = await api(`/api/ai/models/${encodeURIComponent(name)}`, 'DELETE');
    showToast(r.ok ? `Model "${name}" deleted` : (r.error || 'Delete failed'), r.ok ? 'ok' : 'err');
    await loadAiSystemInfo();
  } catch (e) {
    showToast(e.message, 'err');
  }
}

async function saveAiConfig() {
  try {
    const current = await api('/api/config');
    const urlVal = document.getElementById('aiCfgUrl')?.value?.trim();
    const modelVal = document.getElementById('aiCfgModelText')?.value?.trim()
      || document.getElementById('aiCfgModelSelect')?.value?.trim();
    const payload = { ...current, ollama_url: urlVal || current.ollama_url, ollama_model: modelVal || current.ollama_model };
    await api('/api/config', 'POST', payload);
    showToast('AI config saved', 'ok');
    await loadAiSystemInfo();
  } catch (e) {
    showToast(e.message, 'err');
  }
}

async function testAiConnection() {
  const btn = document.getElementById('testConnBtn');
  const resEl = document.getElementById('testConnResult');
  const grid = document.getElementById('testConnGrid');
  if (btn) btn.classList.add('loading');
  if (resEl) resEl.classList.remove('hidden');
  if (grid) grid.innerHTML = '<div class="path-test-item" style="grid-column:1/-1;"><span class="path-test-lbl">Status</span><span class="badge badge-n">Testing — may take up to 2 min on first load…</span></div>';

  try {
    const r = await api('/api/ai/test-connection', 'POST', {});
    if (!grid) return;

    const rows = r.ok
      ? [
          { label: 'Status',       val: 'OK',                    color: 'var(--green)' },
          { label: 'Model',        val: r.model || '—' },
          { label: 'Response',     val: r.response || '—' },
          { label: 'Model load',   val: r.load_duration_ms != null ? `${r.load_duration_ms} ms` : '—' },
          { label: 'Inference',    val: r.eval_duration_ms  != null ? `${r.eval_duration_ms} ms`  : '—' },
          { label: 'Round-trip',   val: r.total_ms != null ? `${r.total_ms} ms` : '—' },
        ]
      : [
          { label: 'Status', val: 'Failed', color: 'var(--red)' },
          { label: 'Model',  val: r.model || '—' },
          { label: 'Error',  val: r.error || '—', color: 'var(--red)' },
          ...(r.available_models?.length ? [{ label: 'Available models', val: r.available_models.join(', ') }] : []),
        ];

    grid.innerHTML = rows.map(i =>
      `<div class="path-test-item"><span class="path-test-lbl">${escHtml(i.label)}</span><span class="badge badge-n" style="${i.color ? `color:${i.color}` : ''}">${escHtml(String(i.val))}</span></div>`
    ).join('');

    showToast(r.ok ? `Connection OK (${r.total_ms} ms)` : `Test failed: ${r.error}`, r.ok ? 'ok' : 'err');
  } catch (e) {
    if (grid) grid.innerHTML = `<div class="path-test-item"><span class="path-test-lbl">Error</span><span class="badge badge-n" style="color:var(--red)">${escHtml(e.message)}</span></div>`;
    showToast(e.message, 'err');
  } finally {
    if (btn) btn.classList.remove('loading');
  }
}

async function pullModelFromAiTab() {
  const name = document.getElementById('pullModelNameInput')?.value?.trim();
  if (!name) { showToast('Enter a model name', 'err'); return; }

  const btn = document.getElementById('pullModelBtnAi');
  const progWrap = document.getElementById('pullProgressAi');
  const fill = document.getElementById('pullProgressFillAi');
  const status = document.getElementById('pullProgressStatusAi');

  if (btn) btn.classList.add('loading');
  if (progWrap) progWrap.classList.remove('hidden');
  if (fill) { fill.style.width = '5%'; fill.style.background = ''; }
  if (status) status.textContent = `Pulling "${name}" — this may take several minutes…`;
  setText('pullResultAi', '', '');

  try {
    const { job_id } = await api('/api/ai/models/pull', 'POST', { model: name });
    const poll = setInterval(async () => {
      const job = await api(`/api/ai/jobs/${job_id}`);
      if (fill) fill.style.width = job.status === 'pulling' ? '40%' : '100%';
      if (job.status !== 'pulling') {
        clearInterval(poll);
        if (btn) btn.classList.remove('loading');
        const ok = job.status === 'done' && job.result?.ok;
        if (fill) fill.style.background = ok ? 'var(--green)' : 'var(--red)';
        if (status) status.textContent = ok ? `"${name}" ready` : `Pull failed: ${job.result?.error || 'unknown error'}`;
        showToast(ok ? `Model "${name}" pulled` : `Pull failed`, ok ? 'ok' : 'err');
        setText('pullResultAi', ok ? `"${name}" pulled successfully` : (job.result?.error || 'error'), ok ? 'ok' : 'err');
        if (ok) {
          if (document.getElementById('pullModelNameInput')) document.getElementById('pullModelNameInput').value = '';
          await loadAiSystemInfo();
        }
      }
    }, 3000);
  } catch (e) {
    if (btn) btn.classList.remove('loading');
    showToast(e.message, 'err');
    setText('pullResultAi', e.message, 'err');
  }
}

async function pullModel() {
  const btn = document.getElementById('pullModelBtn');
  if (btn) btn.classList.add('loading');
  setText('aiPullResult', 'Pulling model — this may take several minutes…', 'info');
  try {
    const { job_id, model } = await api('/api/ai/models/pull', 'POST', {});
    const poll = setInterval(async () => {
      const job = await api(`/api/ai/jobs/${job_id}`);
      if (job.status !== 'pulling') {
        clearInterval(poll);
        if (btn) btn.classList.remove('loading');
        if (job.status === 'done') {
          showToast(`Model "${model}" ready`, 'ok');
          setText('aiPullResult', `Model "${model}" pulled successfully`, 'ok');
          await loadAiStatus();
        } else {
          setText('aiPullResult', job.result?.error || 'Pull failed', 'err');
        }
      }
    }, 3000);
  } catch (e) {
    if (btn) btn.classList.remove('loading');
    showToast(e.message, 'err');
    setText('aiPullResult', e.message, 'err');
  }
}

// ── SSL ────────────────────────────────────────────────────────────
async function loadSslStatus() {
  try {
    const r = await api('/api/ssl/status');
    const el = document.getElementById('sslStatus');
    if (!el) return;
    if (!r.configured) {
      el.innerHTML = '<p class="muted" style="font-size:.875rem;">No certificate installed. Generate or upload one below.</p>';
      return;
    }
    if (r.error) {
      el.innerHTML = `<p class="feedback err">${escHtml(r.error)}</p><p class="muted" style="font-size:.78rem;">Path: ${escHtml(r.cert_path)}</p>`;
      return;
    }
    const notAfter = r.not_after ? new Date(r.not_after) : null;
    const expired = notAfter && notAfter < new Date();
    const daysLeft = notAfter ? Math.ceil((notAfter - new Date()) / 86400000) : null;
    el.innerHTML = [
      { label: 'Subject',   val: r.subject ?? '—' },
      { label: 'Issuer',    val: r.issuer  ?? '—' },
      { label: 'Valid from', val: r.not_before ? r.not_before.slice(0, 10) : '—' },
      { label: 'Valid until', val: r.not_after  ? r.not_after.slice(0, 10)  : '—', color: expired ? 'var(--red)' : daysLeft !== null && daysLeft < 30 ? 'var(--amber)' : 'var(--green)' },
      { label: 'Days left',  val: daysLeft != null ? String(daysLeft) : '—', color: expired ? 'var(--red)' : daysLeft !== null && daysLeft < 30 ? 'var(--amber)' : undefined },
      { label: 'Cert path',  val: r.cert_path ?? '—' },
      { label: 'Key exists', val: r.key_exists ? 'Yes' : 'No', color: r.key_exists ? 'var(--green)' : 'var(--red)' },
    ].map(i => `<div class="path-test-item"><span class="path-test-lbl">${escHtml(i.label)}</span><span class="badge badge-n" style="${i.color ? `color:${i.color}` : ''}">${escHtml(i.val)}</span></div>`).join('');
  } catch (e) {
    setText('sslStatus', e.message, 'err');
  }
}

async function generateCert() {
  const btn = document.getElementById('sslGenBtn');
  if (btn) btn.classList.add('loading');
  try {
    const sanRaw = document.getElementById('sslSAN')?.value || '';
    const san_hosts = sanRaw.split(',').map(s => s.trim()).filter(Boolean);
    const r = await api('/api/ssl/generate', 'POST', {
      common_name: document.getElementById('sslCN')?.value?.trim() || 'seekr.local',
      org:         document.getElementById('sslOrg')?.value?.trim() || 'Seekr',
      country:     (document.getElementById('sslCountry')?.value?.trim() || 'DE').slice(0, 2).toUpperCase(),
      days:        Number(document.getElementById('sslDays')?.value || 365),
      san_hosts,
    });
    showToast('Certificate generated', 'ok');
    setText('sslGenResult', `Certificate saved to ${r.cert_path}. Valid until ${(r.not_after || '').slice(0, 10)}.`, 'ok');
    await loadSslStatus();
  } catch (e) {
    showToast(e.message, 'err');
    setText('sslGenResult', e.message, 'err');
  } finally {
    if (btn) btn.classList.remove('loading');
  }
}

async function uploadCert() {
  const certFile = document.getElementById('sslCertFile')?.files?.[0];
  const keyFile  = document.getElementById('sslKeyFile')?.files?.[0];
  if (!certFile || !keyFile) { showToast('Select both certificate and key files', 'err'); return; }
  const btn = document.getElementById('sslUploadBtn');
  if (btn) btn.classList.add('loading');
  try {
    const fd = new FormData();
    fd.append('cert_file', certFile);
    fd.append('key_file', keyFile);
    const res = await fetch('/api/ssl/upload', { method: 'POST', headers: { 'X-Auth-Token': token ?? '' }, body: fd });
    if (!res.ok) { const t = await res.text(); throw new Error(t); }
    const r = await res.json();
    showToast('Certificate installed', 'ok');
    setText('sslUploadResult', `Installed: ${r.cert_path}`, 'ok');
    await loadSslStatus();
  } catch (e) {
    showToast(e.message, 'err');
    setText('sslUploadResult', e.message, 'err');
  } finally {
    if (btn) btn.classList.remove('loading');
  }
}

// ── Home Assistant connection test ────────────────────────────────
async function testHaConnection() {
  const key = document.getElementById('haTestKey')?.value?.trim();
  if (!key) { showToast('Paste a key to test', 'err'); return; }
  setText('haTestResult', 'Testing…', 'info');
  try {
    const res = await fetch('/api/ha/test', { headers: { 'X-Api-Key': key } });
    const data = await res.json();
    if (data.connected) {
      setText('haTestResult',
        `Connected — key: "${data.key_label}", scope: ${data.path_filter || 'all folders'}, ${data.documents} documents, Seekr ${data.app_version}`,
        'ok');
    } else {
      setText('haTestResult', `Not connected: ${data.error || 'unknown error'}`, 'err');
    }
  } catch (e) {
    setText('haTestResult', `Request failed: ${e.message}`, 'err');
  }
}

// ── Home Assistant key management ─────────────────────────────────
let _haNewKey = null;
const _haKeyStore = {};   // id → key record, for safe onclick lookups

async function loadHaKeys() {
  try {
    const keys = await api('/api/ha/keys');
    renderHaKeysTable(keys);
  } catch (e) {
    setText('haKeysResult', e.message, 'err');
  }
}

function renderHaKeysTable(keys) {
  const el = document.getElementById('haKeyTable');
  if (!el) return;
  // Store records for safe onclick lookups (avoids JS injection from label/key values)
  Object.keys(_haKeyStore).forEach(k => delete _haKeyStore[k]);
  keys.forEach(k => { _haKeyStore[k.id] = k; });
  if (!keys.length) {
    el.innerHTML = '<p class="muted" style="font-size:.82rem;">No keys yet. Create one below.</p>';
    return;
  }
  el.innerHTML = `<table class="u-table"><thead><tr>
    <th>Label</th><th>Path filter</th><th>Key (preview)</th><th>Description</th><th>Created</th><th></th>
  </tr></thead><tbody>${
    keys.map(k => `<tr>
      <td><strong>${escHtml(k.label)}</strong></td>
      <td><code style="font-size:.78rem;">${escHtml(k.path_filter || '—')}</code></td>
      <td><code style="font-size:.75rem;color:var(--txt-3);">${escHtml((k.key || '').slice(0, 8))}…</code>
          <button class="btn btn-g btn-sm" style="margin-left:.35rem;" onclick="prefillHaYamlById('${escHtml(k.id)}')">Use</button>
      </td>
      <td style="font-size:.78rem;color:var(--txt-3);">${escHtml(k.description || '—')}</td>
      <td class="muted" style="font-size:.75rem;">${escHtml((k.created_at || '').slice(0, 10))}</td>
      <td><button class="btn btn-g btn-sm" style="color:var(--red)" onclick="deleteHaKey('${escHtml(k.id)}')">Delete</button></td>
    </tr>`).join('')
  }</tbody></table>`;
}

function prefillHaYamlById(id) {
  const k = _haKeyStore[id];
  if (k) prefillHaYaml(k.key, k.label);
}

async function createHaKey() {
  const label = document.getElementById('haKeyLabel')?.value?.trim();
  const path_filter = document.getElementById('haKeyPath')?.value?.trim();
  const description = document.getElementById('haKeyDesc')?.value?.trim() || '';
  if (!label) { showToast('Enter a label', 'err'); return; }
  if (!path_filter) { showToast('Enter a path filter', 'err'); return; }
  try {
    const k = await api('/api/ha/keys', 'POST', { label, path_filter, description });
    showToast(`Key "${label}" created`, 'ok');
    _haNewKey = k.key;
    const card = document.getElementById('haNewKeyCard');
    const valEl = document.getElementById('haNewKeyValue');
    if (card) card.classList.remove('hidden');
    if (valEl) valEl.textContent = k.key;
    prefillHaYaml(k.key, k.label);
    setText('haCreateResult', '', '');
    if (document.getElementById('haKeyLabel')) document.getElementById('haKeyLabel').value = '';
    if (document.getElementById('haKeyPath')) document.getElementById('haKeyPath').value = '';
    if (document.getElementById('haKeyDesc')) document.getElementById('haKeyDesc').value = '';
    await loadHaKeys();
  } catch (e) {
    showToast(e.message, 'err');
    setText('haCreateResult', e.message, 'err');
  }
}

async function deleteHaKey(id) {
  if (!confirm('Delete this API key? Any Home Assistant automations using it will stop working.')) return;
  try {
    await api(`/api/ha/keys/${id}`, 'DELETE');
    showToast('Key deleted', 'ok');
    await loadHaKeys();
  } catch (e) {
    showToast(e.message, 'err');
  }
}

function copyHaKey() {
  if (!_haNewKey) return;
  navigator.clipboard?.writeText(_haNewKey).then(() => showToast('Key copied to clipboard', 'ok'));
}

function prefillHaYaml(key, label) {
  const keyEl = document.getElementById('haYamlKey');
  if (keyEl) keyEl.value = key;
  renderHaYaml();
}

function renderHaYaml() {
  const host = (document.getElementById('haYamlHost')?.value?.trim() || 'https://seekr.yourdomain.local').replace(/\/$/, '');
  const key = document.getElementById('haYamlKey')?.value?.trim() || 'YOUR_API_KEY_HERE';
  const out = document.getElementById('haYamlOut');
  if (!out) return;
  out.textContent = `# ── Home Assistant configuration.yaml snippet ──────────────────────────
# Seekr document search integration
# Paste into configuration.yaml (or split across packages)

input_text:
  seekr_query:
    name: "Seekr Search"
    initial: ""
    max: 255

rest_command:
  seekr_search:
    url: "${host}/api/ha/search"
    method: POST
    headers:
      Content-Type: application/json
      X-Api-Key: "${key}"
    payload: >-
      {"query": "{{ query }}", "limit": 5}

# Lovelace card (Entities card):
#   - entity: input_text.seekr_query
#     name: Document search

# Automation that shows results as a notification:
automation:
  - alias: "Seekr: show search results"
    trigger:
      platform: state
      entity_id: input_text.seekr_query
    condition:
      condition: template
      value_template: "{{ trigger.to_state.state | length > 2 }}"
    action:
      - service: rest_command.seekr_search
        data:
          query: "{{ states('input_text.seekr_query') }}"
        response_variable: r
      - service: persistent_notification.create
        data:
          title: "Seekr: {{ states('input_text.seekr_query') }}"
          message: >
            {% if r.content.answer %}{{ r.content.answer }}{% endif %}

            Found {{ r.content.count }} result(s) in {{ r.content.path_filter or "all folders" }}

            {% for s in r.content.sources %}
            • {{ s.filename }} ({{ s.modified_at }})
            {% endfor %}`;
}

function copyHaYaml() {
  const out = document.getElementById('haYamlOut');
  if (!out?.textContent) { showToast('Generate YAML first', 'err'); return; }
  navigator.clipboard?.writeText(out.textContent).then(() => showToast('YAML copied to clipboard', 'ok'));
}

// ── Nav & bootstrap ────────────────────────────────────────────────
function initNav() {
  const map = { home: '/', search: '/search', ingest: '/ingest', config: '/config', wiki: '/wiki' };
  const activeHref = map[document.body?.dataset?.page || ''];
  document.querySelectorAll('.nav-links a').forEach(link => {
    if (link.getAttribute('href') === activeHref) link.classList.add('active');
  });
}

function initSearchPage() {
  const queryEl = document.getElementById('query');
  if (!queryEl) return;

  // Restore query from URL param
  const q = new URLSearchParams(location.search).get('q');
  if (q) queryEl.value = q;

  // / shortcut to focus search
  document.addEventListener('keydown', e => {
    const tag = document.activeElement?.tagName;
    if (e.key === '/' && tag !== 'INPUT' && tag !== 'TEXTAREA') {
      e.preventDefault(); queryEl.focus();
    }
  });

  // Enter to run search
  queryEl.addEventListener('keydown', e => { if (e.key === 'Enter') runSearch(); });
}

async function bootstrap() {
  initNav();
  renderRecentSearches();
  initDropZone();

  if (document.body?.dataset?.page === 'search') {
    initSearchPage();
  }

  if (token) {
    showAuthedPanels();
    const role = localStorage.getItem('documentSearchRole') || 'user';
    if (role === 'admin') showAdminUI();
    await loadStatus();
    if (document.getElementById('configPanel')) await loadConfig();
    if (document.body?.dataset?.page === 'search') {
      await loadTagCloud();
      const q = new URLSearchParams(location.search).get('q');
      if (q) await runSearch();
    }
  }
}

bootstrap();

// ── Tag cloud & tag filter ─────────────────────────────────────────
async function loadTagCloud() {
  const card = document.getElementById('tagCloudCard');
  const cloud = document.getElementById('tagCloud');
  if (!card || !cloud) return;
  try {
    const tags = await api('/api/tags');
    if (!tags.length) { card.classList.add('hidden'); return; }
    card.classList.remove('hidden');
    cloud.innerHTML = tags.map(t =>
      `<button class="tag-chip" onclick="filterByTag('${escHtml(t.name)}')" title="${t.count} document${t.count !== 1 ? 's' : ''}">
        ${escHtml(t.name)}<span class="tag-chip-count">${t.count}</span>
      </button>`
    ).join('');
  } catch (_) {}
}

function filterByTag(name) {
  const input = document.getElementById('tagFilterInput');
  if (input) input.value = name;
  const filterBody = document.getElementById('filterBody');
  if (filterBody?.classList.contains('hidden')) toggleFilters();
  runSearch();
}

function clearTagFilter() {
  const input = document.getElementById('tagFilterInput');
  if (input) input.value = '';
  runSearch();
}

// ── Reindex from search results ────────────────────────────────────
async function reindexDocumentFromSearch(documentId) {
  try {
    const r = await api(`/api/documents/${documentId}/reindex`, 'POST', {});
    showToast(`Reindexed — ${r.blocks} block${r.blocks !== 1 ? 's' : ''} extracted`, 'ok');
  } catch (e) {
    showToast(`Reindex failed: ${e.message}`, 'err');
  }
}

// ── Reindex by ID (ingest page) ────────────────────────────────────
async function reindexDocument() {
  const idVal = document.getElementById('reindexDocId')?.value?.trim();
  if (!idVal) { showToast('Enter a document ID', 'err'); return; }
  const btn = document.getElementById('reindexBtn');
  if (btn) btn.classList.add('loading');
  try {
    const r = await api(`/api/documents/${idVal}/reindex`, 'POST', {});
    showToast(`Document ${idVal} reindexed`, 'ok');
    setText('reindexResult', `Status: ${r.extraction_status} · Blocks: ${r.blocks}`, 'ok');
  } catch (e) {
    showToast(e.message, 'err');
    setText('reindexResult', e.message, 'err');
  } finally {
    if (btn) btn.classList.remove('loading');
  }
}

// ── Index cleanup ──────────────────────────────────────────────────
async function runIndexCleanup() {
  const btn = document.getElementById('cleanupBtn');
  if (btn) btn.classList.add('loading');
  try {
    const r = await api('/api/index/cleanup', 'POST', {});
    showToast(r.removed > 0 ? `Removed ${r.removed} stale entries` : 'Index is clean — nothing removed', 'ok');
    setText('cleanupResult', `Removed ${r.removed} missing-file entries`, r.removed > 0 ? 'ok' : 'info');
  } catch (e) {
    showToast(e.message, 'err');
    setText('cleanupResult', e.message, 'err');
  } finally {
    if (btn) btn.classList.remove('loading');
  }
}

// ── AI: Folder structure suggestions ──────────────────────────────
async function startStructureSuggestion() {
  const btn = document.getElementById('structureStartBtn');
  const progWrap = document.getElementById('structureProgress');
  const fill = document.getElementById('structureProgressFill');
  const status = document.getElementById('structureProgressStatus');
  const resultEl = document.getElementById('structureResult');

  if (btn) btn.classList.add('loading');
  if (progWrap) progWrap.classList.remove('hidden');
  if (resultEl) resultEl.classList.add('hidden');
  if (fill) { fill.style.width = '10%'; fill.style.background = ''; }
  if (status) status.textContent = 'Analysing your document corpus…';

  const sampleSize = Number(document.getElementById('structureSampleSize')?.value || 50);
  try {
    const { job_id } = await api(`/api/ai/suggest-structure?sample_size=${sampleSize}`, 'POST', {});
    let ticks = 0;
    const poll = setInterval(async () => {
      try {
        const job = await api(`/api/ai/jobs/${job_id}`);
        ticks++;
        const pct = Math.min(10 + ticks * 8, 85);
        if (fill) fill.style.width = `${pct}%`;

        if (job.status === 'finished') {
          clearInterval(poll);
          if (btn) btn.classList.remove('loading');
          const r = job.result;
          if (!r?.ok) {
            if (fill) { fill.style.width = '100%'; fill.style.background = 'var(--red)'; }
            if (status) status.textContent = `Error: ${r?.error || 'unknown'}`;
            showToast(`Structure suggestion failed: ${r?.error}`, 'err');
            return;
          }
          if (fill) { fill.style.width = '100%'; fill.style.background = 'var(--green)'; }
          if (status) status.textContent = `Done — ${r.suggested_structure?.length || 0} folder suggestions`;
          renderStructureResult(r);
          if (resultEl) resultEl.classList.remove('hidden');
          showToast(`${r.suggested_structure?.length || 0} folder suggestions ready`, 'ok');
        }
      } catch (_) {}
    }, 2000);
  } catch (e) {
    showToast(e.message, 'err');
    if (btn) btn.classList.remove('loading');
  }
}

function renderStructureResult(r) {
  const ratEl = document.getElementById('structureRationale');
  const listEl = document.getElementById('structureFolderList');
  if (ratEl) ratEl.textContent = r.rationale || '';
  if (!listEl) return;

  const folders = r.suggested_structure || [];
  if (!folders.length) {
    listEl.innerHTML = '<p class="muted" style="font-size:.85rem;">No folders suggested.</p>';
    return;
  }
  listEl.innerHTML = `<div class="structure-tree">${folders.map(f => `
    <div class="structure-folder">
      <div class="structure-folder-name">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="14" height="14"><path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/></svg>
        <code style="font-size:.82rem;">${escHtml(f.folder || '—')}</code>
      </div>
      <p class="structure-folder-desc">${escHtml(f.description || '')}</p>
      ${f.examples?.length ? `<div class="structure-folder-examples">${f.examples.map(e => `<span class="badge badge-n" style="font-size:.72rem;">${escHtml(e)}</span>`).join(' ')}</div>` : ''}
    </div>`).join('')}</div>`;
}
