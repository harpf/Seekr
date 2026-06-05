# Frontend Modularisation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the single 1,959-line `document_search/web/static/app.js` into a tree of native ES modules under `document_search/web/static/js/` — one entrypoint `main.js` (loaded via `<script type="module">`) that imports feature modules (`api.js`, `auth.js`, `search.js`, `ingest.js`, `config.js`, `jobs.js`, `ui/toast.js`, `ui/chip-input.js`). No framework, no bundler, no build step. The app must keep working after every single commit.

**Architecture:** Browsers natively support ES modules (`import`/`export`) over HTTP when the entry `<script>` carries `type="module"`. Seekr already serves `document_search/web/static/` at `/static` via `StaticFiles` (`document_search/app.py:262`), so `/static/js/main.js` and its relative imports (`./api.js`, `./ui/toast.js`) resolve with zero server changes. The current `app.js` is one flat scope: a top-level `token` variable, three module-level chip handles (`chipFiletype`, `chipTagFilter`, `chipUploadTags`), a `_resultTagChips` map, several `_last*`/`_reorganize*`/`_ha*` state vars, a global `api()` fetch helper, and ~80 functions. Many of those functions are referenced from inline `onclick="..."` attributes in the Jinja templates (e.g. `onclick="runSearch()"`, `onclick="filterByTag('x')"`, `onclick="deleteUser(3)"`). **ES module scope is NOT global** — functions defined in a module are not visible to inline `onclick`. Therefore the migration's load-bearing constraint is: every function reachable from an inline handler must be re-exposed on `window` by the entrypoint. We do this centrally in `main.js` so templates need no churn.

The shared mutable state (`token`) is the only true cross-module coupling. We isolate it in `api.js` behind `getToken()` / `setToken()` / `clearToken()` so no module reaches across to a sibling's variable.

**Tech Stack:** Vanilla JS (ES2020 modules), no build tooling. Served as-is by FastAPI `StaticFiles`. Python 3.11 for the structural template-invariant checks. No new third-party dependencies. **No JS test runner exists in this repo** — every verification below is manual (load the page in a browser, exercise the moved feature) plus a Python structural assertion on the templates/files.

**Bundler decision (LOCKED): bundler-free.** Seekr is a small, locally-run, single-user-ish app served over LAN/localhost. Native ES modules add at most a handful of extra HTTP round-trips on first load (all from the same origin, all cacheable). That cost is negligible here and it removes an entire toolchain (node_modules, build step, source maps, CI wiring) that the project does not otherwise need. We therefore ship native modules. An optional `esbuild` bundling step is described in the final task as a **deferred, opt-in follow-up** only — it is NOT part of the Definition of Done and must not be implemented unless a concrete need (measured slow first paint, or a target browser without module support) materialises.

**Scope boundaries:**

In scope:
- Create `document_search/web/static/js/` with `main.js` + 8 feature/ui modules.
- Move every function and module-level state variable out of `app.js` into the correct module, preserving behaviour byte-for-byte where possible.
- `main.js` imports the modules, wires page bootstrap, and re-exposes inline-handler functions on `window`.
- Flip all five templates' `<script src="/static/app.js">` to `<script type="module" src="/static/js/main.js">`.
- Delete `app.js` only in the final cleanup task, after everything is migrated and verified.

Out of scope (explicitly NOT done here):
- Removing inline `onclick` handlers from templates / switching to `addEventListener` delegation. (That is a larger, riskier change; tracked as a future plan. We keep `onclick` working by exposing functions on `window`.)
- Any change to API endpoints, CSS, or HTML structure beyond the single `<script>` tag per template.
- Adding a bundler / minifier / TypeScript / linter.
- Adding a JS unit-test runner.
- Behavioural changes, refactors of function internals, or "while I'm here" cleanups.

---

## File Structure

**Create (under `document_search/web/static/js/`):**
- `api.js` — `getToken`, `setToken`, `clearToken`, `api(path, method, body)`, `escHtml`, `setText`. The fetch helper + token store + two tiny DOM/string helpers used by every module.
- `ui/toast.js` — `showToast(msg, type, duration)`.
- `ui/chip-input.js` — `ChipInput` class (default export or named export).
- `auth.js` — `login`, `signOut`, `showAuthedPanels`, `showAdminUI`, `loadStatus`, `formatBytes`.
- `search.js` — search + results + tag cloud + recent searches + per-result tag/mark/reindex actions.
- `ingest.js` — upload, drop zone, post-upload AI suggestion, AI reorganizer, index start, index cleanup, structure suggestion, reindex-by-id, ingest option loading.
- `config.js` — config load/save, tabs, source paths, path test, mount, users, DB test, deps, AI status/config tab, SSL, Home Assistant keys/YAML.
- `jobs.js` — the shared job-polling helper(s) extracted from the index/AI/pull/update pollers (one `pollJob(jobId, { interval, onTick, onDone })` used by callers). Also `checkForUpdates` / `runUpdate` update-poll logic.
- `main.js` — the `type="module"` entrypoint: imports the above, runs `bootstrap()`, calls `initNav` / `initSearchPage`, and re-exposes inline-handler functions on `window`.

**Modify:**
- `document_search/web/templates/index.html:190`
- `document_search/web/templates/search.html:194`
- `document_search/web/templates/ingest.html:358`
- `document_search/web/templates/config.html:772`
- `document_search/web/templates/wiki.html:331`
  (each: swap the one `<script src="/static/app.js"></script>` line)

**Delete (final task only):**
- `document_search/web/static/app.js`

**Untouched:**
- `document_search/app.py` — `StaticFiles` mount already serves the new paths; no change.
- All CSS, all HTML body content, all `data-page` attributes, all inline `onclick` handlers.

---

## Key design decisions (locked)

- **One module per commit.** Each task extracts exactly one feature group into its module, leaves a thin re-export/`window` shim so the still-loaded `app.js` (or already-migrated modules) keep working, and is independently verifiable in the browser. The conventional-commit type is `refactor(ui):`.
- **Two-phase migration to never break the page.** We do NOT flip the template `<script>` to `type="module"` first and then scramble to move everything (that would break the app for many commits). Instead:
  - **Phase A (Task 1):** create `js/main.js` that, for now, simply re-implements bootstrap by importing the *new* modules as they appear — but we start by switching templates to load `main.js`, and `main.js` initially imports nothing except a verbatim copy is avoided. To keep the page working from commit 1, **Task 1 makes `main.js` import the brand-new `api.js`, `ui/toast.js`, and `ui/chip-input.js`, and `<script type="module">`-loads it, while the templates ALSO keep loading the legacy `app.js` as a classic script for the not-yet-migrated functions.** Loading both is safe because module scope and classic-script global scope are separate, and the legacy `app.js` calls `bootstrap()` itself. See Task 1 for the exact ordering that avoids double-bootstrap.
  - **Phase B (Tasks 2–8):** move one group at a time from `app.js` into its module; after each move, delete that group from `app.js`, import it into `main.js`, and expose it on `window`. The legacy `app.js` shrinks every commit.
  - **Phase C (Task 9):** once `app.js` is empty of logic, delete it and remove the legacy `<script>` tag; `main.js` is the sole entrypoint and owns `bootstrap()`.
- **`token` lives in `api.js` only.** Replace every bare `token` read/write across modules with `getToken()` / `setToken()` / `clearToken()`. The raw FormData uploads (`/api/upload`, `/api/ssl/upload`) that build their own headers use `getToken()`.
- **`window` re-exposure is centralised in `main.js`.** No module pollutes `window` itself. `main.js` has one `Object.assign(window, {...})` block listing exactly the functions referenced by inline `onclick`/`onchange` in templates. This is the single place to audit the module/template contract.
- **No double `bootstrap()`.** Exactly one module owns the bootstrap call. During Phase B the legacy `app.js` keeps owning it; `main.js` must NOT call `bootstrap()` until Task 9. (Task 1's `main.js` only initialises the leaf utilities, which have no side effects.)
- **Helpers `escHtml` / `setText` go in `api.js`** because nearly every module needs them and they have zero dependencies. `showToast` goes in `ui/toast.js` (it touches the DOM `#toastWrap`). `api.js` imports `showToast` from `ui/toast.js` for its 401 path — this is the only api→ui edge, and it's acyclic (`toast.js` imports nothing).

---

## Inline-handler contract (the `window` re-export list)

These identifiers are called from inline `onclick=`/`onchange=` in the templates and from string-built HTML inside `app.js` (`renderResults`, `renderUserTable`, `renderPathList`, `renderHaKeysTable`, `loadTagCloud`, `renderModelLibrary`). Every one must be on `window` by the time its template/markup is live. `main.js` owns this list:

```
runSearch, clearSearch, toggleFilters, filterByTag,
saveTags, toggleMark, reindexDocumentFromSearch,
login, signOut,
uploadDocument, applyAiSuggestion, dismissAiSuggestion,
startAiReorganize, toggleSelectAll, applySelectedMoves,
startIndex, runIndexCleanup, reindexDocument, startStructureSuggestion,
saveConfig, loadConfig, switchTab,
addSourcePath, removeSourcePath, testPathQuick, savePathsConfig, runPathTest,
onMountTypeChange, mountShare, unmountShare,
createUser, updateUserRole, deleteUser, openChangePassword, cancelChangePassword, submitChangePassword,
runDbTest,
onAiModelSelectChange, selectAiModel, deleteAiModel, saveAiConfig, testAiConnection, pullModelFromAiTab, pullModel,
generateCert, uploadCert,
testHaConnection, createHaKey, deleteHaKey, copyHaKey, prefillHaYamlById, renderHaYaml, copyHaYaml,
checkForUpdates, runUpdate
```

> **Audit step (do this once, in Task 1):** grep the templates for the authoritative list so nothing is missed:
> ```powershell
> Select-String -Path document_search\web\templates\*.html -Pattern 'on(click|change)="([a-zA-Z0-9_]+)' -AllMatches |
>   ForEach-Object { $_.Matches } | ForEach-Object { $_.Groups[2].Value } |
>   Sort-Object -Unique
> ```
> Also grep `app.js` for `onclick=` inside template-literal strings (HTML built in JS) — those call the same global functions and are covered by the same `window` list.

---

## Task 1: Scaffold `js/`, extract leaf utilities, load `main.js` alongside legacy `app.js`

**Files:**
- Create: `document_search/web/static/js/api.js`
- Create: `document_search/web/static/js/ui/toast.js`
- Create: `document_search/web/static/js/ui/chip-input.js`
- Create: `document_search/web/static/js/main.js`
- Modify: all five templates (add the module `<script>`, keep the legacy one for now)

- [ ] **Step 1: Create `ui/chip-input.js`** — move the `ChipInput` class verbatim (lines 1–66 of `app.js`) and export it.

```javascript
// document_search/web/static/js/ui/chip-input.js
export class ChipInput {
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
```

- [ ] **Step 2: Create `ui/toast.js`** — move `showToast` and the `escHtml` it needs. To avoid a circular import (`api.js` imports `escHtml`, `toast.js` imports `escHtml`), keep `escHtml` in `api.js` and import it here.

```javascript
// document_search/web/static/js/ui/toast.js
import { escHtml } from '../api.js';

export function showToast(msg, type = 'info', duration = 3500) {
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
```

- [ ] **Step 3: Create `api.js`** — the token store + fetch helper + `escHtml`/`setText`. The token, formerly a module-level `let`, becomes private to this module behind accessors. The 401 path imports `showToast` from `ui/toast.js`.

```javascript
// document_search/web/static/js/api.js
import { showToast } from './ui/toast.js';

let _token = localStorage.getItem('documentSearchToken');

export function getToken() { return _token; }

export function setToken(t) {
  _token = t;
  localStorage.setItem('documentSearchToken', t);
}

export function clearToken() {
  _token = null;
  localStorage.removeItem('documentSearchToken');
  localStorage.removeItem('documentSearchRole');
}

export function escHtml(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

export function setText(id, message, type = '') {
  const node = document.getElementById(id);
  if (!node) return;
  node.textContent = message;
  node.className = node.className.replace(/\b(ok|err|info)\b/g, '').trim();
  if (type) node.classList.add(type);
}

export async function api(path, method = 'GET', body = null) {
  const headers = { 'X-Auth-Token': getToken() ?? '' };
  if (body !== null) headers['Content-Type'] = 'application/json';
  const res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : null });
  if (res.status === 401 && path !== '/api/login') {
    showToast('Session expired — please sign in again', 'err');
    clearToken();
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
```

> **Note on the import cycle:** `api.js` imports `showToast` from `toast.js`, and `toast.js` imports `escHtml` from `api.js`. ES modules permit cyclic imports as long as no imported binding is *used* during the module's top-level evaluation. Here both bindings are only referenced inside function bodies (called later), so the cycle resolves cleanly. Do not move `escHtml` into `toast.js` (that would create a cycle whose binding *is* used at evaluation time if anything changes).

- [ ] **Step 4: Create the initial `main.js`.** In this first commit it must NOT call `bootstrap()` (the legacy `app.js` still owns bootstrap). It only proves the module graph loads and exposes the three leaf utilities for any future inline use. Keep it minimal:

```javascript
// document_search/web/static/js/main.js
// Native ES-module entrypoint. During migration this loads ALONGSIDE the
// legacy /static/app.js (a classic script that still owns bootstrap()).
// Modules are extracted one-per-commit; each newly-moved group is imported
// here and re-exposed on window for inline onclick handlers in the templates.
import { ChipInput } from './ui/chip-input.js';
import { showToast } from './ui/toast.js';
import { api, getToken, setToken, clearToken, escHtml, setText } from './api.js';

// Re-expose leaf utilities so legacy app.js code that still references the
// classic globals keeps working during the transition. (app.js currently
// defines its OWN copies; until a group is migrated, app.js's copies win in
// the classic global scope. These module-scoped names do not collide.)
Object.assign(window, { ChipInput, showToast, api, escHtml, setText });
```

- [ ] **Step 5: Update each template** to load `main.js` as a module, BEFORE the legacy classic script. Module scripts are deferred (execute after HTML parse, in order relative to other modules), while the classic `app.js` runs at its position; loading `main.js` first keeps the `window` utilities available. For each of the five templates, replace the single line:

  `  <script src="/static/app.js"></script>`

  with:

  ```html
  <script type="module" src="/static/js/main.js"></script>
  <script src="/static/app.js"></script>
  ```

  Files & lines: `index.html:190`, `search.html:194`, `ingest.html:358`, `config.html:772`, `wiki.html:331`.

- [ ] **Step 6: Manual verification (browser).** Start the app, hard-reload (Ctrl+F5) each page, and confirm in DevTools:
  - **Network tab:** `js/main.js`, `js/api.js`, `js/ui/toast.js`, `js/ui/chip-input.js` all load `200` (not `404`), served from `/static/js/...`.
  - **Console:** no `Failed to load module script` / MIME-type / 404 errors. (FastAPI `StaticFiles` serves `.js` as `text/javascript`, which is a valid module MIME — confirm no MIME error.)
  - **Behaviour unchanged:** log in, run a search, switch a config tab, upload a file. Everything still works because legacy `app.js` is still in charge. This commit changes nothing user-visible; it only proves the module graph resolves.

- [ ] **Step 7: Structural invariant check (Python).** Confirm every template references the module entrypoint with `type="module"`:

```powershell
python -c "import pathlib,sys; tpl=pathlib.Path('document_search/web/templates'); bad=[p.name for p in tpl.glob('*.html') if '<script type=\"module\" src=\"/static/js/main.js\">' not in p.read_text(encoding='utf-8')]; print('MISSING module tag in:', bad) or sys.exit(1 if bad else 0)"
```

Expected: prints `MISSING module tag in: []` and exits 0.

- [ ] **Step 8: Commit.**

```powershell
git checkout -b refactor/frontend-modules
git add document_search/web/static/js document_search/web/templates
git commit -m @'
refactor(ui): scaffold ES modules, extract api/toast/chip-input leaves

Add native ES-module entrypoint static/js/main.js loaded via
<script type="module"> alongside the legacy classic app.js. Extract the
three dependency-free leaves: ChipInput (ui/chip-input.js), showToast
(ui/toast.js), and the api()/token/escHtml/setText helpers (api.js, token
now private behind get/set/clearToken). No behaviour change: legacy app.js
still owns bootstrap and all feature logic; modules only re-expose leaf
utilities on window for the transition.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 2: Extract `auth.js`

**Files:**
- Create: `document_search/web/static/js/auth.js`
- Modify: `document_search/web/static/js/main.js`, `document_search/web/static/app.js`

Moves from `app.js`: `showAuthedPanels` (132–139), `signOut` (141–146), `formatBytes` (148–152), `loadStatus` (154–166), `login` (168–206), `showAdminUI` (860–862).

- [ ] **Step 1: Create `auth.js`** with the full moved bodies. Replace bare `token` writes in `login`/`signOut` with `setToken`/`clearToken`; import `api`, `setText`, `getToken` from `api.js` and `showToast` from `ui/toast.js`, `ChipInput` from `ui/chip-input.js`. Note `login`'s tail constructs page-specific ChipInputs and calls search/ingest loaders — to avoid a premature cross-module dependency, **leave the page-specific re-init in `login` calling functions that `main.js` will provide via a small injected hook** (simplest: have `login` dispatch a `CustomEvent('seekr:authed', { detail: { page } })` that `main.js` listens for and runs the same page-init it runs in `bootstrap`). This removes auth→search/ingest coupling. Full signature of the moved `login`:

```javascript
// document_search/web/static/js/auth.js
import { api, setText, setToken, clearToken } from './api.js';
import { showToast } from './ui/toast.js';

export function showAuthedPanels() { /* moved verbatim */ }
export function signOut() { clearToken(); location.reload(); }
export function formatBytes(bytes) { /* moved verbatim */ }
export async function loadStatus() { /* moved verbatim, uses api() */ }
export function showAdminUI() { /* moved verbatim */ }

export async function login() {
  try {
    const data = await api('/api/login', 'POST', { username: username.value, password: password.value });
    setToken(data.token);
    localStorage.setItem('documentSearchRole', data.role || 'user');
    setText('loginResult', '', '');
    showToast(`Signed in as ${data.username}`, 'ok');
    showAuthedPanels();
    if (data.role === 'admin') showAdminUI();
    await loadStatus();
    // page-specific init (config/search/ingest) is handled by main.js:
    document.dispatchEvent(new CustomEvent('seekr:authed', { detail: { role: data.role } }));
  } catch (error) {
    setText('loginResult', `Login failed: ${error.message}`, 'err');
  }
}
```

- [ ] **Step 2:** Delete those six functions from `app.js`. In `main.js`, `import { login, signOut, showAuthedPanels, showAdminUI, loadStatus } from './auth.js';`, add `login, signOut` to the `window` block, and add the `seekr:authed` listener that runs the same per-page init currently in `app.js`'s `bootstrap` (config load + search/ingest chip setup + option loaders). The legacy `app.js` `bootstrap` still runs on initial page load for the token-present path; the event path covers fresh logins.
- [ ] **Step 3: Verify (browser).** Sign out, reload, sign back in. Confirm: toast "Signed in as …", authed panels appear, status counters populate, and on the search page the filter chips + tag cloud initialise after login. Sign out reloads to the gate.
- [ ] **Step 4: Commit** `refactor(ui): extract auth (login/signout/status) into auth.js`.

---

## Task 3: Extract `search.js`

**Files:** Create `js/search.js`; modify `main.js`, `app.js`.

Moves: `saveRecentSearch`/`renderRecentSearches` (209–230), `toggleFilters` (233–241), `clearSearch` (243–252), `runSearch` (254–291), `saveTags`/`toggleMark` (293–304), `buildHitEl` (306–324), `HITS_SHOW_MAX` + `renderResults` (326–470), `loadTagCloud` (1761–1775), `loadFilterOptions` (1777–1786), `filterByTag` (1838–1843), `reindexDocumentFromSearch` (1846–1853), `initSearchPage` (1696–1714). Owns module state `_resultTagChips` and the page chip handles `chipFiletype`/`chipTagFilter`.

- [ ] **Step 1: Create `search.js` skeleton + moved bodies.** Imports: `{ api, escHtml, setText } from './api.js'`, `{ showToast } from './ui/toast.js'`, `{ ChipInput } from './ui/chip-input.js'`. The three module-level globals it owns:

```javascript
// document_search/web/static/js/search.js
import { api, escHtml } from './api.js';
import { showToast } from './ui/toast.js';
import { ChipInput } from './ui/chip-input.js';

let chipFiletype, chipTagFilter;
const _resultTagChips = {};
const HITS_SHOW_MAX = 5;

export function initSearchChips() {
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
}

export async function runSearch() { /* moved verbatim; uses chipFiletype/chipTagFilter, api, renderResults, saveRecentSearch */ }
export function renderResults(docs) { /* moved verbatim; populates _resultTagChips, references saveTags/toggleMark/reindexDocumentFromSearch/filterByTag (all in this module) */ }
export async function saveTags(documentId) { /* … */ }
export async function toggleMark(documentId, current) { /* … */ }
export function filterByTag(name) { /* … */ }
export async function reindexDocumentFromSearch(documentId) { /* … */ }
export function toggleFilters() { /* … */ }
export function clearSearch() { /* … */ }
export function saveRecentSearch(q) { /* … */ }
export function renderRecentSearches() { /* … */ }
export async function loadTagCloud() { /* … */ }
export async function loadFilterOptions() { /* … */ }
export function initSearchPage() { /* … restores ?q=, "/" focus shortcut, Enter→runSearch */ }
```

> `renderResults` builds HTML strings and per-card `addEventListener` callbacks that reference `saveTags`, `toggleMark`, `reindexDocumentFromSearch`, `filterByTag` — all now same-module, so no `window` needed for those. BUT `loadTagCloud` builds `onclick="filterByTag('…')"` as an inline-string handler, and templates use `onclick="runSearch()"`, `onclick="clearSearch()"`, `onclick="toggleFilters()"` — so `runSearch`, `clearSearch`, `toggleFilters`, `filterByTag` MUST be on `window` (added in main.js).

- [ ] **Step 2:** Delete the moved functions from `app.js`. Remove `app.js`'s own `chipFiletype`/`chipTagFilter`/`_resultTagChips` declarations and `loadFilterOptions`/`loadTagCloud`/`filterByTag`/`initSearchPage` (now owned by `search.js`). Update `app.js`'s remaining `bootstrap` + `seekr:authed` handler in `main.js` to call `initSearchChips()` / `loadFilterOptions()` / `loadTagCloud()` / `initSearchPage()` from the module instead.
- [ ] **Step 3:** In `main.js`: import the search module; add `runSearch, clearSearch, toggleFilters, filterByTag, saveTags, toggleMark, reindexDocumentFromSearch` to the `window` block; wire the search-page branch of bootstrap/`seekr:authed` to the module functions.
- [ ] **Step 4: Verify (browser, search page).** Run a query → results render; star/unstar a doc; edit + save tags on a card; click a result tag chip → it filters; click a tag-cloud chip → filters + runs; "/" focuses the box; Enter runs; `?q=foo` in URL pre-fills and auto-runs; "Show N more hits" expands; Reindex shows its toast.
- [ ] **Step 5: Commit** `refactor(ui): extract search/results/tag-cloud into search.js`.

---

## Task 4: Extract `jobs.js` (shared poller) + update flow

**Files:** Create `js/jobs.js`; modify `main.js`, `app.js`.

Moves: `checkForUpdates` (739–767), `_updatePollInterval`+`runUpdate` (769–807). Also create the reusable `pollJob` helper that Tasks 5 will consume (index/AI pollers). Keep `pollJob` generic:

```javascript
// document_search/web/static/js/jobs.js
import { api, escHtml } from './api.js';
import { showToast } from './ui/toast.js';

// Generic poller: calls api(url) every `interval` ms; onTick(job) each poll;
// when isDone(job) returns true, clears the timer and calls onDone(job).
export function pollJob(url, { interval = 1500, onTick, onDone, isDone }) {
  const handle = setInterval(async () => {
    try {
      const job = await api(url);
      onTick?.(job);
      if (isDone(job)) { clearInterval(handle); onDone?.(job); }
    } catch (_) { /* preserve existing swallow-errors-while-polling behaviour */ }
  }, interval);
  return handle;
}

let _updatePollInterval = null;
export async function checkForUpdates() { /* moved verbatim */ }
export async function runUpdate() { /* moved verbatim, uses _updatePollInterval */ }
```

- [ ] **Step 1:** Create `jobs.js` with `pollJob`, `checkForUpdates`, `runUpdate` (move the two update functions verbatim, keeping their bespoke `setInterval` since their polling shape — `/api/update/status` with restart-tolerance — differs from `pollJob`; do NOT force them through `pollJob`).
- [ ] **Step 2:** Delete those from `app.js`. Import into `main.js`; add `checkForUpdates, runUpdate` to `window`.
- [ ] **Step 3: Verify (browser, config → System tab).** Click "Check for updates" → status line updates (reachable or unreachable both fine). Do NOT click "Run update" on a live box; instead confirm the button is wired (no console error on hover/focus) — or test on a throwaway checkout.
- [ ] **Step 4: Commit** `refactor(ui): extract update flow and shared pollJob into jobs.js`.

---

## Task 5: Extract `ingest.js`

**Files:** Create `js/ingest.js`; modify `main.js`, `app.js`.

Moves: `initDropZone` (473–497), `_lastUploadDocId`/`_lastAiSuggestion`+`uploadDocument` (499–534), `renderAiSuggestion`/`dismissAiSuggestion`/`applyAiSuggestion` (537–592), `_reorganizeResults`+`startAiReorganize`/`renderReorganizeTable`/`toggleSelectAll`/`applySelectedMoves` (595–688), `startIndex` (690–737), `runIndexCleanup` (1874–1887), `reindexDocument` (1856–1871), `startStructureSuggestion`/`renderStructureResult` (1890–1958), `loadIngestOptions` (1788–1836). Owns `chipUploadTags`.

- [ ] **Step 1: Create `ingest.js`** skeleton. Imports: `{ api, escHtml, setText, getToken } from './api.js'`, `{ showToast } from './ui/toast.js'`, `{ ChipInput } from './ui/chip-input.js'`, `{ pollJob } from './jobs.js'`. `uploadDocument` and any FormData upload use `getToken()` for the `X-Auth-Token` header instead of the old bare `token`:

```javascript
const res = await fetch('/api/upload', {
  method: 'POST',
  headers: { 'X-Auth-Token': getToken() ?? '' },
  body: fd,
});
```

Refactor the three job-polling functions (`startIndex`, `startAiReorganize`, `startStructureSuggestion`) to use `pollJob(url, { interval, onTick, onDone, isDone })` — `isDone` maps to each one's terminal check (`status === 'finished'` / `'error'`). Keep the exact UI side-effects (progress fill %, status text, toasts) inside `onTick`/`onDone`. Export `initIngestChips()` (constructs `chipUploadTags`), `initDropZone`, `loadIngestOptions`, and all inline-handler functions.

- [ ] **Step 2:** Delete moved code from `app.js`; remove its `chipUploadTags` decl. Update `main.js` bootstrap/`seekr:authed` ingest branch to call `initIngestChips()` + `initDropZone()` + `loadIngestOptions()` from the module.
- [ ] **Step 3:** In `main.js` add to `window`: `uploadDocument, applyAiSuggestion, dismissAiSuggestion, startAiReorganize, toggleSelectAll, applySelectedMoves, startIndex, runIndexCleanup, reindexDocument, startStructureSuggestion`.
- [ ] **Step 4: Verify (browser, ingest page).** Drag/drop + pick a file (name shows); upload → toast + JSON result; if AI suggestion returns, the card shows and Apply moves the file; Start indexing on a selected folder → progress bar advances to 100% and finishes; "Reindex by ID"; "Index cleanup"; AI reorganizer Start → table fills, select-all + apply selected; structure suggestion Start → folder list renders.
- [ ] **Step 5: Commit** `refactor(ui): extract upload/index/AI ingest into ingest.js`.

---

## Task 6: Extract `config.js`

**Files:** Create `js/config.js`; modify `main.js`, `app.js`.

This is the largest module. Moves: `loadConfig`/`saveConfig` (810–842), `switchTab` (845–858), `_sourcePaths` + path functions `renderPathList`/`addSourcePath`/`removeSourcePath`/`testPathQuick`/`savePathsConfig`/`runPathTest` (865–951), `onMountTypeChange`/`mountShare`/`unmountShare` (953–1001), user mgmt `loadUsers`/`renderUserTable`/`createUser`/`updateUserRole`/`deleteUser`/`openChangePassword`/`cancelChangePassword`/`submitChangePassword` (1004–1109), `runDbTest`/`loadDeps`/`loadAiStatus` (1112–1179), AI tab `loadAiTabData`/`loadAiSystemInfo`/`renderSystemResources`/`populateModelDropdown`/`onAiModelSelectChange`/`renderModelLibrary`/`selectAiModel`/`deleteAiModel`/`saveAiConfig`/`testAiConnection`/`pullModelFromAiTab`/`pullModel` (1183–1432), SSL `loadSslStatus`/`generateCert`/`uploadCert` (1435–1511), Home Assistant `testHaConnection`/`_haNewKey`/`_haKeyStore`/`loadHaKeys`/`renderHaKeysTable`/`prefillHaYamlById`/`createHaKey`/`deleteHaKey`/`copyHaKey`/`prefillHaYaml`/`renderHaYaml`/`copyHaYaml` (1514–1685).

- [ ] **Step 1: Create `config.js`** skeleton. Imports: `{ api, escHtml, setText, getToken } from './api.js'`, `{ showToast } from './ui/toast.js'`, `{ pollJob } from './jobs.js'` (for `pullModelFromAiTab`/`pullModel`). `uploadCert`'s FormData upload uses `getToken()`. `switchTab` keeps its `if (name === 'users') loadUsers()` dispatch — all those loaders are same-module now. Module-owned state: `_sourcePaths`, `_haNewKey`, `_haKeyStore`. Export `loadConfig` (called from bootstrap when `#configPanel` exists) plus every inline-handler function.

```javascript
// document_search/web/static/js/config.js  (skeleton)
import { api, escHtml, setText, getToken } from './api.js';
import { showToast } from './ui/toast.js';
import { pollJob } from './jobs.js';

let _sourcePaths = [];
let _haNewKey = null;
const _haKeyStore = {};

export async function loadConfig() { /* … */ }
export async function saveConfig() { /* … */ }
export function switchTab(name) { /* … same per-tab loader dispatch */ }
// paths, mount, users, db-test, deps, ai-status, ai-tab, ssl, ha … all moved verbatim
```

- [ ] **Step 2:** Delete all moved code from `app.js`. At this point `app.js` should retain only `initNav` + `bootstrap` (Task 7 removes those).
- [ ] **Step 3:** In `main.js` add the full config `window` set: `saveConfig, loadConfig, switchTab, addSourcePath, removeSourcePath, testPathQuick, savePathsConfig, runPathTest, onMountTypeChange, mountShare, unmountShare, createUser, updateUserRole, deleteUser, openChangePassword, cancelChangePassword, submitChangePassword, runDbTest, onAiModelSelectChange, selectAiModel, deleteAiModel, saveAiConfig, testAiConnection, pullModelFromAiTab, pullModel, generateCert, uploadCert, testHaConnection, createHaKey, deleteHaKey, copyHaKey, prefillHaYamlById, renderHaYaml, copyHaYaml`. Have bootstrap/`seekr:authed` call `loadConfig()` when `#configPanel` exists.
- [ ] **Step 4: Verify (browser, config page — admin login).** Each tab opens: General loads + saves config; Paths add/remove/test/save; Mount form toggles SMB creds; Users table loads, create/role-change/delete/change-password; System DB test + deps + AI status; AI tab system info + model library Use/Delete + Save & Apply + Test connection + pull model; SSL status/generate/upload; Home Assistant create key (YAML prefills), Use, delete, copy YAML.
- [ ] **Step 5: Commit** `refactor(ui): extract config/users/paths/ai/ssl/ha into config.js`.

---

## Task 7: Move `initNav` + `bootstrap` into `main.js`; empty `app.js`

**Files:** Modify `js/main.js`, `document_search/web/static/app.js`.

At this point `app.js` contains only `initNav` (1688–1694), `initSearchPage` (already moved to search.js in Task 3 — verify it's gone from app.js), `bootstrap` (1716–1756), and the bare `bootstrap();` call (1758).

- [ ] **Step 1:** Move `initNav` into `main.js` (it only adds an `active` class to nav links — no deps). Reconstruct `bootstrap` in `main.js` using the imported module functions:

```javascript
// in main.js, after imports + window assignment:
function initNav() {
  const map = { home: '/', search: '/search', ingest: '/ingest', config: '/config', wiki: '/wiki' };
  const activeHref = map[document.body?.dataset?.page || ''];
  document.querySelectorAll('.nav-links a').forEach(link => {
    if (link.getAttribute('href') === activeHref) link.classList.add('active');
  });
}

async function pageInit(role) {
  if (document.getElementById('configPanel')) await loadConfig();
  if (document.body?.dataset?.page === 'search') {
    initSearchChips();
    await loadFilterOptions();
    await loadTagCloud();
    const q = new URLSearchParams(location.search).get('q');
    if (q) await runSearch();
  }
  if (document.body?.dataset?.page === 'ingest') {
    initIngestChips();
    initDropZone();
    await loadIngestOptions();
  }
}

document.addEventListener('seekr:authed', e => pageInit(e.detail.role));

async function bootstrap() {
  initNav();
  renderRecentSearches();
  if (document.body?.dataset?.page === 'search') initSearchPage();
  if (getToken()) {
    showAuthedPanels();
    const role = localStorage.getItem('documentSearchRole') || 'user';
    if (role === 'admin') showAdminUI();
    await loadStatus();
    await pageInit(role);
  }
}
bootstrap();
```

- [ ] **Step 2:** Reduce `app.js` to a single deprecation comment (do NOT delete the file yet — its `<script>` tag is still in the templates; an empty-but-present file avoids a 404). Example contents:

```javascript
/* Migrated to ES modules under /static/js/. This file is intentionally empty
   and will be removed once all templates load only /static/js/main.js. */
```

- [ ] **Step 3: Verify (browser, ALL pages).** Hard-reload home, search, ingest, config, wiki. Each: nav active link correct; recent searches render on home; token-present auto-login restores panels + status; search auto-run from `?q=`; ingest chips init; config loads. Confirm in console there is exactly ONE bootstrap (no duplicate status fetch in Network — `app.js` no longer calls bootstrap).
- [ ] **Step 4: Commit** `refactor(ui): move bootstrap/nav into main.js, empty legacy app.js`.

---

## Task 8: Drop the legacy `<script>` tag and delete `app.js`

**Files:** Modify all five templates; delete `document_search/web/static/app.js`.

- [ ] **Step 1:** In each template remove the now-empty legacy line, leaving only the module entrypoint:

  Remove `  <script src="/static/app.js"></script>` from `index.html`, `search.html`, `ingest.html`, `config.html`, `wiki.html`. Keep `<script type="module" src="/static/js/main.js"></script>`.

- [ ] **Step 2:** Delete the file:

```powershell
git rm document_search/web/static/app.js
```

- [ ] **Step 3: Structural invariant check (Python).** No template references the old file; all reference the module:

```powershell
python -c "import pathlib,sys; tpl=pathlib.Path('document_search/web/templates'); files=list(tpl.glob('*.html')); old=[p.name for p in files if '/static/app.js' in p.read_text(encoding='utf-8')]; mod=[p.name for p in files if '<script type=\"module\" src=\"/static/js/main.js\">' in p.read_text(encoding='utf-8')]; print('still ref app.js:', old, '| have module tag:', mod); sys.exit(1 if old or len(mod)!=5 else 0)"
```

Expected: `still ref app.js: [] | have module tag: ['config.html', 'index.html', 'ingest.html', 'search.html', 'wiki.html']`, exit 0.

- [ ] **Step 4: Verify (browser, ALL pages, hard-reload).** Full regression: Network shows no request for `/static/app.js` (no 404), `js/main.js` + all imported modules load 200, console clean. Re-exercise one feature per module: login, search+star+tag, upload, config tab switch + save, update check.
- [ ] **Step 5:** Run the Python suite to confirm nothing server-side broke (templates still render):

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: same pass count as before this plan (no JS is exercised by pytest; this only proves the templates/app import cleanly and routes still serve).

- [ ] **Step 6: Commit** `refactor(ui): remove legacy app.js, modules are sole entrypoint`.

---

## Task 9 (OPTIONAL, DEFERRED — do NOT implement as part of this plan): esbuild bundling follow-up

State for the record only. If, and only if, a concrete problem appears (measured slow first paint on the target hardware, or a deployment target whose browser lacks ES-module support), a follow-up plan may add a single dev-dependency bundler:

- `npx esbuild document_search/web/static/js/main.js --bundle --minify --outfile=document_search/web/static/js/bundle.js`
- Templates would load `/static/js/bundle.js` (classic script) in production and `main.js` (module) in dev, switched by a Jinja flag.
- This introduces a build step + a checked-in or build-time-generated artifact and is therefore explicitly **out of scope** here. The default and shipped state is bundler-free native modules.

Do not create `package.json`, `node_modules`, or any build script under this plan.

---

## Definition of Done

- [ ] `document_search/web/static/js/` contains `main.js`, `api.js`, `auth.js`, `search.js`, `ingest.js`, `config.js`, `jobs.js`, `ui/toast.js`, `ui/chip-input.js` — and nothing else.
- [ ] `document_search/web/static/app.js` is deleted; no template references it.
- [ ] All five templates load exactly `<script type="module" src="/static/js/main.js"></script>` and no other app script (structural check in Task 8 passes, exit 0).
- [ ] `token` is private to `api.js`; no other module reads/writes `localStorage.getItem('documentSearchToken')` directly except via `getToken`/`setToken`/`clearToken`. (Grep: `Select-String -Path document_search\web\static\js\*.js,document_search\web\static\js\ui\*.js -Pattern 'documentSearchToken'` should match only `api.js`.)
- [ ] Every inline `onclick`/`onchange` handler name in the templates is present in `main.js`'s `window` re-export block (compare against the Task-1 audit grep output).
- [ ] Exactly one `bootstrap()` runs per page load (no duplicate `/api/status` request in Network).
- [ ] Manual browser regression passes on all five pages: login/logout, search (run, star, tags, tag-cloud, `?q=` autorun, "/" + Enter), ingest (upload, drop, AI suggestion apply, index start→finish, reorganizer, structure, reindex, cleanup), config (every tab: general save, paths, mount toggle, users CRUD, DB test, deps, AI status/tab/model ops, SSL, HA keys/YAML), update check.
- [ ] `$env:PYTHONPATH = "."; pytest -q` shows the same pass count as before the plan (no server-side regression; templates render).
- [ ] No bundler, no `package.json`, no `node_modules`, no build step introduced.
- [ ] Each module was landed in its own commit with a `refactor(ui): …` message; the app worked (page loaded, moved feature functional) after every commit.

---

## Notes for the executing agent

- **This is a structural/manual refactor — there is NO JS test runner in Seekr.** "Verify" always means: serve the app, hard-reload the page in a real browser, open DevTools, and exercise the moved feature by hand. Do not claim a task done without doing this. The Python `pytest`/structural checks only guard the server side (templates render, no 404 routes) — they cannot catch a broken `onclick`.
- **ES module scope is not global — this is the #1 failure mode.** A function `export`ed from a module is invisible to `onclick="foo()"` in HTML. Anything called from an inline attribute, or from an `onclick="…"` string built inside JS (`renderResults`, `renderUserTable`, `renderPathList`, `renderHaKeysTable`, `renderModelLibrary`, `loadTagCloud`), MUST be assigned to `window` in `main.js`. The Task-1 audit grep is the authoritative source; re-run it after the migration and diff against `main.js`'s `window` block.
- **MIME type:** module scripts require the server to send a JS MIME type (`text/javascript`/`application/javascript`). FastAPI's `StaticFiles` does this correctly for `.js`. If you ever see "Failed to load module script: Expected a JavaScript module script but the server responded with a MIME type of …", the file path is wrong (404 → HTML error page) — check the `/static/js/...` path, not the MIME config.
- **Browser caching during migration.** ES modules are cached aggressively. After every change, hard-reload (Ctrl+F5) or use DevTools "Disable cache" (Network tab) — otherwise you will test stale modules and chase phantom bugs. There is no cache-busting query string here (no build step); rely on hard-reload. If a stale-cache problem persists in deployment, a future plan can append `?v=<gitsha>` in the Jinja `<script>` tag.
- **Module load order & `defer` semantics.** `<script type="module">` is implicitly deferred: it runs after the HTML is parsed, so `document.getElementById(...)` at module top level / in `bootstrap()` is safe. The legacy classic `app.js` (during Phase B) runs at its in-document position — also after the body content above it. Loading the module tag *before* the classic tag (Task 1, Step 5) ensures `window.escHtml` etc. exist if any classic code were to need them; in practice the legacy `app.js` uses its own copies until each group is migrated, so collisions are benign (module names live in module scope; classic names live on the global object).
- **The `token` migration is the trickiest line-level change.** Search the old `app.js` for every bare `token` reference (`headers: { 'X-Auth-Token': token ?? '' }`, `token = data.token`, `if (token)`, `token = null`) and route each through `getToken`/`setToken`/`clearToken`. The two raw `fetch` FormData uploads (`/api/upload` in `uploadDocument`, `/api/ssl/upload` in `uploadCert`) must use `getToken()` — they don't go through `api()`.
- **Don't force the bespoke pollers through `pollJob` if it changes behaviour.** `runUpdate`'s poller tolerates the server restarting (catches the failed request and shows "App restarting, reconnecting…"); keep its dedicated loop. Only the three index/AI start-pollers (`startIndex`, `startAiReorganize`, `startStructureSuggestion`, plus `pullModel`/`pullModelFromAiTab`) share the simple poll-until-terminal shape and may use `pollJob`. Preserve their exact progress-bar percentages and toast text.
- **Preserve incremental progress UX.** As in the job-queue plan's caution: the AI reorganizer and structure suggestion write progress incrementally; copy the `onTick` side-effects faithfully and do not reorder the status/fill updates.
- **One module per commit, app green after each.** If a commit leaves the page broken, you've either forgotten a `window` export or left a dangling reference in the legacy `app.js` to a function you just moved. Grep `app.js` after each extraction for the moved names to confirm they're gone and not still called there.
- **CLAUDE.md quality gate:** error paths stay explicitly handled (keep the `catch (_) {}` swallows where the original had them — they are deliberate "best-effort load" patterns, not bugs to fix here). No behavioural change; this is a pure move.
