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
let chipFiletype, chipTagFilter, chipUploadTags;
const _resultTagChips = {};

// ── User preferences (server-backed, localStorage-cached) ──────────
// Mirror of the server-side DEFAULT_PREFERENCES. `userPrefs` is seeded
// from the offline cache merged over these defaults so theme/page-size
// apply instantly before the network round-trip resolves.
const DEFAULT_PREFS = {
  theme: 'system',
  results_per_page: 25,
  default_filters: { filetype: [], tags: [], path: '', block_type: '' },
};

function _loadCachedPrefs() {
  try {
    const raw = localStorage.getItem('seekr_prefs');
    if (!raw) return { ...DEFAULT_PREFS };
    const cached = JSON.parse(raw) || {};
    return {
      ...DEFAULT_PREFS,
      ...cached,
      default_filters: { ...DEFAULT_PREFS.default_filters, ...(cached.default_filters || {}) },
    };
  } catch (_) {
    return { ...DEFAULT_PREFS };
  }
}

let userPrefs = _loadCachedPrefs();

// Apply a theme to <html>. 'system' means "follow the OS" — we express
// that by REMOVING the data-theme attribute (never data-theme="system").
function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === 'light' || theme === 'dark') {
    root.setAttribute('data-theme', theme);
  } else {
    root.removeAttribute('data-theme');
  }
}

// Flash-free theme: paint the cached theme at parse time, before the
// network round-trip (and before most of the page renders), so a reload
// in dark mode never flashes the light palette. Uses the same offline
// cache (`userPrefs`) — no competing theme store.
applyTheme(userPrefs.theme);

// Merge `prefs` into userPrefs, persist to the offline cache, apply the
// theme, and (if on the search page with chips constructed) pre-fill the
// filter chips/inputs from default_filters. Idempotent — safe to call
// repeatedly (e.g. once for theme, once after chips exist).
function applyPreferences(prefs) {
  if (!prefs) return;
  userPrefs = {
    ...DEFAULT_PREFS,
    ...userPrefs,
    ...prefs,
    default_filters: {
      ...DEFAULT_PREFS.default_filters,
      ...(userPrefs.default_filters || {}),
      ...(prefs.default_filters || {}),
    },
  };
  try { localStorage.setItem('seekr_prefs', JSON.stringify(userPrefs)); } catch (_) {}

  applyTheme(userPrefs.theme);

  // Reflect results-per-page in the optional select, if present.
  const rpp = document.getElementById('resultsPerPage');
  if (rpp) rpp.value = String(userPrefs.results_per_page);

  // Pre-fill search filters from defaults when the chips exist.
  const df = userPrefs.default_filters || {};
  if (chipFiletype && Array.isArray(df.filetype)) chipFiletype.setValues(df.filetype);
  if (chipTagFilter && Array.isArray(df.tags)) chipTagFilter.setValues(df.tags);
  const pathEl = document.getElementById('pathFilter');
  if (pathEl && typeof df.path === 'string') pathEl.value = df.path;
  const blockEl = document.getElementById('blockType');
  if (blockEl && typeof df.block_type === 'string') blockEl.value = df.block_type;
}

// Apply the cached theme immediately, then refresh from the server.
// Errors are swallowed so the offline cache keeps working.
async function hydratePreferences() {
  applyTheme(userPrefs.theme);
  try {
    const me = await api('/api/me');
    if (me?.preferences) applyPreferences(me.preferences);
  } catch (_) {}
}

// Optimistically apply a partial patch, then persist it. On failure the
// optimistic value stays in the local cache (toast informs the user).
async function savePreferences(patch) {
  applyPreferences({ ...userPrefs, ...patch });
  try {
    const saved = await api('/api/preferences', 'PUT', patch);
    if (saved) applyPreferences(saved);
  } catch (_) {
    showToast('Preference saved locally — could not reach server', 'info');
  }
}

// Idempotently bind the optional theme toggle + results-per-page select.
// Both are guarded so this is a harmless no-op when the controls are
// absent (the theming UI / select may not have landed yet).
function bindPreferenceControls() {
  const themeBtn = document.getElementById('themeToggle');
  if (themeBtn && !themeBtn.dataset.bound) {
    themeBtn.dataset.bound = '1';
    themeBtn.addEventListener('click', () => {
      const order = ['system', 'light', 'dark'];
      const next = order[(order.indexOf(userPrefs.theme) + 1) % order.length];
      savePreferences({ theme: next });
    });
  }

  const rpp = document.getElementById('resultsPerPage');
  if (rpp && !rpp.dataset.bound) {
    rpp.dataset.bound = '1';
    rpp.value = String(userPrefs.results_per_page);
    rpp.addEventListener('change', () => {
      const n = Number(rpp.value) || DEFAULT_PREFS.results_per_page;
      savePreferences({ results_per_page: n });
    });
  }
}

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

// ── Loading skeletons & error states ───────────────────────────────
// Result-card skeletons for the search results region.
function skeletonResults(n = 3) {
  let html = '';
  for (let i = 0; i < n; i++) {
    html += `<div class="rc" aria-hidden="true">
      <div class="skeleton skeleton-line skeleton-w-40"></div>
      <div class="skeleton skeleton-line skeleton-w-80"></div>
      <div class="skeleton skeleton-line skeleton-w-60"></div>
    </div>`;
  }
  return html;
}

// Plain shimmer lines for tables/grids (users, etc.).
function skeletonLines(n = 3) {
  let html = '';
  for (let i = 0; i < n; i++) {
    html += '<div class="skeleton skeleton-line skeleton-w-80" aria-hidden="true"></div>';
  }
  return html;
}

// Inline error panel (announced — role="alert").
function errorState(message) {
  return `<div class="error-state" role="alert">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
    <p>${escHtml(message)}</p>
  </div>`;
}

// ── Toast notifications ────────────────────────────────────────────
function showToast(msg, type = 'info', duration = 3500) {
  const wrap = document.getElementById('toastWrap');
  if (!wrap) return;
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  // Errors are assertive ('alert'); everything else is polite ('status').
  // The #toastWrap is the aria-live region; this role sharpens the cue.
  t.setAttribute('role', type === 'err' ? 'alert' : 'status');
  const dot = document.createElement('div');
  dot.className = 'toast-dot';
  const span = document.createElement('span');
  span.textContent = msg;   // textContent → exact, safe announce text
  t.appendChild(dot);
  t.appendChild(span);
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
    await hydratePreferences();
    if (data.role === 'admin') showAdminUI();
    await loadStatus();
    if (document.getElementById('configPanel')) await loadConfig();
    if (document.body?.dataset?.page === 'search') {
      chipFiletype = new ChipInput(
        document.getElementById('filetypeWrap'),
        document.getElementById('filetypeInput'),
        document.getElementById('filetypeList'),
      );
      chipTagFilter = new ChipInput(
        document.getElementById('tagFilterWrap'),
        document.getElementById('tagFilterInput'),
        document.getElementById('tagFilterList'),
      );
      await loadFilterOptions();
      await loadTagCloud();
      await hydratePreferences();
      await renderRecentSearches();
      await renderSavedSearches();
    }
    if (document.body?.dataset?.page === 'ingest') {
      chipUploadTags = new ChipInput(
        document.getElementById('uploadTagsWrap'),
        document.getElementById('uploadTagsInput'),
        document.getElementById('uploadTagList'),
      );
      initDropZone();
      await loadIngestOptions();
    }
  } catch (error) {
    setText('loginResult', `Login failed: ${error.message}`, 'err');
  }
}

// ── Recent searches & saved searches (server-backed) ───────────────
// Capture the current search filter state in the same shape the backend
// stores it ({filetype, path, block_type, modified_from, modified_to, tags}).
// Reads the SAME refs as _currentSearchPayload() so a captured filter set
// re-runs identically.
function captureFilters() {
  return {
    filetype: chipFiletype?.values().join(',') || null,
    path: document.getElementById('pathFilter')?.value || null,
    block_type: document.getElementById('blockType')?.value || null,
    modified_from: document.getElementById('modifiedFrom')?.value || null,
    modified_to: document.getElementById('modifiedTo')?.value || null,
    tags: chipTagFilter?.values() ?? [],
  };
}

// Restore a query + filters into the search form, reveal the filter panel
// if any filter is set, then run the search via the canonical entry point.
function applyFilters(query, filters) {
  const queryEl = document.getElementById('query');
  if (!queryEl) {
    // No search form on this page (e.g. dashboard) — navigate to /search.
    location.href = `/search?q=${encodeURIComponent(query ?? '')}`;
    return;
  }
  filters = filters || {};
  queryEl.value = query ?? '';

  const pathEl = document.getElementById('pathFilter');
  if (pathEl) pathEl.value = filters.path || '';
  const blockEl = document.getElementById('blockType');
  if (blockEl) blockEl.value = filters.block_type || '';
  const fromEl = document.getElementById('modifiedFrom');
  if (fromEl) fromEl.value = filters.modified_from || '';
  const toEl = document.getElementById('modifiedTo');
  if (toEl) toEl.value = filters.modified_to || '';

  const fileTypes = filters.filetype
    ? String(filters.filetype).split(',').map(s => s.trim()).filter(Boolean)
    : [];
  chipFiletype?.setValues(fileTypes);
  chipTagFilter?.setValues(Array.isArray(filters.tags) ? filters.tags : []);

  const hasFilter = fileTypes.length || (filters.tags?.length) ||
    filters.path || filters.block_type || filters.modified_from || filters.modified_to;
  const filterBody = document.getElementById('filterBody');
  if (hasFilter && filterBody?.classList.contains('hidden')) toggleFilters();

  runSearch();
}

async function renderRecentSearches() {
  const el = document.getElementById('recentSearches');
  if (!el) return;
  if (!token) {
    el.innerHTML = '<p class="muted" style="font-size:.82rem;">No recent searches yet.</p>';
    return;
  }
  let history;
  try {
    history = await api('/api/search/history');
  } catch (_) { return; }
  if (!history || !history.length) {
    el.innerHTML = '<p class="muted" style="font-size:.82rem;">No recent searches yet.</p>';
    return;
  }
  el.replaceChildren();
  const list = document.createElement('div');
  list.className = 'recent-list';
  history.forEach(item => {
    const a = document.createElement('a');
    a.className = 'recent-item';
    a.href = `/search?q=${encodeURIComponent(item.query || '')}`;
    a.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="13" height="13"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>${escHtml(item.query || '(empty)')}`;
    if (document.getElementById('query')) {
      a.addEventListener('click', e => {
        e.preventDefault();
        applyFilters(item.query, item.filters);
      });
    }
    list.appendChild(a);
  });
  el.appendChild(list);
}

async function saveCurrentSearch() {
  const queryEl = document.getElementById('query');
  const name = prompt('Name this saved search:');
  if (name === null) return;
  const trimmed = name.trim();
  if (!trimmed) { showToast('Enter a name for the saved search', 'err'); return; }
  try {
    await api('/api/search/saved', 'POST', {
      name: trimmed,
      query: queryEl?.value || '',
      filters: captureFilters(),
    });
    showToast('Search saved', 'ok');
    await renderSavedSearches();
  } catch (e) {
    showToast(e.message, 'err');
  }
}

async function renderSavedSearches() {
  const card = document.getElementById('savedSearchCard');
  const list = document.getElementById('savedSearchList');
  if (!card || !list) return;
  if (!token) { card.classList.add('hidden'); return; }
  let saved;
  try {
    saved = await api('/api/search/saved');
  } catch (_) { return; }
  if (!saved || !saved.length) {
    card.classList.add('hidden');
    list.replaceChildren();
    return;
  }
  card.classList.remove('hidden');
  list.replaceChildren();
  saved.forEach(item => {
    const chip = document.createElement('span');
    chip.className = 'tag-chip';
    chip.title = item.query ? `Query: ${item.query}` : 'Saved search';

    const label = document.createElement('span');
    label.textContent = item.name;
    label.style.cursor = 'pointer';
    label.addEventListener('click', () => applyFilters(item.query, item.filters));
    chip.appendChild(label);

    const x = document.createElement('span');
    x.className = 'chip-x';
    x.textContent = '×';
    x.style.marginLeft = '.4rem';
    x.addEventListener('click', ev => { ev.stopPropagation(); deleteSavedSearch(item.id, item.name); });
    chip.appendChild(x);

    list.appendChild(chip);
  });
}

async function deleteSavedSearch(id, name) {
  if (!confirm(`Delete saved search "${name}"?`)) return;
  try {
    await api(`/api/search/saved/${id}`, 'DELETE');
    showToast('Saved search deleted', 'ok');
    await renderSavedSearches();
  } catch (e) {
    showToast(e.message, 'err');
  }
}

// ── Search ─────────────────────────────────────────────────────────
function toggleFilters() {
  const body = document.getElementById('filterBody');
  const btn = document.getElementById('filterToggle');
  if (!body || !btn) return;
  const willOpen = body.classList.contains('hidden');
  body.classList.toggle('hidden', !willOpen);
  btn.classList.toggle('open', willOpen);
  btn.setAttribute('aria-expanded', String(willOpen));
  body.setAttribute('aria-hidden', String(!willOpen));
  btn.querySelector('.ft-lbl').textContent = willOpen ? 'Hide filters' : 'Show filters';
}

function clearSearch() {
  const q = document.getElementById('query');
  if (q) { q.value = ''; q.focus(); }
  chipFiletype?.clear();
  chipTagFilter?.clear();
  const metaEl = document.getElementById('resultsMeta');
  if (metaEl) metaEl.textContent = '';
  const resultsEl = document.getElementById('results');
  if (resultsEl) resultsEl.innerHTML = '';
  _searchState = { offset: 0, hasMore: false, total: 0, loading: false };
  _updateLoadMore();
}

// ── Pagination state + header-reading fetch helper ─────────────────
const PAGE_SIZE = 25;
let _searchState = { offset: 0, hasMore: false, total: 0, loading: false };

function _currentSearchPayload(offset) {
  return {
    query: query.value,
    limit: userPrefs.results_per_page || PAGE_SIZE,
    offset,
    filetype: chipFiletype?.values().join(',') || null,
    path: pathFilter.value || null,
    block_type: blockType.value || null,
    modified_from: modifiedFrom.value || null,
    modified_to: modifiedTo.value || null,
    tags: chipTagFilter?.values() ?? [],
  };
}

async function _fetchSearchPage(offset) {
  const res = await fetch('/api/search', {
    method: 'POST',
    headers: { 'X-Auth-Token': token ?? '', 'Content-Type': 'application/json' },
    body: JSON.stringify(_currentSearchPayload(offset)),
  });
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
  const docs = await res.json();
  const total = Number(res.headers.get('X-Total-Count') ?? docs.length);
  const totalApprox = (res.headers.get('X-Total-Approx') ?? '').toLowerCase() === 'true';
  const hasMore = (res.headers.get('X-Has-More') ?? '').toLowerCase() === 'true';
  const nextOffset = Number(res.headers.get('X-Next-Offset') ?? (offset + docs.length));
  return { docs, total, totalApprox, hasMore, nextOffset };
}

function _updateLoadMore() {
  const wrap = document.getElementById('loadMoreWrap');
  const btn = document.getElementById('loadMoreBtn');
  if (!wrap) return;
  wrap.style.display = _searchState.hasMore ? '' : 'none';
  if (btn) {
    btn.disabled = _searchState.loading;
    btn.textContent = _searchState.loading ? 'Loading…' : 'Load more';
  }
}

async function runSearch() {
  const resultsEl = document.getElementById('results');
  // Announce the fresh-search load and show placeholder skeletons while the
  // first page is in flight. aria-busy is cleared on every exit path below.
  if (resultsEl) {
    resultsEl.setAttribute('aria-busy', 'true');
    resultsEl.innerHTML = skeletonResults(3);
  }
  try {
    _searchState.loading = true;
    _updateLoadMore();
    const { docs, total, totalApprox, hasMore, nextOffset } = await _fetchSearchPage(0);
    // History is recorded server-side by /api/search; refresh the list.
    renderRecentSearches();
    // Remember the current filter set as the user's default filters
    // (fire-and-forget; failures fall back to the local cache).
    savePreferences({
      default_filters: {
        filetype: chipFiletype?.values() ?? [],
        tags: chipTagFilter?.values() ?? [],
        path: document.getElementById('pathFilter')?.value || '',
        block_type: document.getElementById('blockType')?.value || '',
      },
    });

    _searchState = { offset: nextOffset, hasMore, total, loading: false };

    const metaEl = document.getElementById('resultsMeta');
    if (metaEl) {
      metaEl.textContent = !total
        ? ''
        : `${total}${totalApprox ? '+' : ''} result${(total !== 1 || totalApprox) ? 's' : ''}`;
    }

    if (!docs.length) {
      resultsEl.innerHTML = `
        <div class="empty">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
          </svg>
          <p>No results found for this query.</p>
        </div>`;
      resultsEl.removeAttribute('aria-busy');
      _updateLoadMore();
      return;
    }

    renderResults(docs, false);
    if (resultsEl) resultsEl.removeAttribute('aria-busy');
    _updateLoadMore();
  } catch (e) {
    _searchState = { offset: 0, hasMore: false, total: 0, loading: false };
    _updateLoadMore();
    if (resultsEl) {
      resultsEl.removeAttribute('aria-busy');
      resultsEl.innerHTML = errorState(e.message);
    }
  }
}

async function loadMoreResults() {
  if (_searchState.loading || !_searchState.hasMore) return;
  try {
    _searchState.loading = true;
    _updateLoadMore();
    const { docs, hasMore, nextOffset, total } = await _fetchSearchPage(_searchState.offset);
    _searchState = { offset: nextOffset, hasMore, total, loading: false };
    renderResults(docs, true);
    _updateLoadMore();
  } catch (e) {
    _searchState.loading = false;
    _updateLoadMore();
    showToast(e.message, 'err');
  }
}

async function saveTags(documentId) {
  const chip = _resultTagChips[documentId];
  if (!chip) { showToast('Tag editor not ready', 'err'); return; }
  const tags = chip.values();
  await api('/api/documents/tags', 'POST', { document_id: documentId, tags });
  showToast('Tags saved', 'ok');
}

async function toggleMark(documentId, current) {
  await api('/api/documents/mark', 'POST', { document_id: documentId, is_marked: !current });
  showToast(current ? 'Unmarked' : 'Marked', 'ok');
}

function buildHitEl(hit) {
  const div = document.createElement('div');
  div.className = 'rc-hit';

  const label = document.createElement('span');
  label.className = 'rc-hit-label';
  label.textContent = `${hit.block_type} ${hit.block_number}`;

  const body = document.createElement('span');
  if (hit.snippet_html) {
    body.insertAdjacentHTML('beforeend', hit.snippet_html);
  } else {
    body.textContent = '—';
  }

  div.appendChild(label);
  div.appendChild(body);
  return div;
}

const HITS_SHOW_MAX = 5;

function renderResults(docs, append = false) {
  const el = document.getElementById('results');
  if (!el) return;
  if (!append) {
    el.replaceChildren();
    for (const k in _resultTagChips) delete _resultTagChips[k];
  }

  docs.forEach(doc => {
    const card = document.createElement('div');
    card.className = 'rc';

    // Head: filename link + star button
    const head = document.createElement('div');
    head.className = 'rc-head';
    const nameLink = document.createElement('a');
    nameLink.className = 'rc-name';
    nameLink.href = doc.open_url;          // keep a real href for middle-click/copy
    nameLink.textContent = doc.filename;
    nameLink.addEventListener('click', (e) => {
      e.preventDefault();
      openPreview({
        documentId: doc.document_id,
        filename: doc.filename,
        kind: doc.preview_kind,
        previewUrl: doc.preview_url,
        previewTextUrl: doc.preview_text_url,
        openUrl: doc.open_url,
      });
    });
    const starBtn = document.createElement('button');
    starBtn.className = 'star-btn' + (doc.is_marked ? ' marked' : '');
    starBtn.title = doc.is_marked ? 'Unmark' : 'Mark';
    starBtn.textContent = '★';
    starBtn.addEventListener('click', () => toggleMark(doc.document_id, doc.is_marked));
    head.appendChild(nameLink);
    head.appendChild(starBtn);
    card.appendChild(head);

    // Badges: extension, hit count, tags
    const badges = document.createElement('div');
    badges.className = 'rc-badges';
    const extBadge = document.createElement('span');
    extBadge.className = 'badge badge-n';
    extBadge.textContent = doc.extension;
    badges.appendChild(extBadge);
    const hitsBadge = document.createElement('span');
    hitsBadge.className = 'badge badge-n';
    hitsBadge.textContent = `${doc.hit_count} hit${doc.hit_count !== 1 ? 's' : ''}`;
    badges.appendChild(hitsBadge);
    doc.tags.forEach(t => {
      const chip = document.createElement('span');
      chip.className = 'tag-chip';
      chip.textContent = t;
      chip.addEventListener('click', () => filterByTag(t));
      badges.appendChild(chip);
    });
    card.appendChild(badges);

    // Path
    const pathEl = document.createElement('div');
    pathEl.className = 'rc-path';
    pathEl.textContent = doc.path;
    card.appendChild(pathEl);

    // Hits list
    const hitsEl = document.createElement('div');
    hitsEl.className = 'rc-hits';
    hitsEl.id = `hits-${doc.document_id}`;
    doc.hits.slice(0, HITS_SHOW_MAX).forEach(h => hitsEl.appendChild(buildHitEl(h)));
    card.appendChild(hitsEl);

    const extra = doc.hits.slice(HITS_SHOW_MAX);
    if (extra.length) {
      const moreBtn = document.createElement('button');
      moreBtn.className = 'rc-more-btn';
      moreBtn.textContent = `Show ${extra.length} more hit${extra.length !== 1 ? 's' : ''}`;
      moreBtn.addEventListener('click', () => {
        extra.forEach(h => hitsEl.appendChild(buildHitEl(h)));
        moreBtn.remove();
      });
      card.appendChild(moreBtn);
    }

    // Footer: chip tag editor + action buttons
    const foot = document.createElement('div');
    foot.className = 'rc-foot';

    const tagWrap = document.createElement('div');
    tagWrap.className = 'chip-wrap';
    tagWrap.id = `tagWrap-${doc.document_id}`;
    const tagInput = document.createElement('input');
    tagInput.className = 'chip-input';
    tagInput.id = `tagInput-${doc.document_id}`;
    tagInput.placeholder = 'add tag…';
    tagInput.setAttribute('list', 'globalTagList');
    tagInput.setAttribute('autocomplete', 'off');
    tagWrap.appendChild(tagInput);

    const saveBtn = document.createElement('button');
    saveBtn.className = 'btn btn-g btn-sm';
    saveBtn.textContent = 'Save tags';
    saveBtn.addEventListener('click', () => saveTags(doc.document_id));

    const markBtn = document.createElement('button');
    markBtn.className = 'btn btn-g btn-sm';
    markBtn.textContent = doc.is_marked ? 'Unmark' : 'Mark';
    markBtn.addEventListener('click', () => toggleMark(doc.document_id, doc.is_marked));

    const reindexBtn = document.createElement('button');
    reindexBtn.className = 'btn btn-g btn-sm';
    reindexBtn.textContent = 'Reindex';
    reindexBtn.addEventListener('click', () => reindexDocumentFromSearch(doc.document_id));

    const previewBtn = document.createElement('button');
    previewBtn.className = 'btn btn-p btn-sm';
    previewBtn.textContent = 'Preview';
    previewBtn.addEventListener('click', () => openPreview({
      documentId: doc.document_id,
      filename: doc.filename,
      kind: doc.preview_kind,
      previewUrl: doc.preview_url,
      previewTextUrl: doc.preview_text_url,
      openUrl: doc.open_url,
    }));

    const openLink = document.createElement('a');
    openLink.className = 'btn btn-g btn-sm';
    openLink.href = doc.open_url;
    openLink.target = '_blank';
    openLink.textContent = 'Open file';

    foot.appendChild(tagWrap);
    foot.appendChild(saveBtn);
    foot.appendChild(markBtn);
    foot.appendChild(reindexBtn);
    foot.appendChild(previewBtn);
    foot.appendChild(openLink);
    card.appendChild(foot);

    el.appendChild(card);

    // Init per-card ChipInput after DOM insertion
    requestAnimationFrame(() => {
      const wrap = document.getElementById(`tagWrap-${doc.document_id}`);
      const inp = document.getElementById(`tagInput-${doc.document_id}`);
      if (wrap && inp) {
        _resultTagChips[doc.document_id] = new ChipInput(wrap, inp, null);
        _resultTagChips[doc.document_id].setValues(doc.tags);
      }
    });
  });

  // Ensure shared datalist for tag autocomplete
  if (!document.getElementById('globalTagList')) {
    const dl = document.createElement('datalist');
    dl.id = 'globalTagList';
    document.body.appendChild(dl);
    api('/api/tags').then(tags => {
      if (!tags) return;
      tags.forEach(t => {
        const o = document.createElement('option');
        o.value = t.name || String(t);
        dl.appendChild(o);
      });
    }).catch(() => {});
  }
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
  fd.append('tags', chipUploadTags?.values().join(',') || '');
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
  const selectedPaths = Array.from(
    document.getElementById('pathsSelect')?.selectedOptions ?? []
  ).map(o => o.value).filter(Boolean);
  if (!selectedPaths.length) {
    showToast('Select at least one folder to index.', 'err');
    return;
  }
  const btn = document.getElementById('startIndexBtn');
  const progressWrap = document.getElementById('indexProgress');
  const progressFill = document.getElementById('indexProgressFill');
  const progressStatus = document.getElementById('indexProgressStatus');
  try {
    if (btn) btn.classList.add('loading');
    if (progressWrap) progressWrap.classList.remove('hidden');
    if (progressFill) { progressFill.style.width = '5%'; progressFill.style.background = ''; }
    if (progressStatus) progressStatus.textContent = 'Starting job…';

    const force = !!(document.getElementById('idxForce') && idxForce.checked);
    const data = await api('/api/index/start', 'POST', { paths: selectedPaths, force });
    _trackIndexJob(data.job_id, btn, progressFill, progressStatus);
  } catch (e) {
    showToast(e.message, 'err');
    if (btn) btn.classList.remove('loading');
  }
}

// Drive the shared index-progress UI for a running index_paths job.
function _trackIndexJob(id, btn, progressFill, progressStatus) {
  if (progressStatus) progressStatus.textContent = `Job ${id} started`;
  let pct = 10;
  const interval = setInterval(async () => {
    try {
      const j = await api(`/api/index/jobs/${id}`);
      if (progressStatus) {
        progressStatus.textContent =
          `${j.status} — found: ${j.found ?? 0}, indexed: ${j.indexed ?? 0}, updated: ${j.updated ?? 0}, skipped: ${j.skipped ?? 0}, errors: ${j.errors ?? 0}`;
      }
      if (j.status === 'finished' || j.status === 'failed' || j.status === 'interrupted') {
        clearInterval(interval);
        if (btn) btn.classList.remove('loading');
        const ok = j.status === 'finished';
        if (progressFill) {
          progressFill.style.width = '100%';
          progressFill.style.background = ok ? 'var(--green)' : 'var(--red)';
        }
        showToast(ok ? 'Indexing complete' : 'Indexing failed', ok ? 'ok' : 'err');
      } else {
        pct = Math.min(pct + 7, 88);
        if (progressFill) progressFill.style.width = `${pct}%`;
      }
    } catch (_) {}
  }, 1200);
}

// Global reindex: force re-extract every configured source path in one job.
async function reindexAll() {
  if (!confirm('Re-extract ALL configured source paths (force)? This re-runs extraction on every indexed file and can take a while.')) {
    return;
  }
  const btn = document.getElementById('reindexAllBtn');
  const progressWrap = document.getElementById('indexProgress');
  const progressFill = document.getElementById('indexProgressFill');
  const progressStatus = document.getElementById('indexProgressStatus');
  try {
    if (btn) btn.classList.add('loading');
    if (progressWrap) progressWrap.classList.remove('hidden');
    if (progressFill) { progressFill.style.width = '5%'; progressFill.style.background = ''; }
    if (progressStatus) progressStatus.textContent = 'Starting full reindex…';
    const data = await api('/api/index/reindex-all', 'POST', {});
    _trackIndexJob(data.job_id, btn, progressFill, progressStatus);
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
    const ocr = c.ocr || {};
    if (document.getElementById('cfgOcrEnabled')) cfgOcrEnabled.checked = !!ocr.enabled;
    if (document.getElementById('cfgOcrLang')) cfgOcrLang.value = (ocr.languages || ['deu', 'eng']).join('+');
    if (document.getElementById('cfgOcrForce')) cfgOcrForce.checked = !!ocr.force_ocr;
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
      ocr: {
        enabled: !!(document.getElementById('cfgOcrEnabled') && cfgOcrEnabled.checked),
        languages: (cfgOcrLang?.value || 'deu+eng').split('+').map(s => s.trim()).filter(Boolean),
        force_ocr: !!(document.getElementById('cfgOcrForce') && cfgOcrForce.checked),
      },
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
  if (name === 'access') loadAccessTab();
  if (name === 'ssl') loadSslStatus();
  if (name === 'ai') loadAiTabData();
  if (name === 'ha') loadHaKeys();
  if (name === 'audit') { _auditOffset = 0; loadAudit(); }
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
  const el = document.getElementById('userTable');
  if (el) {
    el.setAttribute('aria-busy', 'true');
    el.innerHTML = skeletonLines(4);
  }
  try {
    const users = await api('/api/users');
    renderUserTable(users);
  } catch (e) {
    setText('usersResult', e.message, 'err');
    if (el) el.innerHTML = errorState(e.message);
  } finally {
    if (el) el.removeAttribute('aria-busy');
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

// ── Audit log ──────────────────────────────────────────────────────
let _auditOffset = 0;
let _auditTotal = 0;
const _AUDIT_LIMIT = 50;

function _auditFilters() {
  const params = new URLSearchParams();
  params.set('limit', String(_AUDIT_LIMIT));
  params.set('offset', String(_auditOffset));
  const action = document.getElementById('auditActionFilter')?.value;
  if (action) params.set('action', action);
  const from = document.getElementById('auditFrom')?.value;
  if (from) params.set('date_from', `${from}T00:00:00+00:00`);
  const to = document.getElementById('auditTo')?.value;
  if (to) params.set('date_to', `${to}T23:59:59+00:00`);
  return params;
}

async function loadAudit() {
  try {
    const data = await api(`/api/audit?${_auditFilters().toString()}`);
    _auditTotal = data.total || 0;
    renderAuditTable(data.items || []);
    updateAuditPager();
    setText('auditResult', '', '');
  } catch (e) {
    setText('auditResult', e.message, 'err');
  }
}

function auditFirstPage() {
  _auditOffset = 0;
  loadAudit();
}

function auditPage(dir) {
  const next = _auditOffset + dir * _AUDIT_LIMIT;
  if (next < 0 || next >= _auditTotal) return;
  _auditOffset = next;
  loadAudit();
}

function updateAuditPager() {
  const prev = document.getElementById('auditPrev');
  const next = document.getElementById('auditNext');
  if (prev) prev.disabled = _auditOffset <= 0;
  if (next) next.disabled = _auditOffset + _AUDIT_LIMIT >= _auditTotal;
  const summary = document.getElementById('auditSummary');
  if (summary) {
    if (_auditTotal === 0) {
      summary.textContent = '0 entries';
    } else {
      const from = _auditOffset + 1;
      const to = Math.min(_auditOffset + _AUDIT_LIMIT, _auditTotal);
      summary.textContent = `${from}–${to} of ${_auditTotal}`;
    }
  }
}

function _auditTarget(row) {
  if (!row.target_type) return '—';
  return row.target_id != null ? `${row.target_type}#${row.target_id}` : String(row.target_type);
}

function _auditDetail(row) {
  if (row.detail == null) return '—';
  try {
    return JSON.stringify(row.detail);
  } catch (_) {
    return String(row.detail);
  }
}

function renderAuditTable(items) {
  const el = document.getElementById('auditTable');
  if (!el) return;
  if (!items.length) {
    el.innerHTML = '<p class="muted" style="font-size:.82rem;">No audit entries match the current filters.</p>';
    return;
  }
  el.innerHTML = `<table class="u-table"><thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Target</th><th>IP</th><th>Detail</th></tr></thead><tbody>${
    items.map(row => `<tr>
      <td class="muted" style="font-size:.78rem;white-space:nowrap;">${escHtml((row.created_at || '').replace('T', ' ').slice(0, 19))}</td>
      <td>${escHtml(row.actor_username || (row.actor_user_id != null ? `#${row.actor_user_id}` : '—'))}</td>
      <td><span class="badge badge-n">${escHtml(row.action || '')}</span></td>
      <td class="muted" style="font-size:.8rem;">${escHtml(_auditTarget(row))}</td>
      <td class="muted" style="font-size:.78rem;">${escHtml(row.ip || '—')}</td>
      <td class="muted" style="font-size:.78rem;word-break:break-all;">${escHtml(_auditDetail(row))}</td>
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

// ── Access (ACL) ───────────────────────────────────────────────────

let _accessUsersCache = [];
let _accessGroupsCache = [];

async function loadAccessTab() {
  try {
    const [users, groups] = await Promise.all([api('/api/users'), api('/api/groups')]);
    _accessUsersCache = users;
    _accessGroupsCache = groups;
    renderGroupTable(groups);
    populateAclPrincipalSelect(users, groups);
  } catch (e) {
    setText('groupsResult', e.message, 'err');
  }
}

function renderGroupTable(groups) {
  const el = document.getElementById('groupTable');
  if (!el) return;
  if (!groups.length) {
    el.innerHTML = '<p class="muted" style="font-size:.82rem;">No groups yet.</p>';
    return;
  }
  el.innerHTML = `<table class="u-table"><thead><tr><th>ID</th><th>Name</th><th>Members</th><th>Actions</th></tr></thead><tbody>${
    groups.map(g => {
      const isPublic = g.external_id === 'public';
      return `<tr>
        <td class="muted">${g.id}</td>
        <td><strong>${escHtml(g.display_name || g.external_id)}</strong> <span class="muted">(${escHtml(g.external_id)})</span></td>
        <td class="muted">${g.member_count ?? 0}</td>
        <td>
          <button class="btn btn-g btn-sm" onclick="openGroupMembers(${g.id}, '${escHtml(g.display_name || g.external_id)}')">Members</button>
          ${isPublic ? '' : `<button class="btn btn-g btn-sm" style="color:var(--red)" onclick="deleteGroup(${g.id})">Delete</button>`}
        </td>
      </tr>`;
    }).join('')
  }</tbody></table>`;
}

async function createGroup() {
  const name = document.getElementById('newGroupName')?.value?.trim();
  const display = document.getElementById('newGroupLabel')?.value?.trim() || null;
  if (!name) { showToast('Group name required', 'err'); return; }
  try {
    await api('/api/groups', 'POST', { name, display_name: display });
    showToast(`Group "${name}" created`, 'ok');
    if (document.getElementById('newGroupName')) document.getElementById('newGroupName').value = '';
    if (document.getElementById('newGroupLabel')) document.getElementById('newGroupLabel').value = '';
    await loadAccessTab();
  } catch (e) {
    showToast(e.message, 'err');
    setText('groupsResult', e.message, 'err');
  }
}

async function deleteGroup(groupId) {
  if (!confirm('Delete this group? Memberships and its grants are removed.')) return;
  try {
    await api(`/api/groups/${groupId}`, 'DELETE');
    showToast('Group deleted', 'ok');
    document.getElementById('groupMembersCard')?.classList.add('hidden');
    await loadAccessTab();
  } catch (e) {
    showToast(e.message, 'err');
    setText('groupsResult', e.message, 'err');
  }
}

async function openGroupMembers(groupId, label) {
  const card = document.getElementById('groupMembersCard');
  const title = document.getElementById('groupMembersTitle');
  const idInput = document.getElementById('selectedGroupId');
  if (idInput) idInput.value = String(groupId);
  if (title) title.textContent = `Members — ${label}`;
  if (card) card.classList.remove('hidden');
  await refreshGroupMembers(groupId);
  card?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function refreshGroupMembers(groupId) {
  try {
    const members = await api(`/api/groups/${groupId}/members`);
    renderGroupMemberTable(members);
    populateAddMemberSelect(members);
  } catch (e) {
    setText('groupMembersResult', e.message, 'err');
  }
}

function renderGroupMemberTable(members) {
  const el = document.getElementById('groupMemberTable');
  if (!el) return;
  if (!members.length) {
    el.innerHTML = '<p class="muted" style="font-size:.82rem;">No members.</p>';
    return;
  }
  const gid = document.getElementById('selectedGroupId')?.value;
  el.innerHTML = `<table class="u-table"><thead><tr><th>User</th><th>Role</th><th>Actions</th></tr></thead><tbody>${
    members.map(m => `<tr>
      <td><strong>${escHtml(m.username)}</strong></td>
      <td class="muted">${escHtml(m.role || 'user')}</td>
      <td><button class="btn btn-g btn-sm" style="color:var(--red)" onclick="removeGroupMember(${gid}, ${m.user_id})">Remove</button></td>
    </tr>`).join('')
  }</tbody></table>`;
}

function populateAddMemberSelect(members) {
  const sel = document.getElementById('addMemberSelect');
  if (!sel) return;
  const memberIds = new Set(members.map(m => m.user_id));
  const available = _accessUsersCache.filter(u => !memberIds.has(u.id));
  sel.innerHTML = available.length
    ? available.map(u => `<option value="${u.id}">${escHtml(u.username)}</option>`).join('')
    : '<option value="">— all users are members —</option>';
}

async function addGroupMember() {
  const gid = document.getElementById('selectedGroupId')?.value;
  const uid = document.getElementById('addMemberSelect')?.value;
  if (!gid || !uid) { showToast('Select a user', 'err'); return; }
  try {
    await api(`/api/groups/${gid}/members`, 'POST', { user_id: Number(uid) });
    showToast('Member added', 'ok');
    await refreshGroupMembers(gid);
    await loadAccessTab();
    document.getElementById('selectedGroupId').value = gid;
  } catch (e) {
    showToast(e.message, 'err');
    setText('groupMembersResult', e.message, 'err');
  }
}

async function removeGroupMember(groupId, userId) {
  try {
    await api(`/api/groups/${groupId}/members/${userId}`, 'DELETE');
    showToast('Member removed', 'ok');
    await refreshGroupMembers(groupId);
  } catch (e) {
    showToast(e.message, 'err');
    setText('groupMembersResult', e.message, 'err');
  }
}

function populateAclPrincipalSelect(users, groups) {
  const sel = document.getElementById('aclPrincipalSelect');
  if (!sel) return;
  const groupOpts = groups.map(g =>
    `<option value="${g.id}">Group: ${escHtml(g.display_name || g.external_id)}</option>`).join('');
  sel.innerHTML = groupOpts || '<option value="">— no groups —</option>';
}

async function loadDocumentAcl() {
  const docId = document.getElementById('aclDocId')?.value;
  if (!docId) { showToast('Enter a document ID', 'err'); return; }
  try {
    const entries = await api(`/api/acl/documents/${docId}`);
    renderDocAclTable(docId, entries);
  } catch (e) {
    showToast(e.message, 'err');
    setText('docAclResult', e.message, 'err');
    const el = document.getElementById('docAclTable');
    if (el) el.innerHTML = '';
  }
}

function renderDocAclTable(docId, entries) {
  const el = document.getElementById('docAclTable');
  if (!el) return;
  if (!entries.length) {
    el.innerHTML = '<p class="muted" style="font-size:.82rem;">No ACL entries for this document.</p>';
    return;
  }
  el.innerHTML = `<table class="u-table"><thead><tr><th>Principal</th><th>Type</th><th>Permission</th><th>Actions</th></tr></thead><tbody>${
    entries.map(e => `<tr>
      <td><strong>${escHtml(e.display_name || e.external_id)}</strong></td>
      <td class="muted">${escHtml(e.principal_type)}</td>
      <td>${escHtml(e.permission)}</td>
      <td><button class="btn btn-g btn-sm" style="color:var(--red)" onclick="revokeDocumentAcl(${docId}, ${e.principal_id}, '${escHtml(e.permission)}')">Revoke</button></td>
    </tr>`).join('')
  }</tbody></table>`;
}

async function grantDocumentAcl() {
  const docId = document.getElementById('aclDocId')?.value;
  const principalId = document.getElementById('aclPrincipalSelect')?.value;
  const permission = document.getElementById('aclPermSelect')?.value;
  if (!docId || !principalId) { showToast('Document ID and principal required', 'err'); return; }
  try {
    await api('/api/acl/grant', 'POST', {
      document_id: Number(docId), principal_id: Number(principalId), permission,
    });
    showToast('Access granted', 'ok');
    await loadDocumentAcl();
  } catch (e) {
    showToast(e.message, 'err');
    setText('docAclResult', e.message, 'err');
  }
}

async function revokeDocumentAcl(docId, principalId, permission) {
  if (!confirm('Revoke this permission?')) return;
  try {
    await api('/api/acl/revoke', 'POST', {
      document_id: Number(docId), principal_id: Number(principalId), permission,
    });
    showToast('Access revoked', 'ok');
    await loadDocumentAcl();
  } catch (e) {
    showToast(e.message, 'err');
    setText('docAclResult', e.message, 'err');
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
  const map = { home: '/', search: '/search', ingest: '/ingest', config: '/config', jobs: '/jobs', wiki: '/wiki' };
  const activeHref = map[document.body?.dataset?.page || ''];
  document.querySelectorAll('.nav-links a').forEach(link => {
    if (link.getAttribute('href') === activeHref) link.classList.add('active');
  });
  bindPreferenceControls();
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

  // Escape: close the filter panel (and return focus to its toggle) if it's
  // open; otherwise blur the query box.
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    const body = document.getElementById('filterBody');
    const toggle = document.getElementById('filterToggle');
    if (body && !body.classList.contains('hidden')) {
      toggleFilters();
      toggle?.focus();
    } else if (document.activeElement === queryEl) {
      queryEl.blur();
    }
  });

  // Load more pager
  document.getElementById('loadMoreBtn')?.addEventListener('click', loadMoreResults);
}

async function bootstrap() {
  initNav();
  renderRecentSearches();

  // Global Escape: dismiss any open dismissable card (AI suggestion after
  // upload, or the change-password panel). No-ops when none are open.
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    const aiCard = document.getElementById('aiSuggestionCard');
    if (aiCard && !aiCard.classList.contains('hidden')) dismissAiSuggestion();
    const pwCard = document.getElementById('changePwCard');
    if (pwCard && !pwCard.classList.contains('hidden')) cancelChangePassword();
  });

  if (document.body?.dataset?.page === 'search') {
    initSearchPage();
  }

  if (token) {
    showAuthedPanels();
    await hydratePreferences();
    const role = localStorage.getItem('documentSearchRole') || 'user';
    if (role === 'admin') showAdminUI();
    await loadStatus();
    if (document.getElementById('configPanel')) await loadConfig();
    if (document.body?.dataset?.page === 'search') {
      chipFiletype = new ChipInput(
        document.getElementById('filetypeWrap'),
        document.getElementById('filetypeInput'),
        document.getElementById('filetypeList'),
      );
      chipTagFilter = new ChipInput(
        document.getElementById('tagFilterWrap'),
        document.getElementById('tagFilterInput'),
        document.getElementById('tagFilterList'),
      );
      await loadFilterOptions();
      await loadTagCloud();
      await hydratePreferences();
      await renderRecentSearches();
      await renderSavedSearches();
      const q = new URLSearchParams(location.search).get('q');
      if (q) await runSearch();
    }
    if (document.body?.dataset?.page === 'ingest') {
      chipUploadTags = new ChipInput(
        document.getElementById('uploadTagsWrap'),
        document.getElementById('uploadTagsInput'),
        document.getElementById('uploadTagList'),
      );
      initDropZone();
      await loadIngestOptions();
    }
    if (document.body?.dataset?.page === 'jobs') {
      document.getElementById('jobsPanel')?.classList.remove('hidden');
      await loadJobs();
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
  } catch (e) {
    card.classList.remove('hidden');
    cloud.innerHTML = errorState('Could not load tags');
  }
}

async function loadFilterOptions() {
  try {
    const [exts, tags] = await Promise.all([
      api('/api/index/extensions'),
      api('/api/tags'),
    ]);
    chipFiletype?.setOptions(exts || []);
    chipTagFilter?.setOptions((tags || []).map(t => t.name || String(t)));
  } catch (_) {}
}

async function loadIngestOptions() {
  try {
    const [tags, folders, sourceFolders] = await Promise.all([
      api('/api/tags'),
      api('/api/folders'),
      api('/api/source-folders'),
    ]);

    chipUploadTags?.setOptions((tags || []).map(t => t.name || String(t)));

    const folderList = document.getElementById('uploadFolderList');
    if (folderList) {
      (folders || []).forEach(f => {
        const o = document.createElement('option');
        o.value = f;
        folderList.appendChild(o);
      });
    }

    const sel = document.getElementById('pathsSelect');
    if (!sel) return;
    sel.replaceChildren();
    const roots = (sourceFolders || []).filter(f => f.is_root);
    if (!roots.length) {
      const opt = document.createElement('option');
      opt.disabled = true;
      opt.textContent = 'No source folders configured — add them in Config';
      sel.appendChild(opt);
    } else {
      roots.forEach(root => {
        const grp = document.createElement('optgroup');
        grp.label = root.label;
        const rootOpt = document.createElement('option');
        rootOpt.value = root.path;
        rootOpt.textContent = `${root.label} (root)`;
        grp.appendChild(rootOpt);
        sourceFolders
          .filter(f => !f.is_root && f.path.startsWith(root.path + '/'))
          .forEach(sub => {
            const opt = document.createElement('option');
            opt.value = sub.path;
            opt.textContent = sub.label;
            grp.appendChild(opt);
          });
        sel.appendChild(grp);
      });
    }
  } catch (_) {}
}

function filterByTag(name) {
  chipTagFilter?.setValues([name]);
  const filterBody = document.getElementById('filterBody');
  if (filterBody?.classList.contains('hidden')) toggleFilters();
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

// ── Duplicate documents ───────────────────────────────────────────
// Holds the most recently scanned groups so removeDuplicates() can look up
// the rendered documents by group key without re-querying the server.
let _duplicateGroups = {};

async function loadDuplicates() {
  const btn = document.getElementById('scanDuplicatesBtn');
  const listEl = document.getElementById('duplicatesList');
  if (btn) btn.classList.add('loading');
  if (listEl) listEl.innerHTML = '';
  _duplicateGroups = {};
  setText('duplicatesSummary', 'Scanning…', 'info');
  try {
    const data = await api('/api/documents/duplicates');
    const exact = Array.isArray(data.exact) ? data.exact : [];
    const content = Array.isArray(data.content) ? data.content : [];
    const total = exact.length + content.length;
    if (total === 0) {
      setText('duplicatesSummary', 'No duplicates found', 'ok');
      return;
    }
    setText(
      'duplicatesSummary',
      `Found ${exact.length} exact-file group(s) and ${content.length} content group(s)`,
      'info',
    );
    let html = '';
    html += renderDuplicateGroups(exact, 'exact', 'Exact file duplicates (identical bytes)');
    html += renderDuplicateGroups(content, 'content', 'Content duplicates (same extracted text)');
    if (listEl) listEl.innerHTML = html;
  } catch (e) {
    setText('duplicatesSummary', e.message, 'err');
    showToast(e.message, 'err');
  } finally {
    if (btn) btn.classList.remove('loading');
  }
}

function renderDuplicateGroups(groups, kind, heading) {
  if (!groups.length) return '';
  let html = `<h3 style="font-size:.9rem;margin:1rem 0 .5rem;">${escHtml(heading)}</h3>`;
  groups.forEach((group, idx) => {
    const groupKey = `${kind}-${idx}`;
    _duplicateGroups[groupKey] = group;
    const docs = Array.isArray(group.documents) ? group.documents : [];
    html += '<div class="card" style="margin-bottom:.75rem;padding:.75rem;">';
    html += `<p class="muted" style="font-size:.8rem;margin-bottom:.5rem;">${docs.length} copies · hash <code>${escHtml(String(group.hash).slice(0, 12))}</code></p>`;
    docs.forEach((d, di) => {
      const rid = `dup-${groupKey}-${d.id}`;
      const checked = di === 0 ? 'checked' : '';
      html += '<label for="' + rid + '" style="display:flex;align-items:center;gap:.5rem;padding:.25rem 0;cursor:pointer;font-size:.85rem;">';
      html += `<input type="radio" id="${rid}" name="${escHtml(groupKey)}" value="${d.id}" ${checked} />`;
      html += `<span>${escHtml(d.path)}</span>`;
      html += `<span class="muted" style="margin-left:auto;white-space:nowrap;">${formatBytes(d.file_size)} · #${d.id}</span>`;
      html += '</label>';
    });
    html += '<div class="btn-row" style="margin-top:.5rem;">';
    html += `<button class="btn btn-g" onclick="removeDuplicates('${groupKey}')">`;
    html += '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4h6v2"/></svg>';
    html += 'Keep selected, remove others</button>';
    html += '</div></div>';
  });
  return html;
}

async function removeDuplicates(groupKey) {
  const group = _duplicateGroups[groupKey];
  if (!group) return;
  const checked = document.querySelector(`input[name="${groupKey}"]:checked`);
  if (!checked) {
    showToast('Select a document to keep first', 'err');
    return;
  }
  const keepId = Number(checked.value);
  const docs = Array.isArray(group.documents) ? group.documents : [];
  const removeIds = docs.map(d => d.id).filter(id => id !== keepId);
  if (removeIds.length === 0) {
    showToast('Nothing to remove in this group', 'info');
    return;
  }
  if (!confirm(
    `Keep #${keepId} and remove ${removeIds.length} other entr${removeIds.length === 1 ? 'y' : 'ies'} from the index?\n\n`
    + 'This removes the entries from the search index only — the files on disk are NOT deleted.',
  )) return;
  try {
    const r = await api('/api/documents/duplicates/remove', 'POST', { keep_id: keepId, remove_ids: removeIds });
    showToast(`Removed ${r.removed} entr${r.removed === 1 ? 'y' : 'ies'}, kept #${r.kept}`, 'ok');
    await loadDuplicates();
  } catch (e) {
    showToast(e.message, 'err');
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

// ── Global Jobs dashboard ──────────────────────────────────────────
let _jobsPollTimer = null;

function _jobStateBadge(state) {
  const cls = {
    pending: 'badge-n', running: 'badge-b', succeeded: 'badge-g',
    failed: 'badge-a', interrupted: 'badge-a', cancelled: 'badge-n',
  }[state] || 'badge-n';
  return `<span class="badge ${cls}">${escHtml(state)}</span>`;
}

function _jobProgressText(job) {
  const p = job.progress || {};
  if (job.kind === 'index_paths') {
    return `done ${p.done ?? 0} (idx ${p.indexed ?? 0}, upd ${p.updated ?? 0}, skip ${p.skipped ?? 0}, err ${p.errors ?? 0})`;
  }
  if (job.kind === 'ai_reorganize') {
    return `${p.done ?? 0}/${p.total ?? 0}`;
  }
  if (job.kind === 'ai_pull') {
    const c = p.completed ?? 0, t = p.total ?? 0;
    const pct = t ? Math.round((c / t) * 100) : 0;
    return p.status ? `${escHtml(p.status)}${t ? ` ${pct}%` : ''}` : '—';
  }
  return p.status ? escHtml(String(p.status)) : '—';
}

function renderJobsTable(jobs) {
  const tbody = document.getElementById('jobsTbody');
  const summary = document.getElementById('jobsSummary');
  if (!tbody) return;
  if (!jobs.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="muted">No jobs yet.</td></tr>';
    if (summary) summary.textContent = '0 jobs';
    return;
  }
  const active = jobs.filter(j => j.state === 'pending' || j.state === 'running').length;
  if (summary) summary.textContent = `${jobs.length} jobs · ${active} active`;

  tbody.innerHTML = jobs.map(j => {
    const isActive = j.state === 'pending' || j.state === 'running';
    const isReenqueueable = j.state === 'interrupted' || j.state === 'failed' || j.state === 'cancelled';
    const actions = [];
    if (isActive) {
      const label = j.cancel_requested ? 'Cancelling…' : 'Cancel';
      const dis = j.cancel_requested ? 'disabled' : '';
      actions.push(`<button class="btn btn-danger btn-sm" ${dis} onclick="cancelJob(${j.id})">${label}</button>`);
    }
    if (isReenqueueable) {
      actions.push(`<button class="btn btn-g btn-sm" onclick="reEnqueueJob(${j.id})">Re-enqueue</button>`);
    }
    const created = (j.created_at || '').replace('T', ' ').slice(0, 19);
    return `<tr>
      <td>${j.id}</td>
      <td><code>${escHtml(j.kind)}</code></td>
      <td>${_jobStateBadge(j.state)}</td>
      <td class="muted" style="font-size:.8rem;">${_jobProgressText(j)}</td>
      <td>${j.retry_count}/${j.max_retries}</td>
      <td class="muted" style="font-size:.78rem;">${escHtml(created)}</td>
      <td>${actions.join(' ') || '—'}</td>
    </tr>`;
  }).join('');
}

async function loadJobs() {
  try {
    const jobs = await api('/api/jobs');
    renderJobsTable(jobs);
    const active = jobs.some(j => j.state === 'pending' || j.state === 'running');
    if (_jobsPollTimer) clearTimeout(_jobsPollTimer);
    if (active) _jobsPollTimer = setTimeout(loadJobs, 2500);
  } catch (e) {
    showToast(e.message, 'err');
  }
}

async function cancelJob(jobId) {
  try {
    const r = await api(`/api/jobs/${jobId}/cancel`, 'POST', {});
    showToast(r.outcome === 'cancelled' ? 'Job cancelled' : 'Cancellation requested', 'ok');
    await loadJobs();
  } catch (e) {
    showToast(e.message, 'err');
  }
}

async function reEnqueueJob(jobId) {
  try {
    const r = await api(`/api/jobs/${jobId}/re-enqueue`, 'POST', {});
    showToast(`Re-enqueued as job ${r.job_id}`, 'ok');
    await loadJobs();
  } catch (e) {
    showToast(e.message, 'err');
  }
}

// ── Document preview modal ─────────────────────────────────────────
const _pv = {
  el: null, body: null, title: null, download: null, closeBtn: null,
  pager: null, prevBtn: null, nextBtn: null, pageInfo: null,
  lastFocused: null, pdf: null, pageNum: 1, pageCount: 1, rendering: false,
};

function _pvInit() {
  if (_pv.el) return;
  _pv.el = document.getElementById('previewModal');
  if (!_pv.el) return;
  _pv.body = document.getElementById('pvBody');
  _pv.title = document.getElementById('pvTitle');
  _pv.download = document.getElementById('pvDownload');
  _pv.closeBtn = document.getElementById('pvClose');
  _pv.pager = document.getElementById('pvPager');
  _pv.prevBtn = document.getElementById('pvPrev');
  _pv.nextBtn = document.getElementById('pvNext');
  _pv.pageInfo = document.getElementById('pvPageInfo');

  _pv.closeBtn.addEventListener('click', closePreview);
  _pv.el.addEventListener('click', (e) => { if (e.target === _pv.el) closePreview(); });
  _pv.prevBtn.addEventListener('click', () => _pvGoto(_pv.pageNum - 1));
  _pv.nextBtn.addEventListener('click', () => _pvGoto(_pv.pageNum + 1));
  document.addEventListener('keydown', (e) => {
    if (_pv.el.classList.contains('hidden')) return;
    if (e.key === 'Escape') { e.preventDefault(); closePreview(); }
    else if (e.key === 'ArrowLeft' && !_pv.pager.classList.contains('hidden')) _pvGoto(_pv.pageNum - 1);
    else if (e.key === 'ArrowRight' && !_pv.pager.classList.contains('hidden')) _pvGoto(_pv.pageNum + 1);
  });
}

function _pvAuthHeaders() {
  // Match the rest of app.js: session token in X-Auth-Token.
  return token ? { 'X-Auth-Token': token } : {};
}

async function openPreview(info) {
  _pvInit();
  if (!_pv.el) return;
  _pv.lastFocused = document.activeElement;
  _pv.title.textContent = info.filename || 'Preview';
  _pv.download.href = info.openUrl || '#';
  _pv.download.setAttribute('download', info.filename || '');
  _pv.body.replaceChildren();
  _pv.pager.classList.add('hidden');
  _pv.pdf = null; _pv.pageNum = 1; _pv.pageCount = 1;

  _pv.el.classList.remove('hidden');
  _pv.el.setAttribute('aria-hidden', 'false');
  _pv.closeBtn.focus();

  const kind = info.kind || 'unsupported';
  try {
    if (kind === 'pdf')        await _pvRenderPdf(info);
    else if (kind === 'image') _pvRenderImage(info);
    else if (kind === 'text')  await _pvRenderText(info);
    else                       await _pvRenderUnsupported(info);
  } catch (err) {
    console.error('preview failed', err);
    _pvShowMessage('Could not render a preview. Use Download to open the file.');
  }
}

function closePreview() {
  if (!_pv.el) return;
  _pv.el.classList.add('hidden');
  _pv.el.setAttribute('aria-hidden', 'true');
  _pv.body.replaceChildren();
  _pv.pdf = null;
  if (_pv.lastFocused && typeof _pv.lastFocused.focus === 'function') _pv.lastFocused.focus();
}

function _pvShowMessage(msg) {
  const d = document.createElement('div');
  d.className = 'pv-empty';
  d.textContent = msg;
  _pv.body.replaceChildren(d);
}

// Authenticated fetch of a binary URL → object URL (so the <img>/PDF carry the token).
async function _pvFetchBlobUrl(url) {
  const r = await fetch(url, { headers: _pvAuthHeaders() });
  if (!r.ok) throw new Error('fetch failed: ' + r.status);
  const blob = await r.blob();
  return URL.createObjectURL(blob);
}

function _pvRenderImage(info) {
  // Images need the auth header; fetch as blob then show.
  _pvShowMessage('Loading image…');
  _pvFetchBlobUrl(info.previewUrl).then((objUrl) => {
    const img = document.createElement('img');
    img.alt = info.filename || 'image preview';
    img.src = objUrl;
    img.addEventListener('load', () => URL.revokeObjectURL(objUrl), { once: true });
    _pv.body.replaceChildren(img);
  }).catch(() => _pvShowMessage('Could not load image. Use Download.'));
}

async function _pvRenderText(info) {
  _pvShowMessage('Loading…');
  const r = await fetch(info.previewTextUrl, { headers: _pvAuthHeaders() });
  if (!r.ok) { _pvShowMessage('Could not load text.'); return; }
  const data = await r.json();
  const text = (data.blocks || []).map(b => b.text).join('\n\n');
  const ext = (data.extension || '').toLowerCase();
  const container = document.createElement('div');
  if (ext === '.md' || ext === '.markdown') {
    container.className = 'pv-md';
    container.innerHTML = _pvRenderMarkdown(text);
  } else {
    container.className = 'pv-text';
    container.textContent = text || '(no extractable text)';
  }
  const kids = [container];
  if (data.truncated) {
    const note = document.createElement('div');
    note.className = 'pv-empty';
    note.textContent = `Showing first ${data.blocks.length} blocks (truncated).`;
    kids.push(note);
  }
  _pv.body.replaceChildren(...kids);
}

async function _pvRenderUnsupported(info) {
  // Try the text pane first (docx/pptx have extracted blocks); else offer download.
  try {
    const r = await fetch(info.previewTextUrl, { headers: _pvAuthHeaders() });
    if (r.ok) {
      const data = await r.json();
      if ((data.blocks || []).length) { await _pvRenderText(info); return; }
    }
  } catch (_e) { /* fall through */ }
  _pvShowMessage('No in-browser preview for this file type. Use Download to open it.');
}

async function _pvRenderPdf(info) {
  const pdfjs = window.pdfjsLib;
  if (!pdfjs) { await _pvRenderUnsupported(info); return; }
  _pvShowMessage('Loading PDF…');
  const r = await fetch(info.previewUrl, { headers: _pvAuthHeaders() });
  if (!r.ok) { _pvShowMessage('Could not load PDF.'); return; }
  const buf = await r.arrayBuffer();
  const doc = await pdfjs.getDocument({ data: buf }).promise;
  _pv.pdf = doc;
  _pv.pageCount = doc.numPages;
  _pv.pageNum = 1;
  _pv.pager.classList.toggle('hidden', doc.numPages <= 1);
  await _pvRenderPdfPage(1);
}

async function _pvRenderPdfPage(n) {
  if (!_pv.pdf || _pv.rendering) return;
  _pv.rendering = true;
  try {
    const page = await _pv.pdf.getPage(n);
    const scale = Math.min(2, (_pv.body.clientWidth - 32) / page.getViewport({ scale: 1 }).width);
    const viewport = page.getViewport({ scale: Math.max(0.5, scale) });
    const canvas = document.createElement('canvas');
    canvas.width = viewport.width;
    canvas.height = viewport.height;
    const ctx = canvas.getContext('2d');
    _pv.body.replaceChildren(canvas);
    await page.render({ canvasContext: ctx, viewport }).promise;
    _pv.pageNum = n;
    _pv.pageInfo.textContent = `Page ${n} / ${_pv.pageCount}`;
    _pv.prevBtn.disabled = (n <= 1);
    _pv.nextBtn.disabled = (n >= _pv.pageCount);
  } finally {
    _pv.rendering = false;
  }
}

function _pvGoto(n) {
  if (!_pv.pdf) return;
  if (n < 1 || n > _pv.pageCount) return;
  _pvRenderPdfPage(n);
}

// Minimal, XSS-safe markdown: escape first, then apply a few inline/block rules.
function _pvRenderMarkdown(src) {
  const esc = escHtml(src);
  return esc
    .replace(/^### (.*)$/gm, '<h3>$1</h3>')
    .replace(/^## (.*)$/gm, '<h2>$1</h2>')
    .replace(/^# (.*)$/gm, '<h1>$1</h1>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
    .replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>')
    .replace(/\n{2,}/g, '<br><br>')
    .replace(/\n/g, '<br>');
}
