let token = localStorage.getItem('documentSearchToken');

async function api(path, method = 'GET', body = null) {
  const headers = { 'X-Auth-Token': token ?? '' };
  if (body !== null) headers['Content-Type'] = 'application/json';
  const res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : null });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `Request failed (${res.status})`);
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
    setText('loginResult', '', '');
    showToast(`Signed in as ${data.username}`, 'ok');
    showAuthedPanels();
    await loadStatus();
    if (document.getElementById('configPanel')) await loadConfig();
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

async function uploadDocument() {
  const f = uploadFile.files[0];
  if (!f) { showToast('Select a file first', 'err'); return; }
  const btn = document.getElementById('uploadBtn');
  if (btn) btn.classList.add('loading');
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
  } catch (e) {
    showToast(e.message, 'err');
    uploadResult.textContent = e.message;
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

async function runUpdate() {
  const btn = document.getElementById('runUpdateBtn');
  if (btn) btn.classList.add('loading');
  try {
    await api('/api/update/run', 'POST', {});
    showToast('System update started', 'ok');
  } catch (e) {
    showToast(e.message, 'err');
  } finally {
    if (btn) btn.classList.remove('loading');
  }
}

// ── Config ─────────────────────────────────────────────────────────
async function loadConfig() {
  try {
    const c = await api('/api/config');
    cfgDb.value = c.database_path ?? '';
    cfgExt.value = (c.supported_extensions || []).join(', ');
    cfgExcludeDirs.value = (c.exclude_dirs || []).join(', ');
    cfgExcludePatterns.value = (c.exclude_patterns || []).join(', ');
    cfgMaxSize.value = c.max_file_size_mb ?? 100;
  } catch (e) {
    setText('configResult', e.message, 'err');
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
    };
    await api('/api/config', 'POST', payload);
    showToast('Configuration saved', 'ok');
    setText('configResult', 'Saved successfully', 'ok');
  } catch (e) {
    showToast(e.message, 'err');
    setText('configResult', e.message, 'err');
  }
}

// ── Nav & bootstrap ────────────────────────────────────────────────
function initNav() {
  const map = { home: '/', search: '/search', ingest: '/ingest', config: '/config' };
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
    await loadStatus();
    if (document.getElementById('configPanel')) await loadConfig();
    // Auto-run search if URL has ?q=
    if (document.body?.dataset?.page === 'search') {
      const q = new URLSearchParams(location.search).get('q');
      if (q) await runSearch();
    }
  }
}

bootstrap();
