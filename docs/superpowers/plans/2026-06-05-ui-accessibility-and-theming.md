# UI Accessibility & Theming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring Seekr's web UI up to WCAG 2.1 AA: semantic landmarks + ARIA, visible focus rings, screen-reader-announced toasts/progress, Escape-to-close panels, keyboard operability. Add a persisted dark mode that respects `prefers-color-scheme`. Move ~60 inline `style=` usages into reusable CSS utility classes. De-duplicate the auth-gate markup (currently copy-pasted in 4 templates) into one parametrised Jinja partial. Add loading skeletons and consistent empty/error states.

**Architecture:** This is a frontend-only change. Touch points:
- `document_search/web/static/styles.css` (~645 lines) — add a `[data-theme="dark"]` token block that overrides the existing `:root` variables, a global `:focus-visible` ring, screen-reader-only helper class, skeleton/utility classes, and a theme-toggle button style.
- `document_search/web/static/app.js` (~1959 lines) — add theme init/toggle, make `showToast` write into an `aria-live` region, set `aria-busy`/`aria-expanded` where panels toggle, bind Escape-to-close, and emit skeletons during async loads.
- `document_search/web/templates/*.html` (5 files) — add landmarks/ARIA, the theme toggle in the topbar, replace inline styles with classes, and `{% include "_partials/auth_gate.html" %}` in the 4 gated pages.
- New `document_search/web/templates/_partials/auth_gate.html` — the shared, parametrised auth gate.

**Tech Stack:** Vanilla JS (no framework, no build step), CSS custom properties, Jinja2 (FastAPI `Jinja2Templates`). New structural tests in pytest only — there is no JS test runner.

**Verification reality check:** There is no JS/DOM test harness in this repo. Full WCAG conformance verification is inherently **manual** (keyboard walkthroughs, a screen reader, and optionally axe-core in the browser). The automated pytest checks added here assert *structural invariants only* — that the rendered HTML contains the required ARIA hooks, that the dark-theme token block exists, that no template still embeds the duplicated literal, and that inline-style counts drop. These are guardrails, **not** a substitute for the manual checklist in the Definition of Done.

**Scope boundaries (out of scope for this plan, picked up by later plans):**
- No Global Jobs dashboard (separate P3 item, pairs with backend `GET /api/jobs`).
- No responsive/mobile-nav overhaul beyond what naturally falls out of the focus/ARIA work — the single 640px breakpoint stays; intermediate breakpoints are a later `[M]` item.
- No `app.js` ES-module split / bundler (separate `[M]` item).
- No backend/API changes. `create_app` and all endpoints are untouched.

---

## File Structure

**Create:**
- `document_search/web/templates/_partials/auth_gate.html` — parametrised shared auth gate.
- `tests/test_ui_templates.py` — structural invariants on rendered templates + CSS.

**Modify:**
- `document_search/web/static/styles.css` — dark theme tokens, focus ring, `.sr-only`, skeletons, utility classes, theme-toggle + reduced-motion.
- `document_search/web/static/app.js` — theme init/toggle, ARIA-aware toasts, `aria-busy`/`aria-expanded`, Escape handler, skeletons.
- `document_search/web/templates/index.html`
- `document_search/web/templates/search.html`
- `document_search/web/templates/ingest.html`
- `document_search/web/templates/config.html`
- `document_search/web/templates/wiki.html` (landmarks + theme toggle only — wiki has **no** auth gate)

**Read-only references:**
- `document_search/app.py:261` — `Jinja2Templates(directory="document_search/web/templates")` (confirms `{% include %}` resolves relative to that dir).
- `document_search/app.py:517-533` — the 5 page routes, each `TemplateResponse(..., {"request": request})`.

---

## Task 1: Dark-mode tokens, focus ring, sr-only, reduced-motion

This is the foundation. No markup yet — pure CSS additions so later tasks can reference the classes.

**Files:**
- Modify: `document_search/web/static/styles.css`
- Test: `tests/test_ui_templates.py` (new)

- [ ] **Step 1: Write the failing CSS-invariant test**

Create `tests/test_ui_templates.py`:

```python
from pathlib import Path

STATIC = Path("document_search/web/static")
TEMPLATES = Path("document_search/web/templates")


def _css() -> str:
    return (STATIC / "styles.css").read_text(encoding="utf-8")


def test_dark_theme_token_block_exists():
    css = _css()
    assert '[data-theme="dark"]' in css, "dark theme token block missing"
    # Dark theme must redefine the core background + text tokens
    block = css.split('[data-theme="dark"]', 1)[1].split("}", 1)[0]
    for token in ("--bg:", "--surface:", "--txt-1:", "--b-lo:"):
        assert token in block, f"{token} not overridden in dark theme"


def test_focus_visible_ring_exists():
    css = _css()
    assert ":focus-visible" in css, "no :focus-visible focus ring defined"


def test_sr_only_helper_exists():
    css = _css()
    assert ".sr-only" in css, ".sr-only screen-reader helper missing"


def test_reduced_motion_block_exists():
    css = _css()
    assert "prefers-reduced-motion" in css, "no reduced-motion guard"
```

- [ ] **Step 2: Run, expect FAIL**

Run: `$env:PYTHONPATH = "."; pytest -q tests/test_ui_templates.py`
Expected: the four tests fail (none of these selectors exist in `styles.css` yet).

- [ ] **Step 3: Add the dark-theme token block**

In `document_search/web/static/styles.css`, immediately **after** the closing `}` of the `:root { … }` block (currently ends at line 29, the `--sh:` line then `}`), insert:

```css
/* ── Dark theme tokens ────────────────────────────── */
/* Applied when <html data-theme="dark"> (toggle or prefers-color-scheme). */
[data-theme="dark"] {
  --bg:       #0f172a;
  --surface:  #1e293b;
  --overlay:  #334155;
  --elevated: #1e293b;
  --bg-2:     #334155;
  --b-lo:     #334155;
  --b-md:     #475569;
  --b-hi:     #64748b;

  --txt-1: #f1f5f9;
  --txt-2: #cbd5e1;
  --txt-3: #94a3b8;

  --blue:    #3b82f6;
  --blue-dk: #2563eb;
  --blue-a:  #1e3a5f;
  --green:   #22c55e;
  --amber:   #f59e0b;
  --red:     #ef4444;

  --sh: 0 1px 4px rgba(0, 0, 0, .4);
}

/* Honour the OS preference until the user explicitly chooses a theme.
   app.js sets data-theme on <html> before paint when a saved choice exists;
   this block covers first paint / no-JS / no-saved-choice. */
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg:       #0f172a;
    --surface:  #1e293b;
    --overlay:  #334155;
    --elevated: #1e293b;
    --bg-2:     #334155;
    --b-lo:     #334155;
    --b-md:     #475569;
    --b-hi:     #64748b;
    --txt-1: #f1f5f9;
    --txt-2: #cbd5e1;
    --txt-3: #94a3b8;
    --blue:    #3b82f6;
    --blue-dk: #2563eb;
    --blue-a:  #1e3a5f;
    --green:   #22c55e;
    --amber:   #f59e0b;
    --red:     #ef4444;
    --sh: 0 1px 4px rgba(0, 0, 0, .4);
  }
}
```

- [ ] **Step 4: Add focus ring, sr-only, reduced-motion, theme-toggle, skeleton + utility classes**

At the **end** of `document_search/web/static/styles.css`, append:

```css
/* ── Accessibility: visible focus ring (WCAG 2.4.7) ── */
/* Keyboard focus only — :focus-visible avoids rings on mouse clicks. */
:focus-visible {
  outline: 2px solid var(--blue);
  outline-offset: 2px;
  border-radius: var(--r1);
}
/* Inputs already show a box-shadow on :focus; add the keyboard ring too. */
input:focus-visible, textarea:focus-visible, select:focus-visible {
  outline: 2px solid var(--blue);
  outline-offset: 1px;
}
/* Remove the legacy outline:none from .topbar/buttons that suppressed focus. */
a:focus-visible, button:focus-visible, .btn:focus-visible,
.tab:focus-visible, .nav-links a:focus-visible {
  outline: 2px solid var(--blue);
  outline-offset: 2px;
}

/* ── Screen-reader-only content (visually hidden, still announced) ── */
.sr-only {
  position: absolute;
  width: 1px; height: 1px;
  padding: 0; margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

/* ── Skip link (first tab stop) ── */
.skip-link {
  position: absolute; left: -999px; top: 0; z-index: 10000;
  background: var(--blue); color: #fff;
  padding: .5rem 1rem; border-radius: var(--r2);
  text-decoration: none; font-weight: 600;
}
.skip-link:focus { left: .5rem; top: .5rem; }

/* ── Theme toggle button (topbar) ── */
.theme-toggle {
  display: inline-flex; align-items: center; justify-content: center;
  width: 32px; height: 32px; flex-shrink: 0;
  color: var(--txt-2); background: none;
  border: 1px solid var(--b-lo); border-radius: var(--r2);
  cursor: pointer; transition: color .12s, background .12s, border-color .12s;
}
.theme-toggle:hover { color: var(--txt-1); background: var(--overlay); }
.theme-toggle svg { width: 16px; height: 16px; }
/* Show the correct glyph for the active theme. */
.theme-toggle .icon-moon { display: none; }
.theme-toggle .icon-sun  { display: block; }
[data-theme="dark"] .theme-toggle .icon-moon { display: block; }
[data-theme="dark"] .theme-toggle .icon-sun  { display: none; }

/* ── Loading skeletons ── */
.skeleton {
  background: linear-gradient(90deg,
    var(--overlay) 25%, var(--bg-2) 37%, var(--overlay) 63%);
  background-size: 400% 100%;
  border-radius: var(--r2);
  animation: skeleton-shimmer 1.4s ease infinite;
}
.skeleton-line   { height: .8rem; margin: .4rem 0; }
.skeleton-card   { height: 92px; margin-bottom: .75rem; }
.skeleton-w-40   { width: 40%; }
.skeleton-w-60   { width: 60%; }
.skeleton-w-80   { width: 80%; }
@keyframes skeleton-shimmer {
  0%   { background-position: 100% 50%; }
  100% { background-position: 0 50%; }
}

/* ── Error state (mirrors .empty but signals failure) ── */
.error-state     { text-align: center; padding: 3rem 1.5rem; color: var(--red); }
.error-state svg { width: 34px; height: 34px; margin: 0 auto .6rem; opacity: .5; display: block; }
.error-state p   { font-size: .9rem; }

/* ── Inline-style replacements: layout/utility helpers ── */
/* Single-column form grid (was style="grid-template-columns:1fr") */
.f-grid-1     { grid-template-columns: 1fr; }
/* Flex helpers (replace ad-hoc inline display:flex …) */
.row          { display: flex; align-items: center; gap: .5rem; }
.row-between  { display: flex; align-items: center; justify-content: space-between; }
.row-wrap     { display: flex; flex-wrap: wrap; gap: .4rem; }
.col-gap-sm   { display: flex; flex-direction: column; gap: .5rem; }
.mt-sm        { margin-top: .875rem; }
.mb-sm        { margin-bottom: .75rem; }
.full-span    { grid-column: 1 / -1; }
/* Eyebrow label used by tag-cloud / filters header */
.eyebrow {
  font-size: .75rem; font-weight: 600;
  text-transform: uppercase; letter-spacing: .05em; color: var(--txt-3);
}
/* Section header with a top border (was the "Optional filters" inline block) */
.section-divider {
  margin-top: .875rem; display: flex; align-items: center;
  border-top: 1px solid var(--b-lo); padding-top: .75rem;
}
.ml-auto      { margin-left: auto; }
.text-danger  { color: var(--red); }
.font-sm      { font-size: .82rem; }

/* ── Reduced motion (WCAG 2.3.3) ── */
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: .001ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: .001ms !important;
    scroll-behavior: auto !important;
  }
}
```

- [ ] **Step 5: Run, expect PASS**

Run: `$env:PYTHONPATH = "."; pytest -q tests/test_ui_templates.py`
Expected: all four tests pass.

- [ ] **Step 6: Manual sanity check**

Open `http://localhost:8080/` in a browser. Nothing visual changes yet (no toggle wired). In DevTools console run `document.documentElement.setAttribute('data-theme','dark')` — the whole page should flip to the dark palette. Run `document.documentElement.removeAttribute('data-theme')` to revert. Tab through the page: focus rings should now be visible on links/buttons.

- [ ] **Step 7: Commit**

```powershell
git add document_search/web/static/styles.css tests/test_ui_templates.py
git commit -m "feat(ui): add dark-theme tokens, focus ring, sr-only and utility classes"
```

---

## Task 2: Theme toggle wiring + flash-free init in app.js

**Files:**
- Modify: `document_search/web/static/app.js`
- Test: `tests/test_ui_templates.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ui_templates.py`:

```python
def _js() -> str:
    return (STATIC / "app.js").read_text(encoding="utf-8")


def test_theme_toggle_logic_present():
    js = _js()
    assert "seekr_theme" in js, "theme is not persisted under seekr_theme key"
    assert "data-theme" in js, "app.js never sets data-theme"
    assert "toggleTheme" in js, "toggleTheme() function missing"
```

- [ ] **Step 2: Run, expect FAIL**

Run: `$env:PYTHONPATH = "."; pytest -q tests/test_ui_templates.py::test_theme_toggle_logic_present`
Expected: FAIL — none of those tokens exist yet.

- [ ] **Step 3: Add the theme module to app.js**

At the **top** of `document_search/web/static/app.js`, **before** the `class ChipInput {` line (line 1), insert:

```javascript
// ── Theme (dark mode) ──────────────────────────────────────────────
// Applied as early as possible to minimise flash. A saved choice wins;
// otherwise the CSS prefers-color-scheme media query covers first paint.
(function initTheme() {
  const saved = localStorage.getItem('seekr_theme');
  if (saved === 'dark' || saved === 'light') {
    document.documentElement.setAttribute('data-theme', saved);
  }
})();

function toggleTheme() {
  const root = document.documentElement;
  const current = root.getAttribute('data-theme')
    || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  const next = current === 'dark' ? 'light' : 'dark';
  root.setAttribute('data-theme', next);
  localStorage.setItem('seekr_theme', next);
  const btn = document.getElementById('themeToggle');
  if (btn) btn.setAttribute('aria-label', next === 'dark' ? 'Switch to light theme' : 'Switch to dark theme');
}
```

> Note: `initTheme` runs at script-parse time. `app.js` is loaded at the end of `<body>`, so a saved dark choice may flash light for one frame on slow loads. This is acceptable for a self-hosted single-user tool; a `<head>` inline snippet is the zero-flash alternative but adds duplicated markup to every template and is out of scope here.

- [ ] **Step 4: Run, expect PASS**

Run: `$env:PYTHONPATH = "."; pytest -q tests/test_ui_templates.py::test_theme_toggle_logic_present`
Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add document_search/web/static/app.js tests/test_ui_templates.py
git commit -m "feat(ui): persist and toggle dark/light theme in app.js"
```

---

## Task 3: Accessible toasts + progress (aria-live), aria-busy, Escape-to-close

**Files:**
- Modify: `document_search/web/static/app.js`
- Test: `tests/test_ui_templates.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ui_templates.py`:

```python
def test_app_js_has_escape_and_aria():
    js = _js()
    assert "Escape" in js, "no Escape key handler bound"
    assert "aria-expanded" in js, "filter toggle never updates aria-expanded"


def test_toast_wrap_is_live_region_in_templates():
    # Every page that has a toast wrap must mark it as an aria-live region.
    for name in ("index.html", "search.html", "ingest.html", "config.html"):
        html = (TEMPLATES / name).read_text(encoding="utf-8")
        assert 'id="toastWrap"' in html
        assert 'aria-live="polite"' in html, f"{name} toastWrap not aria-live"
```

> The toast template assertion is satisfied in Task 4 (template edits). It is grouped here because it documents the contract `showToast` relies on. Expect it to stay red until Task 4 — that is intentional; do not weaken it.

- [ ] **Step 2: Run, expect FAIL**

Run: `$env:PYTHONPATH = "."; pytest -q tests/test_ui_templates.py::test_app_js_has_escape_and_aria`
Expected: FAIL.

- [ ] **Step 3: Make `showToast` announce + auto-clear the live region**

The toast wrap becomes the live region itself (Task 4 adds `aria-live="polite"` to it). `showToast` already appends a child node into `#toastWrap`; appending text into a polite live region is announced automatically. Harden it so the visible text and the announced text match, and so screen readers re-announce repeated identical messages. Replace `showToast` (currently lines ~118-129) with:

```javascript
// ── Toast notifications (announced via #toastWrap aria-live region) ─
function showToast(msg, type = 'info', duration = 3500) {
  const wrap = document.getElementById('toastWrap');
  if (!wrap) return;
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  t.setAttribute('role', type === 'err' ? 'alert' : 'status');
  const dot = document.createElement('div');
  dot.className = 'toast-dot';
  dot.setAttribute('aria-hidden', 'true');
  const span = document.createElement('span');
  span.textContent = msg;              // textContent: no HTML injection, exact announce text
  t.appendChild(dot);
  t.appendChild(span);
  wrap.appendChild(t);
  setTimeout(() => {
    t.style.opacity = '0'; t.style.transition = 'opacity .25s';
    setTimeout(() => t.remove(), 280);
  }, duration);
}
```

> This also fixes a latent XSS-adjacent issue: the old version used `innerHTML` with an `escHtml`'d message; `textContent` is simpler and strictly safe.

- [ ] **Step 4: Mark async regions busy + drive `aria-expanded` on the filter toggle**

Replace `toggleFilters` (currently lines ~233-241) with:

```javascript
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
```

In `runSearch` (currently starts ~line 254), set `aria-busy` on the results region around the fetch. Change the start of the `try` block so that right after `const resultsEl = document.getElementById('results');` you add:

```javascript
  if (resultsEl) resultsEl.setAttribute('aria-busy', 'true');
```

…and in **both** the success path (just before `renderResults(data);` and before the early `return` for the empty case) and the `catch` block, clear it. The cleanest way: wrap the existing body in `try { … } finally { if (resultsEl) resultsEl.removeAttribute('aria-busy'); }`. Concretely, replace the whole `runSearch` function with:

```javascript
async function runSearch() {
  const resultsEl = document.getElementById('results');
  if (resultsEl) {
    resultsEl.setAttribute('aria-busy', 'true');
    resultsEl.innerHTML = skeletonResults(3);
  }
  try {
    const payload = {
      query: query.value, limit: 25,
      filetype: chipFiletype?.values().join(',') || null,
      path: pathFilter.value || null,
      block_type: blockType.value || null,
      modified_from: modifiedFrom.value || null,
      modified_to: modifiedTo.value || null,
      tags: chipTagFilter?.values() ?? [],
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
        <div class="empty" role="status">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
          </svg>
          <p>No results found for this query.</p>
        </div>`;
      return;
    }

    renderResults(data);
  } catch (e) {
    if (resultsEl) {
      resultsEl.innerHTML = `
        <div class="error-state" role="alert">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
          </svg>
          <p>${escHtml(e.message)}</p>
        </div>`;
    }
  } finally {
    if (resultsEl) resultsEl.removeAttribute('aria-busy');
  }
}
```

Add a small skeleton helper directly **above** `runSearch`:

```javascript
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
```

- [ ] **Step 5: Bind Escape-to-close for the expandable panels**

In `initSearchPage` (currently ~line 1696), add an Escape handler alongside the existing `/` shortcut. Inside `initSearchPage`, after the existing `document.addEventListener('keydown', …)` for `/`, add:

```javascript
  // Escape: close the filter panel if open, else clear focus from the query box
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    const body = document.getElementById('filterBody');
    if (body && !body.classList.contains('hidden')) {
      toggleFilters();
      document.getElementById('filterToggle')?.focus();
    } else if (document.activeElement === queryEl) {
      queryEl.blur();
    }
  });
```

Also add a global Escape handler for the dismissable cards (AI suggestion, change-password). Inside `bootstrap()` (currently ~line 1716), after `initNav();`, add:

```javascript
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    const aiCard = document.getElementById('aiSuggestionCard');
    if (aiCard && !aiCard.classList.contains('hidden')) { dismissAiSuggestion(); return; }
    const pwCard = document.getElementById('changePwCard');
    if (pwCard && !pwCard.classList.contains('hidden')) { cancelChangePassword(); return; }
  });
```

- [ ] **Step 6: Run, expect PASS (escape/aria test)**

Run: `$env:PYTHONPATH = "."; pytest -q tests/test_ui_templates.py::test_app_js_has_escape_and_aria`
Expected: PASS. (`test_toast_wrap_is_live_region_in_templates` stays red until Task 4.)

- [ ] **Step 7: Commit**

```powershell
git add document_search/web/static/app.js tests/test_ui_templates.py
git commit -m "feat(ui): aria-live toasts, aria-busy results, escape-to-close, skeletons"
```

---

## Task 4: Shared auth-gate partial + landmarks + theme toggle in templates

This is the biggest markup change. The auth gate is duplicated in `index.html`, `search.html`, `ingest.html`, `config.html` (NOT `wiki.html`, which is public). Each copy differs only in the page heading + subtitle. The partial takes those as Jinja variables with defaults.

**Files:**
- Create: `document_search/web/templates/_partials/auth_gate.html`
- Modify: `index.html`, `search.html`, `ingest.html`, `config.html`, `wiki.html`
- Test: `tests/test_ui_templates.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ui_templates.py`:

```python
GATED = ("index.html", "search.html", "ingest.html", "config.html")
ALL_PAGES = GATED + ("wiki.html",)


def test_auth_gate_partial_exists():
    assert (TEMPLATES / "_partials" / "auth_gate.html").exists()


def test_no_template_inlines_the_duplicated_auth_gate():
    # The duplicated literal was the sign-in card head subtitle text.
    for name in GATED:
        html = (TEMPLATES / name).read_text(encoding="utf-8")
        assert 'include "_partials/auth_gate.html"' in html, f"{name} must include the partial"
        # The old duplicated markers must be gone from the page body.
        assert html.count('id="authGate"') == 0, f"{name} still inlines the auth gate"


def test_every_page_has_main_landmark_and_skip_link():
    for name in ALL_PAGES:
        html = (TEMPLATES / name).read_text(encoding="utf-8")
        assert 'id="main"' in html, f"{name} missing main landmark id"
        assert "skip-link" in html, f"{name} missing skip link"


def test_every_page_has_theme_toggle():
    for name in ALL_PAGES:
        html = (TEMPLATES / name).read_text(encoding="utf-8")
        assert 'id="themeToggle"' in html, f"{name} missing theme toggle"
```

Plus the still-red `test_toast_wrap_is_live_region_in_templates` from Task 3.

- [ ] **Step 2: Run, expect FAIL**

Run: `$env:PYTHONPATH = "."; pytest -q tests/test_ui_templates.py`
Expected: the new template tests + the toast-live test fail.

- [ ] **Step 3: Create the partial**

Create `document_search/web/templates/_partials/auth_gate.html`:

```html
{# Shared sign-in gate. Hidden via JS once authenticated.
   Variables (all optional, with sensible defaults):
     gate_title    – the <h1> shown above the card
     gate_subtitle – the muted line under the <h1>
     gate_note     – the small text under "Sign in" in the card head #}
<section id="authGate" class="auth-wrap" aria-labelledby="authGateHeading">
  <div class="pg-head">
    <h1 id="authGateHeading">{{ gate_title | default("Welcome to Seekr") }}</h1>
    <p>{{ gate_subtitle | default("Sign in to access your document index.") }}</p>
  </div>
  <div class="card">
    <div class="card-head">
      <div class="card-ico" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
      </div>
      <div class="card-titles">
        <h2>Sign in</h2>
        <p>{{ gate_note | default("Authentication required") }}</p>
      </div>
    </div>
    <div class="card-body">
      <form class="login-form" onsubmit="event.preventDefault(); login();">
        <div class="f-grid f-grid-1">
          <div class="f-col">
            <label class="f-label" for="username">Username</label>
            <input id="username" name="username" placeholder="admin" value="admin" autocomplete="username" />
          </div>
          <div class="f-col">
            <label class="f-label" for="password">Password</label>
            <input id="password" name="password" type="password" placeholder="••••••••" autocomplete="current-password" />
          </div>
        </div>
        <div class="btn-row">
          <button type="submit" class="btn btn-p">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 3h4a2 2 0 012 2v14a2 2 0 01-2 2h-4"/><polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/></svg>
            Sign in
          </button>
        </div>
        <p id="loginResult" class="feedback" role="status" aria-live="polite"></p>
      </form>
    </div>
  </div>
</section>
```

> Wrapping the inputs in a `<form>` with `onsubmit` gives Enter-to-submit and proper semantics for free. `login()` already reads `username.value` / `password.value` by element id, so no JS change is needed.

- [ ] **Step 4: Replace the topbar + auth gate in each gated page**

For **each** of `index.html`, `search.html`, `ingest.html`, `config.html`:

1. Add a skip link as the very first child of `<body>` (before `<nav class="topbar">`):

```html
  <a class="skip-link" href="#main">Skip to main content</a>
```

2. Convert the topbar to a labelled landmark and add the theme toggle. Change the opening nav tag from `<nav class="topbar">` to `<nav class="topbar" aria-label="Primary">`, and **inside** `.nav-links`, immediately before `<div id="navSep" …>`, insert the toggle:

```html
        <button id="themeToggle" class="theme-toggle" type="button" onclick="toggleTheme()" aria-label="Toggle dark theme">
          <svg class="icon-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>
          <svg class="icon-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
        </button>
```

3. Decorative nav SVGs: add `aria-hidden="true"` to each `<svg>` directly inside a `.nav-links a` and the `.brand` svg (they sit next to text labels, so the icon is redundant to a screen reader). Example for the brand link:

```html
      <a class="brand" href="/">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        Seekr
      </a>
```

4. Give `<main>` an id and `tabindex="-1"` (so the skip link can move focus to it): change `<main class="main">` to `<main class="main" id="main" tabindex="-1">`.

5. Replace the entire inlined auth-gate `<div id="authGate" …> … </div>` block with an include carrying that page's heading. Use these exact values:

`index.html`:
```html
      {% include "_partials/auth_gate.html" with context %}
```
(defaults already match the dashboard copy: "Welcome to Seekr" / "Sign in to access your document index.")

`search.html` — set the page-specific vars right before the include:
```html
      {% set gate_title = "Search Documents" %}
      {% set gate_subtitle = "Sign in to start searching your index." %}
      {% include "_partials/auth_gate.html" with context %}
```

`ingest.html`:
```html
      {% set gate_title = "Upload & Index" %}
      {% set gate_subtitle = "Sign in to upload files and manage indexing." %}
      {% include "_partials/auth_gate.html" with context %}
```

`config.html`:
```html
      {% set gate_title = "Configuration" %}
      {% set gate_subtitle = "Sign in to manage system settings." %}
      {% include "_partials/auth_gate.html" with context %}
```

6. Mark the toast wrap as a live region (this satisfies the Task 3 test). Change `<div class="toast-wrap" id="toastWrap"></div>` to:

```html
  <div class="toast-wrap" id="toastWrap" aria-live="polite" aria-atomic="false"></div>
```

- [ ] **Step 5: Update `wiki.html` (landmarks + toggle only, NO auth gate)**

In `wiki.html`: add the same skip link before the topbar, add `aria-label="Primary"` to the nav, insert the `#themeToggle` button in `.nav-links`, add `aria-hidden="true"` to decorative nav/brand SVGs, change `<main class="main">` to `<main class="main" id="main" tabindex="-1">`, and (if a `toastWrap` exists) add `aria-live="polite"`. Do **not** add an auth gate — the wiki is intentionally public.

- [ ] **Step 6: Run, expect PASS**

Run: `$env:PYTHONPATH = "."; pytest -q tests/test_ui_templates.py`
Expected: all template/partial/landmark/toggle/toast tests pass.

- [ ] **Step 7: Verify the pages still render server-side**

Run: `$env:PYTHONPATH = "."; pytest -q tests/test_app_status.py tests/test_app_search.py`
Then a live render smoke test (the include must resolve):

```powershell
$env:PYTHONPATH = "."
python -c "from fastapi.testclient import TestClient; from document_search.app import create_app; import tempfile, os; c = TestClient(create_app(os.path.join(tempfile.mkdtemp(),'i.db'))); [print(p, c.get(p).status_code) for p in ('/','/search','/ingest','/config','/wiki')]"
```

Expected: every route prints `200`. A Jinja `TemplateNotFound` or undefined-variable error would surface here.

- [ ] **Step 8: Commit**

```powershell
git add document_search/web/templates/ tests/test_ui_templates.py
git commit -m "feat(ui): shared auth-gate partial, landmarks, skip links, theme toggle"
```

---

## Task 5: Replace remaining inline styles with utility classes

Task 4 already removed the auth-gate inline styles. This task sweeps the rest of the inline `style=` usages in the page bodies (search filters header, tag-cloud header, dashboard "no recent searches", etc.) and the recurring `style=` strings emitted from `app.js` render helpers, replacing them with the utility classes added in Task 1.

**Files:**
- Modify: `search.html`, `index.html`, `ingest.html`, `config.html`, `app.js`
- Test: `tests/test_ui_templates.py` (extend)

- [ ] **Step 1: Write the failing budget test**

Append to `tests/test_ui_templates.py`:

```python
def test_inline_style_budget():
    # Baseline before this work was ~61 across templates. After consolidation
    # the page-body inline styles should be largely gone. Allow a small budget
    # for genuinely dynamic styles that must stay inline.
    total = 0
    for name in ALL_PAGES:
        html = (TEMPLATES / name).read_text(encoding="utf-8")
        total += html.count("style=")
    assert total <= 8, f"too many inline styles remain in templates: {total}"
```

> The budget of 8 is deliberate headroom for the handful of inline styles that are awkward to classfy (e.g. one-off width tweaks on config widgets). The goal is "almost none", not zero-at-any-cost. If you finish well under budget, do not pad it.

- [ ] **Step 2: Run, expect FAIL**

Run: `$env:PYTHONPATH = "."; pytest -q tests/test_ui_templates.py::test_inline_style_budget`
Expected: FAIL (~50+ inline styles still present).

- [ ] **Step 3: Sweep `search.html`**

Apply these exact replacements:

- The query header row:
  - From: `<div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:.2rem;">`
  - To:   `<div class="row-between" style="margin-bottom:.2rem;">` → then drop the margin too by using `<div class="row-between mb-2px">` only if you add `.mb-2px{margin-bottom:.2rem;}`. Simpler: keep the class and accept the tiny inline margin within budget, **or** add `.mb-2px { margin-bottom: .2rem; }` to `styles.css` and use `<div class="row-between mb-2px">`. Choose the class form to stay under budget.
- `<div class="f-col" style="gap:.5rem;">` → `<div class="f-col col-gap-sm">` (note: `.f-col` already sets flex-direction column + gap .35rem; `.col-gap-sm` overrides the gap — ensure `.col-gap-sm` is defined after `.f-col` in the stylesheet, which it is since Task 1 appended at end).
- The "Optional filters" divider:
  - From: `<div style="margin-top:.875rem; display:flex; align-items:center; border-top:1px solid var(--b-lo); padding-top:.75rem;">`
  - To:   `<div class="section-divider">`
  - And `<span style="font-size:.79rem; color:var(--txt-3);">Optional filters</span>` → `<span class="f-hint">Optional filters</span>`
  - And `<button id="filterToggle" class="filter-toggle-btn" onclick="toggleFilters()" style="margin-left:auto;">` → `<button id="filterToggle" class="filter-toggle-btn ml-auto" type="button" onclick="toggleFilters()" aria-expanded="false" aria-controls="filterBody">`
- `<div id="filterBody" class="hidden" style="margin-top:.875rem;">` → `<div id="filterBody" class="hidden mt-sm" aria-hidden="true">`
- Tag-cloud card: `<div id="tagCloudCard" class="card hidden" style="margin-bottom:.75rem;">` → `<div id="tagCloudCard" class="card hidden mb-sm">`
  - `<div class="card-body" style="padding:.75rem 1rem;">` → keep as-is **or** add `.card-body-tight { padding:.75rem 1rem; }` and use it. Prefer the class.
  - `<div style="display:flex;align-items:center;gap:.5rem;margin-bottom:.5rem;">` → `<div class="row mb-sm">`
  - `<span style="font-size:.75rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--txt-3);">Browse by tag</span>` → `<span class="eyebrow">Browse by tag</span>`
  - `<div id="tagCloud" style="display:flex;gap:.4rem;flex-wrap:wrap;"></div>` → `<div id="tagCloud" class="row-wrap"></div>`

Add to `styles.css` (end of file) the two small extras referenced above:

```css
.mb-2px        { margin-bottom: .2rem; }
.card-body-tight { padding: .75rem 1rem; }
```

- [ ] **Step 4: Sweep `index.html`, `ingest.html`, `config.html`**

- `index.html`: `<div id="recentSearches"><p class="muted" style="font-size:.82rem;">No recent searches yet.</p></div>` → use the `.font-sm` class: `<p class="muted font-sm">No recent searches yet.</p>`.
- The single-column `f-grid`: every `<div class="f-grid" style="grid-template-columns:1fr;">` left in `ingest.html`/`config.html` is now inside the partial, but if any remain on the pages themselves, replace with `<div class="f-grid f-grid-1">`.
- For any remaining `style="…"` on these pages, map to the Task-1 utilities (`.row`, `.row-between`, `.row-wrap`, `.mt-sm`, `.mb-sm`, `.full-span`, `.text-danger`, `.font-sm`, `.eyebrow`). Inspect with:

  Run: `$env:PYTHONPATH = "."; pytest -q tests/test_ui_templates.py::test_inline_style_budget` and iterate, OR list them: use the Grep tool for `style=` scoped to `document_search/web/templates`.

- [ ] **Step 5: Sweep the worst `app.js` inline-style emitters (optional, quality)**

`app.js` render helpers emit `style="…"` strings (e.g. `renderPathList`, `renderUserTable`, the `path-test-item` badge color). These are **not** counted by the template budget test (they live in JS), so they are optional for passing tests but recommended for consistency. The highest-value, lowest-risk swaps:

- `<button class="btn btn-g btn-sm" style="color:var(--red)" …>Remove</button>` → `<button class="btn btn-g btn-sm text-danger" …>Remove</button>` (appears in `renderPathList`, `renderUserTable`, `renderHaKeysTable`, `renderModelLibrary`).
- `<div ... style="display:flex;gap:.3rem;flex-wrap:wrap;">` in `renderModelLibrary` → `class="row-wrap"`.

Leave the genuinely dynamic ones (computed badge `color:` based on status) inline — they are data-driven and a class-per-color would be noise. Document this decision in the commit body.

- [ ] **Step 6: Run, expect PASS**

Run: `$env:PYTHONPATH = "."; pytest -q tests/test_ui_templates.py`
Expected: all pass, including `test_inline_style_budget`.

- [ ] **Step 7: Manual visual regression check**

Load `/`, `/search`, `/ingest`, `/config` in both light and dark theme. Confirm the filters divider, tag cloud, and recent-searches blocks look identical to before (same spacing). Toggle the filter panel — layout must not shift.

- [ ] **Step 8: Commit**

```powershell
git add document_search/web/templates/ document_search/web/static/styles.css document_search/web/static/app.js tests/test_ui_templates.py
git commit -m "style(ui): move inline styles into reusable utility classes"
```

---

## Task 6: Loading skeletons + consistent empty/error states across pages

Task 3 already added skeletons + error state to `runSearch`. This task extends the pattern to the other async-populated regions so the UI never shows a bare blank during a fetch.

**Files:**
- Modify: `document_search/web/static/app.js`
- Test: `tests/test_ui_templates.py` (extend)

- [ ] **Step 1: Write the test**

Append to `tests/test_ui_templates.py`:

```python
def test_loading_and_error_helpers_present():
    js = _js()
    assert "skeletonResults" in js, "skeleton helper missing"
    assert "error-state" in js, "no consistent error-state markup in app.js"
    assert 'role="alert"' in js, "error states not announced as alerts"
```

- [ ] **Step 2: Run, expect FAIL (or partial)**

Run: `$env:PYTHONPATH = "."; pytest -q tests/test_ui_templates.py::test_loading_and_error_helpers_present`
Expected: passes only the parts added in Task 3; ensure all three asserts hold after this task.

- [ ] **Step 3: Add a generic skeleton + error helper and apply to user/tag-cloud loads**

Add near `skeletonResults` (top of the search section):

```javascript
function skeletonLines(n = 3) {
  let html = '<div aria-hidden="true">';
  for (let i = 0; i < n; i++) html += '<div class="skeleton skeleton-line skeleton-w-80"></div>';
  return html + '</div>';
}

function errorState(message) {
  return `<div class="error-state" role="alert">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
    </svg>
    <p>${escHtml(message)}</p>
  </div>`;
}
```

In `loadUsers` (currently ~line 1004), show a skeleton while loading and a consistent error state on failure:

```javascript
async function loadUsers() {
  const el = document.getElementById('userTable');
  if (el) { el.setAttribute('aria-busy', 'true'); el.innerHTML = skeletonLines(4); }
  try {
    const users = await api('/api/users');
    renderUserTable(users);
  } catch (e) {
    if (el) el.innerHTML = errorState(e.message);
    setText('usersResult', e.message, 'err');
  } finally {
    if (el) el.removeAttribute('aria-busy');
  }
}
```

In `loadTagCloud` (currently ~line 1761), the empty path already hides the card; add an error fallback by replacing the bare `catch (_) {}` with:

```javascript
  } catch (e) {
    cloud.innerHTML = errorState('Could not load tags');
  }
```

- [ ] **Step 4: Run, expect PASS**

Run: `$env:PYTHONPATH = "."; pytest -q tests/test_ui_templates.py::test_loading_and_error_helpers_present`
Expected: PASS.

- [ ] **Step 5: Manual check**

In DevTools, throttle network to "Slow 3G", open the Config → Users tab: a shimmer skeleton should appear before the table. Stop the backend and reload Users: the red error-state card should show and be announced (VoiceOver/NVDA reads "alert").

- [ ] **Step 6: Commit**

```powershell
git add document_search/web/static/app.js tests/test_ui_templates.py
git commit -m "feat(ui): loading skeletons and consistent empty/error states"
```

---

## Task 7: Full suite + manual a11y walkthrough

**Files:** none (verification only)

- [ ] **Step 1: Full suite**

Run: `$env:PYTHONPATH = "."; pytest -q`
Expected: green. The new `tests/test_ui_templates.py` plus all pre-existing tests pass.

- [ ] **Step 2: Keyboard-only walkthrough (manual, required)**

Start the app (`uvicorn document_search.app:app --port 8080`). Using **only** the keyboard:
1. Press Tab once on page load → the "Skip to main content" link appears and is focused.
2. Activate it (Enter) → focus jumps into `<main>`.
3. Tab through the topbar → each nav item and the theme toggle shows a visible blue focus ring.
4. Activate the theme toggle (Enter/Space) → palette flips; the toggle's `aria-label` updates; reload → choice persists.
5. On `/search`: press `/` → focus moves to the query box. Type, press Enter → results load (skeleton first). Press Escape → if the filter panel is open it closes and focus returns to the toggle button; otherwise the query box blurs.
6. Tab to "Show filters", activate → panel opens, `aria-expanded` becomes `true` (verify in DevTools).

- [ ] **Step 3: Screen-reader spot check (manual, required)**

With NVDA (Windows) or VoiceOver (macOS):
- Trigger a toast (e.g. sign in) → it is announced politely without stealing focus.
- Trigger an error toast (bad password) → announced as an alert.
- Run a search → "results" region change is perceivable; empty state reads "No results found for this query."

- [ ] **Step 4: axe-core automated audit (manual, recommended)**

In Chrome DevTools console on each page (`/`, `/search`, `/ingest`, `/config`, `/wiki`), in both themes:

```javascript
var s = document.createElement('script');
s.src = 'https://cdn.jsdelivr.net/npm/axe-core@4/axe.min.js';
s.onload = () => axe.run().then(r => console.log('violations:', r.violations));
document.body.appendChild(s);
```

Expected: zero **critical/serious** violations. Note any moderate items in the PR description. (This is an offline-hosted tool; only run this against a local dev instance with network access, never bundle the CDN into the app.)

- [ ] **Step 5: Contrast check (manual, required for AA)**

Using DevTools' contrast inspector or a contrast checker, verify in **dark** theme that:
- `--txt-2` on `--surface` ≥ 4.5:1 (body text).
- `--txt-3` on `--bg` ≥ 3:1 (it is used for small/decorative text only; if any normal-size body text uses `--txt-3`, it must hit 4.5:1 — adjust the dark `--txt-3` toward `#a8b4c4` if a violation is found).
- `--blue` text/links on `--surface` ≥ 4.5:1.

Record results in the PR. If any token fails, nudge the dark-theme value and re-run; do not ship a failing contrast.

- [ ] **Step 6: No commit (verification only)**

---

## Definition of Done

- [ ] `$env:PYTHONPATH = "."; pytest -q` is green on a clean clone (includes `tests/test_ui_templates.py`).
- [ ] All 5 page routes (`/`, `/search`, `/ingest`, `/config`, `/wiki`) render HTTP 200 with the partial resolved.
- [ ] The auth-gate markup exists in exactly one place (`_partials/auth_gate.html`); no page inlines `id="authGate"`.
- [ ] Dark mode: `[data-theme="dark"]` token block overrides `:root`; toggle in the topbar persists to `localStorage['seekr_theme']`; `prefers-color-scheme` is honoured on first paint with no saved choice.
- [ ] Inline-style budget in templates ≤ 8 (down from ~61).
- [ ] Loading skeletons appear for search results and the Users table; empty + error states are consistent and announced.

### WCAG 2.1 AA checklist (manual unless noted)
- [ ] **1.3.1 Info & Relationships** — semantic landmarks present: `<nav aria-label="Primary">`, `<main id="main">`, auth gate as a labelled `<section>`. (partially auto: `test_every_page_has_main_landmark_and_skip_link`)
- [ ] **1.4.3 Contrast (Minimum)** — light and dark themes pass 4.5:1 for body text, 3:1 for large/UI (Task 7 Step 5).
- [ ] **2.1.1 Keyboard** — all interactive controls reachable and operable by keyboard; forms submit on Enter (Task 7 Step 2).
- [ ] **2.1.2 No Keyboard Trap** — Escape closes the filter panel and dismissable cards; focus returns sensibly.
- [ ] **2.4.1 Bypass Blocks** — skip link present and functional on every page. (partially auto)
- [ ] **2.4.7 Focus Visible** — `:focus-visible` ring visible on every interactive element (auto: `test_focus_visible_ring_exists`; visual: Task 7 Step 2).
- [ ] **2.3.3 Animation from Interactions** — `prefers-reduced-motion` disables shimmer/transitions (auto: `test_reduced_motion_block_exists`).
- [ ] **4.1.3 Status Messages** — toasts use `aria-live="polite"` / `role="alert"`; async regions set `aria-busy`; the filter toggle exposes `aria-expanded` (auto: toast + escape/aria tests; manual: Task 7 Step 3).
- [ ] **3.3.2 Labels or Instructions** — every input has an associated `<label for>` (already true; preserved in the partial).
- [ ] Decorative SVGs carry `aria-hidden="true"`; icon-only theme toggle has an `aria-label`.

---

## Notes for the executing agent

- **No JS test runner exists.** The pytest checks in `tests/test_ui_templates.py` assert structural invariants on the *files*, not runtime DOM behaviour. They are guardrails against regressions (e.g. someone re-inlining the auth gate), not proof of accessibility. The DoD's manual steps are mandatory, not optional.
- **`with context` on the include is required** because the auth-gate partial uses `{% set %}` variables defined in the including template. Without `with context`, Jinja renders an isolated scope and your per-page `gate_title` would be ignored. The `index.html` include needs no `set` block — it relies on the partial's `default(...)` filters.
- **Theme flash:** `initTheme()` runs when `app.js` is parsed, which is at end-of-`<body>`. A saved dark theme can flash light for one frame. The zero-flash fix is a tiny inline `<script>` in each `<head>`, but that re-introduces per-template duplication, so it is intentionally out of scope. If a future task adds a base template (`{% extends %}`), move the snippet there.
- **Wiki is public — no auth gate.** Do not add `_partials/auth_gate.html` to `wiki.html`. It gets landmarks, skip link, and the theme toggle only. The `GATED` tuple in the tests encodes this; keep wiki out of it.
- **Dynamic inline styles in `app.js` stay inline.** Status-driven `color:` (green/amber/red badges computed from data) are data, not styling decisions — converting them to classes would be noise. Only the *static, repeated* inline styles (`color:var(--red)` on Remove buttons, fixed flex wrappers) should become classes. The template budget test does not police `app.js`, so use judgement.
- **`.col-gap-sm` / `.f-grid-1` override ordering:** these utilities must appear *after* `.f-col` / `.f-grid` in the cascade to win on equal specificity. Task 1 appends them at the end of `styles.css`, so ordering is correct — do not move the base rules below the utilities.
- **Contrast is the one place dark mode can silently fail AA.** The chosen dark `--txt-3` (`#94a3b8`) on `--bg` (`#0f172a`) is borderline for *small* text; it is only used for decorative/secondary labels in the current design. If Task 7 Step 5 flags any normal-size body copy using `--txt-3`, lighten it to ~`#a8b4c4` rather than recolouring individual elements.
- **Keep diffs reviewable:** commit per task as specified. The Task 4 template sweep is large but mechanical — if a reviewer balks at the size, splitting "partial extraction" from "landmarks/ARIA" into two commits is acceptable.
