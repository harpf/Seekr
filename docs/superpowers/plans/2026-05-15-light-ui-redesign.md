# Seekr Light UI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the existing dark navy theme with a clean light/minimal design system across all Seekr pages, fix the wiki 500 error, add a Swagger iframe to the wiki, and handle 401 errors gracefully.

**Architecture:** Full rewrite of `styles.css` with a light token set; six HTML templates updated for the new system (mostly class-name-compatible, with targeted additions for the Swagger iframe, wiki fixes, and config tab bar); `app.js` extended to catch 401 responses before raw JSON reaches the DOM. No backend changes.

**Tech Stack:** Vanilla CSS (custom properties), Jinja2 templates (FastAPI), vanilla JS. Tests: `pytest -q` for backend regression; visual browser check for frontend.

---

## File Map

| File | Action |
|---|---|
| `document_search/web/static/styles.css` | Full rewrite |
| `document_search/web/static/app.js` | Modify `api()` — 401 handling |
| `document_search/web/templates/wiki.html` | Fix Jinja2 500 + fix variable names in `<style>` + add Swagger iframe card |
| `document_search/web/templates/config.html` | No HTML changes — CSS drives pill tab bar |
| `document_search/web/templates/index.html` | No HTML changes needed |
| `document_search/web/templates/search.html` | No HTML changes needed |
| `document_search/web/templates/ingest.html` | No HTML changes needed |

---

## Task 1: Fix wiki.html Jinja2 500

The HA automation YAML code block in `wiki.html` contains `{% if %}`, `{% for %}`, and `{{ }}` which Jinja2 tries to evaluate at render time, causing a 500.

**Files:**
- Modify: `document_search/web/templates/wiki.html:200-207`

- [ ] **Step 1: Wrap the HA automation YAML in a raw block**

In `wiki.html`, find the `<pre><code>` block that starts at line ~201 (the `message: >` YAML example). Replace it with:

```html
            <pre><code>{% raw %}message: >
  {% if r.content.answer %}{{ r.content.answer }}{% endif %}

  Sources:
  {% for s in r.content.sources %}
  • {{ s.filename }} ({{ s.modified_at }})
  {% endfor %}{% endraw %}</code></pre>
```

- [ ] **Step 2: Verify the fix**

Start the dev server and navigate to `http://localhost:8000/wiki` (or whichever port is configured). Confirm you see the wiki page content instead of a 500 error.

- [ ] **Step 3: Run backend tests to confirm nothing broke**

```
pytest -q
```

Expected: all existing tests pass (this change is template-only).

- [ ] **Step 4: Commit**

```
git add document_search/web/templates/wiki.html
git commit -m "fix: escape Jinja2 template syntax in wiki HA YAML example"
```

---

## Task 2: Rewrite styles.css — light design system

Full rewrite. Every selector is covered. No existing class names are removed, so JS selectors stay valid.

**Files:**
- Modify: `document_search/web/static/styles.css` (full file replacement)

- [ ] **Step 1: Replace the entire content of styles.css**

```css
/* ═══════════════════════════════════════════════════
   SEEKR — Light Design System
   ═══════════════════════════════════════════════════ */

/* ── Tokens ───────────────────────────────────────── */
:root {
  --bg:       #f8f9fc;
  --surface:  #ffffff;
  --overlay:  #f1f4f8;
  --elevated: #ffffff;
  --b-lo:     #e2e8f0;
  --b-md:     #cbd5e1;
  --b-hi:     #94a3b8;

  --txt-1: #0f172a;
  --txt-2: #475569;
  --txt-3: #94a3b8;

  --blue:    #2563eb;
  --blue-dk: #1d4ed8;
  --blue-a:  #eff6ff;
  --green:   #16a34a;
  --amber:   #d97706;
  --red:     #dc2626;

  --r1: 4px; --r2: 8px; --r3: 10px; --r4: 12px;
  --sh: 0 1px 4px rgba(0, 0, 0, .06);
}

/* ── Reset ────────────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { scroll-behavior: smooth; }

body {
  font-family: Inter, system-ui, -apple-system, sans-serif;
  background: var(--bg);
  color: var(--txt-1);
  min-height: 100vh;
  line-height: 1.55;
  font-size: 15px;
}

/* ── Layout ───────────────────────────────────────── */
.wrap      { display: flex; flex-direction: column; min-height: 100vh; }
.container { max-width: 1080px; margin: 0 auto; padding: 0 1.25rem; }
.main      { flex: 1; padding: 2rem 0 5rem; }

/* ── Topbar ───────────────────────────────────────── */
.topbar {
  position: sticky; top: 0; z-index: 100;
  background: var(--surface);
  border-bottom: 1px solid var(--b-lo);
  box-shadow: 0 1px 3px rgba(0, 0, 0, .06);
}
.topbar-inner {
  display: flex; align-items: center;
  justify-content: space-between;
  height: 56px; gap: 1rem;
}
.brand {
  display: flex; align-items: center; gap: .5rem;
  color: var(--txt-1); text-decoration: none;
  font-weight: 700; font-size: 1.0625rem; letter-spacing: -.02em;
}
.brand svg { width: 20px; height: 20px; color: var(--blue); flex-shrink: 0; }

.nav-links { display: flex; gap: .1rem; }
.nav-links a {
  display: flex; align-items: center; gap: .375rem;
  color: var(--txt-2); text-decoration: none;
  padding: .325rem .65rem; border-radius: var(--r2);
  font-size: .8125rem; font-weight: 500;
  transition: color .12s, background .12s;
}
.nav-links a svg       { width: 14px; height: 14px; flex-shrink: 0; }
.nav-links a:hover     { color: var(--txt-1); background: var(--overlay); }
.nav-links a.active    { color: var(--blue); background: var(--blue-a); }

/* ── Page header ──────────────────────────────────── */
.pg-head { margin-bottom: 1.75rem; }
.pg-head h1 {
  font-size: 1.5rem; font-weight: 700;
  letter-spacing: -.03em; line-height: 1.2;
  color: var(--txt-1);
}
.pg-head p { color: var(--txt-2); margin-top: .35rem; font-size: .9375rem; }

/* ── Cards ────────────────────────────────────────── */
.card {
  background: var(--surface);
  border: 1px solid var(--b-lo);
  border-radius: var(--r4);
  box-shadow: var(--sh);
  overflow: hidden;
}
.card + .card { margin-top: 1rem; }

.card-head {
  padding: 1rem 1.5rem;
  border-bottom: 1px solid var(--b-lo);
  display: flex; align-items: center; gap: .75rem;
}
.card-ico {
  width: 36px; height: 36px; flex-shrink: 0;
  border-radius: var(--r2);
  background: var(--blue-a);
  display: flex; align-items: center; justify-content: center;
}
.card-ico svg          { width: 18px; height: 18px; color: var(--blue); }
.card-titles           { flex: 1; min-width: 0; }
.card-titles h2        { font-size: .9375rem; font-weight: 600; line-height: 1.3; color: var(--txt-1); }
.card-titles p         { font-size: .8125rem; color: var(--txt-3); margin-top: 2px; }
.card-body             { padding: 1.5rem; }

/* ── Stats ────────────────────────────────────────── */
.stats-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
  gap: 1rem; margin-bottom: 1.5rem;
}
.stat {
  background: var(--surface); border: 1px solid var(--b-lo);
  border-radius: var(--r4); padding: 1.25rem 1.5rem;
  box-shadow: var(--sh);
  transition: border-color .2s, box-shadow .2s;
}
.stat:hover { border-color: var(--b-md); box-shadow: 0 4px 12px rgba(0,0,0,.08); }
.stat-row1 {
  display: flex; align-items: center;
  justify-content: space-between; margin-bottom: .6rem;
}
.stat-label {
  font-size: .7rem; font-weight: 600;
  text-transform: uppercase; letter-spacing: .06em; color: var(--txt-3);
}
.stat-ico {
  width: 28px; height: 28px; border-radius: var(--r2);
  background: var(--blue-a);
  display: flex; align-items: center; justify-content: center;
}
.stat-ico svg  { width: 14px; height: 14px; color: var(--blue); }
.stat-val      { font-size: 1.875rem; font-weight: 700; letter-spacing: -.05em; line-height: 1; color: var(--txt-1); }
.stat-note     { font-size: .73rem; color: var(--txt-3); margin-top: .3rem; }

/* ── Quick-access grid ───────────────────────────── */
.quick-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 1rem;
}
.quick-card {
  display: flex; flex-direction: column; gap: .5rem;
  background: var(--overlay); border: 1px solid var(--b-lo);
  border-radius: var(--r3); padding: 1.25rem;
  text-decoration: none; color: var(--txt-1);
  transition: border-color .15s, transform .15s, box-shadow .15s;
}
.quick-card:hover { border-color: var(--b-md); box-shadow: 0 4px 12px rgba(0,0,0,.08); transform: translateY(-2px); }
.quick-card-ico {
  width: 38px; height: 38px; border-radius: var(--r2);
  background: var(--blue-a);
  display: flex; align-items: center; justify-content: center;
}
.quick-card-ico svg    { width: 18px; height: 18px; color: var(--blue); }
.quick-card-title      { font-size: .9rem; font-weight: 600; color: var(--txt-1); }
.quick-card-desc       { font-size: .78rem; color: var(--txt-3); line-height: 1.4; }

/* ── Forms ────────────────────────────────────────── */
.f-grid   { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: .875rem; }
.f-col    { display: flex; flex-direction: column; gap: .35rem; }
.f-label  { font-size: .8125rem; font-weight: 500; color: var(--txt-2); }
.f-hint   { font-size: .73rem; color: var(--txt-3); margin-top: .1rem; }
.f-full   { grid-column: 1 / -1; }

input, textarea, select {
  width: 100%; padding: .575rem .8rem;
  background: var(--surface); color: var(--txt-1);
  border: 1px solid var(--b-md); border-radius: var(--r2);
  font-size: .875rem; font-family: inherit;
  transition: border-color .12s, box-shadow .12s; outline: none;
}
input::placeholder, textarea::placeholder { color: var(--txt-3); }
input:focus, textarea:focus, select:focus {
  border-color: var(--blue);
  box-shadow: 0 0 0 3px rgba(37, 99, 235, .12);
}
textarea          { min-height: 88px; resize: vertical; line-height: 1.5; }
input[type="file"] { padding: .35rem .65rem; cursor: pointer; }

/* ── Buttons ──────────────────────────────────────── */
.btn {
  display: inline-flex; align-items: center; gap: .45rem;
  padding: .525rem 1rem; border-radius: var(--r2); border: none;
  font-size: .875rem; font-weight: 600; font-family: inherit;
  cursor: pointer; text-decoration: none; line-height: 1;
  transition: all .13s; white-space: nowrap;
}
.btn:active               { transform: scale(.97); }
.btn svg                  { width: 14px; height: 14px; flex-shrink: 0; }
.btn-p                    { background: var(--blue); color: #fff; }
.btn-p:hover              { background: var(--blue-dk); box-shadow: 0 4px 14px rgba(37,99,235,.25); }
.btn-g                    { background: var(--surface); color: var(--txt-2); border: 1px solid var(--b-md); }
.btn-g:hover              { background: var(--overlay); color: var(--txt-1); }
.btn-danger               { background: var(--red); color: #fff; }
.btn-danger:hover         { background: #b91c1c; }
.btn-sm                   { padding: .325rem .7rem; font-size: .8125rem; }
.btn-row                  { display: flex; flex-wrap: wrap; gap: .625rem; margin-top: 1.125rem; align-items: center; }
.btn.loading, .btn[disabled] { opacity: .5; cursor: not-allowed; pointer-events: none; }

/* ── Feedback / Status ────────────────────────────── */
.feedback      { margin-top: .75rem; font-size: .8125rem; min-height: 1.2em; }
.feedback.ok   { color: var(--green); }
.feedback.err  { color: var(--red); }
.feedback.info { color: var(--txt-2); }

.status       { margin-top: .75rem; font-size: .8125rem; min-height: 1.2em; color: var(--txt-2); }
.status.error { color: var(--red); }

pre.out {
  background: var(--overlay); border: 1px solid var(--b-lo);
  border-radius: var(--r2); padding: .8rem .9rem;
  overflow: auto; font-size: .8125rem; color: var(--txt-2);
  line-height: 1.6; margin-top: .75rem; max-height: 260px;
  font-family: 'JetBrains Mono', 'Fira Code', ui-monospace, monospace;
}

/* ── Badge ────────────────────────────────────────── */
.badge {
  display: inline-flex; align-items: center;
  padding: 2px 8px; border-radius: 999px;
  font-size: .6875rem; font-weight: 600; letter-spacing: .025em;
}
.badge-b { background: #dbeafe; color: #1d4ed8; }
.badge-g { background: #dcfce7; color: #16a34a; }
.badge-a { background: #fef9c3; color: #b45309; }
.badge-n { background: var(--overlay); color: var(--txt-2); border: 1px solid var(--b-md); }

/* ── Search results ───────────────────────────────── */
#results { margin-top: 1.125rem; display: flex; flex-direction: column; gap: .75rem; }

.rc {
  background: var(--surface); border: 1px solid var(--b-lo);
  border-radius: var(--r4); padding: 1rem 1.25rem;
  transition: border-color .15s, box-shadow .15s;
}
.rc:hover         { border-color: var(--b-md); box-shadow: 0 2px 8px rgba(0,0,0,.06); }
.rc-name          { font-weight: 600; font-size: .9375rem; color: var(--txt-1); }
.rc-badges        { display: flex; flex-wrap: wrap; gap: .4rem; margin: .45rem 0 .35rem; align-items: center; }
.rc-path          { font-size: .71rem; color: var(--txt-3); font-family: monospace; word-break: break-all; }
.rc-snippet       { font-size: .875rem; color: var(--txt-2); line-height: 1.65; margin-top: .5rem; }
.rc-snippet mark  { background: #fef9c3; color: #92400e; border-radius: 2px; padding: 0 2px; font-style: normal; }
.rc-foot {
  display: flex; flex-wrap: wrap; gap: .4rem; align-items: center;
  margin-top: .75rem; padding-top: .75rem; border-top: 1px solid var(--b-lo);
}
.rc-foot input { flex: 1; min-width: 140px; max-width: 260px; }

/* ── Empty state ──────────────────────────────────── */
.empty     { text-align: center; padding: 3.5rem 1.5rem; color: var(--txt-3); }
.empty svg { width: 38px; height: 38px; margin: 0 auto .75rem; opacity: .3; display: block; }
.empty p   { font-size: .9rem; }

/* ── Section divider ──────────────────────────────── */
.sep { border: none; border-top: 1px solid var(--b-lo); margin: 1.5rem 0; }

/* ── Auth gate ────────────────────────────────────── */
.auth-wrap { max-width: 390px; margin: 0 auto; }

/* ── Utility ──────────────────────────────────────── */
.hidden  { display: none !important; }
.muted   { color: var(--txt-3); }
.gap-top { margin-top: 1.375rem; }

/* ── Responsive ───────────────────────────────────── */
@media (max-width: 640px) {
  .nav-links a .nav-lbl { display: none; }
  .nav-signout .nav-lbl { display: none; }
  .stat-val              { font-size: 1.5rem; }
  .pg-head h1            { font-size: 1.25rem; }
  .quick-grid            { grid-template-columns: 1fr 1fr; }
  .card-body             { padding: 1rem; }
}

/* ── Nav sign-out ─────────────────────────────────── */
.nav-sep {
  width: 1px; height: 20px;
  background: var(--b-lo); margin: 0 .2rem; flex-shrink: 0;
}
.nav-signout {
  display: flex; align-items: center; gap: .375rem;
  color: var(--txt-3); background: none; border: none;
  font-size: .8125rem; font-weight: 500; font-family: inherit;
  padding: .325rem .65rem; border-radius: var(--r2);
  cursor: pointer; transition: color .12s, background .12s;
}
.nav-signout svg { width: 14px; height: 14px; flex-shrink: 0; }
.nav-signout:hover { color: var(--red); background: #fee2e2; }

/* ── Toast notifications ──────────────────────────── */
.toast-wrap {
  position: fixed; bottom: 1.5rem; right: 1.5rem;
  display: flex; flex-direction: column; gap: .45rem;
  z-index: 9999; pointer-events: none;
}
.toast {
  background: var(--surface); border: 1px solid var(--b-lo);
  border-radius: var(--r3); padding: .6rem 1rem;
  font-size: .8375rem; color: var(--txt-1);
  box-shadow: 0 8px 24px rgba(0, 0, 0, .12);
  display: flex; align-items: center; gap: .55rem;
  pointer-events: all; max-width: 320px;
  animation: toast-in .2s ease;
}
.toast.ok   { border-color: #bbf7d0; }
.toast.err  { border-color: #fecaca; }
.toast-dot  { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
.toast.ok  .toast-dot  { background: var(--green); }
.toast.err .toast-dot  { background: var(--red); }
.toast.info .toast-dot { background: var(--blue); }
@keyframes toast-in {
  from { opacity: 0; transform: translateY(8px) scale(.96); }
  to   { opacity: 1; transform: translateY(0) scale(1); }
}

/* ── Drag & drop zone ─────────────────────────────── */
.drop-zone {
  border: 2px dashed var(--b-md); border-radius: var(--r3);
  padding: 2rem 1.5rem; text-align: center;
  cursor: pointer; transition: border-color .15s, background .15s;
  background: var(--overlay);
}
.drop-zone:hover, .drop-zone.over {
  border-color: var(--blue); background: var(--blue-a);
}
.drop-zone-ico {
  width: 44px; height: 44px; margin: 0 auto .75rem;
  background: var(--blue-a);
  border-radius: var(--r3);
  display: flex; align-items: center; justify-content: center;
}
.drop-zone-ico svg   { width: 20px; height: 20px; color: var(--blue); }
.drop-zone-title     { font-weight: 600; font-size: .9rem; color: var(--txt-1); margin-bottom: .2rem; }
.drop-zone-sub       { font-size: .78rem; color: var(--txt-3); }
.drop-file-name      { font-size: .82rem; color: var(--green); margin-top: .55rem; font-weight: 500; }

/* ── Progress bar ─────────────────────────────────── */
.progress-wrap   { margin-top: .875rem; }
.progress-bar    { height: 6px; border-radius: 999px; background: var(--b-lo); overflow: hidden; }
.progress-fill   { height: 100%; background: var(--blue); border-radius: 999px; transition: width .45s ease; width: 0%; }
.progress-status { font-size: .79rem; color: var(--txt-2); margin-top: .4rem; font-family: 'JetBrains Mono', monospace; }

/* ── Filters toggle ───────────────────────────────── */
.filter-toggle-btn {
  display: inline-flex; align-items: center; gap: .35rem;
  font-size: .8rem; color: var(--txt-2); cursor: pointer;
  padding: 0; background: none; border: none;
  font-family: inherit; font-weight: 500;
  transition: color .12s;
}
.filter-toggle-btn svg { width: 13px; height: 13px; transition: transform .2s; flex-shrink: 0; }
.filter-toggle-btn.open svg { transform: rotate(180deg); }
.filter-toggle-btn:hover { color: var(--txt-1); }

/* ── Search bar row ───────────────────────────────── */
.search-row {
  display: flex; gap: .5rem; align-items: center;
}
.search-row input { flex: 1; }
.kbd {
  display: inline-flex; align-items: center; justify-content: center;
  background: var(--overlay); border: 1px solid var(--b-md);
  border-radius: var(--r1); padding: 2px 6px;
  font-size: .7rem; color: var(--txt-3); font-family: monospace;
  flex-shrink: 0; white-space: nowrap;
}

/* ── Results meta bar ─────────────────────────────── */
.results-meta {
  font-size: .79rem; color: var(--txt-3);
  padding: .3rem .1rem; min-height: 1.4em;
}

/* ── Recent searches list ─────────────────────────── */
.recent-list { display: flex; flex-direction: column; gap: .25rem; }
.recent-item {
  display: flex; align-items: center; gap: .5rem;
  padding: .45rem .65rem; border-radius: var(--r2);
  text-decoration: none; color: var(--txt-2);
  font-size: .85rem;
  transition: background .12s, color .12s;
}
.recent-item svg { flex-shrink: 0; opacity: .5; }
.recent-item:hover { background: var(--overlay); color: var(--txt-1); }

/* ── Config tabs — pill style ─────────────────────── */
.tab-bar {
  display: flex; gap: .25rem; flex-wrap: wrap;
  margin-bottom: 1.5rem;
  background: var(--overlay);
  border-radius: var(--r3);
  padding: 4px;
  width: fit-content; max-width: 100%;
  border: 1px solid var(--b-lo);
}
.tab {
  display: inline-flex; align-items: center; gap: .4rem;
  padding: .45rem .9rem; border-radius: var(--r2);
  border: none;
  font-size: .8125rem; font-weight: 500; font-family: inherit;
  color: var(--txt-2); background: none;
  cursor: pointer; transition: color .12s, background .12s, box-shadow .12s;
}
.tab svg { width: 13px; height: 13px; flex-shrink: 0; }
.tab:hover  { color: var(--txt-1); background: rgba(255, 255, 255, .7); }
.tab.active {
  color: var(--blue);
  background: var(--surface);
  box-shadow: 0 1px 4px rgba(0, 0, 0, .1);
}

.tab-panel { margin-top: 0; }

/* ── User / path table ────────────────────────────── */
.u-table-wrap { overflow-x: auto; }
.u-table {
  width: 100%; border-collapse: collapse;
  font-size: .85rem;
}
.u-table th {
  text-align: left; padding: .5rem .75rem;
  font-size: .72rem; font-weight: 600;
  text-transform: uppercase; letter-spacing: .05em;
  color: var(--txt-3); border-bottom: 1px solid var(--b-lo);
  background: var(--overlay);
}
.u-table td {
  padding: .625rem .75rem;
  border-bottom: 1px solid var(--b-lo);
  vertical-align: middle; color: var(--txt-2);
}
.u-table tr:last-child td { border-bottom: none; }
.u-table tr:hover td { background: var(--overlay); }

.u-role-select {
  width: auto; padding: .2rem .5rem;
  font-size: .8rem; border-radius: var(--r1);
}

/* ── Path test / DB test grid ─────────────────────── */
.path-test-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: .5rem;
}
.path-test-item {
  display: flex; flex-direction: column; gap: .25rem;
  background: var(--overlay); border: 1px solid var(--b-lo);
  border-radius: var(--r2); padding: .6rem .8rem;
}
.path-test-lbl {
  font-size: .7rem; font-weight: 600;
  text-transform: uppercase; letter-spacing: .05em; color: var(--txt-3);
}

/* ── SSL status grid ──────────────────────────────── */
.ssl-status-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: .5rem;
}

/* ── Path list ────────────────────────────────────── */
.path-list { margin-bottom: .5rem; }

.gap-top { margin-top: 1.25rem; }

code {
  background: var(--overlay); border: 1px solid var(--b-lo);
  border-radius: 3px; padding: 1px 5px;
  font-size: .85em; font-family: 'JetBrains Mono', ui-monospace, monospace;
  color: var(--txt-1);
}

/* ── AI suggestion grid ───────────────────────────── */
.ai-suggestion-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
  gap: .5rem;
}

/* ── Reorganize table checkbox ────────────────────── */
.reorg-check { width: 15px; height: 15px; cursor: pointer; accent-color: var(--blue); }

/* ── Tag cloud chips ──────────────────────────────── */
.tag-chip {
  display: inline-flex; align-items: center; gap: .3rem;
  padding: .25rem .6rem;
  border-radius: 99px;
  border: 1px solid var(--b-lo);
  background: var(--surface);
  font-size: .78rem; font-weight: 500; font-family: inherit;
  color: var(--txt-2); cursor: pointer;
  transition: color .12s, background .12s, border-color .12s;
}
.tag-chip:hover { color: var(--blue); border-color: #bfdbfe; background: var(--blue-a); }
.tag-chip-count {
  display: inline-block;
  padding: 0 .35rem;
  border-radius: 99px;
  background: var(--overlay);
  font-size: .68rem; color: var(--txt-3);
}

/* ── Structure suggestion tree ────────────────────── */
.structure-tree { display: flex; flex-direction: column; gap: .5rem; }
.structure-folder {
  padding: .6rem .875rem;
  border: 1px solid var(--b-lo);
  border-radius: var(--r2);
  background: var(--overlay);
}
.structure-folder-name {
  display: flex; align-items: center; gap: .4rem;
  margin-bottom: .25rem;
}
.structure-folder-desc {
  font-size: .82rem; color: var(--txt-2);
  margin: 0 0 .3rem 1.25rem;
}
.structure-folder-examples {
  display: flex; gap: .3rem; flex-wrap: wrap;
  margin-left: 1.25rem;
}

/* ── AI recommendation banner ─────────────────────── */
.ai-rec-banner {
  display: flex; align-items: flex-start; gap: .75rem;
  padding: .75rem 1rem;
  border-radius: var(--r2);
  border: 1px solid var(--b-lo);
  margin-top: .75rem;
  font-size: .85rem;
  line-height: 1.45;
}
.ai-rec-banner.tier-tiny    { background: #fee2e2; border-color: #fecaca; color: #dc2626; }
.ai-rec-banner.tier-small   { background: #ffedd5; border-color: #fed7aa; color: #c2410c; }
.ai-rec-banner.tier-medium  { background: #fef9c3; border-color: #fde68a; color: #b45309; }
.ai-rec-banner.tier-large   { background: #dcfce7; border-color: #bbf7d0; color: #16a34a; }
.ai-rec-banner.tier-xlarge  { background: #dbeafe; border-color: #bfdbfe; color: #2563eb; }
.ai-rec-banner strong { display: block; font-weight: 600; margin-bottom: .2rem; }

/* ── AI model library fit badges ─────────────────── */
.badge-fit-ok       { background: #dcfce7; color: #16a34a; border: 1px solid #bbf7d0; }
.badge-fit-warn     { background: #fef9c3; color: #b45309; border: 1px solid #fde68a; }
.badge-fit-toolarge { background: #fee2e2; color: #dc2626; border: 1px solid #fecaca; }
.badge-fit-ok, .badge-fit-warn, .badge-fit-toolarge {
  display: inline-block; padding: 1px 7px;
  border-radius: 99px; font-size: .72rem; font-weight: 600; white-space: nowrap;
}

/* ── AI pull progress bar ─────────────────────────── */
.pull-progress-wrap {
  height: 6px; border-radius: 3px;
  background: var(--b-lo);
  margin-top: .5rem; overflow: hidden;
}
.pull-progress-bar {
  height: 100%; border-radius: 3px;
  background: var(--blue); width: 0%;
  transition: width .3s ease;
}
```

- [ ] **Step 2: Run backend tests to confirm nothing broke**

```
pytest -q
```

Expected: all tests pass (CSS-only change, no Python touched).

- [ ] **Step 3: Commit**

```
git add document_search/web/static/styles.css
git commit -m "feat: rewrite styles.css as light design system"
```

---

## Task 3: Update app.js — graceful 401 handling

When an API call returns 401 (session expired or not logged in), the current code throws an error whose message contains the raw JSON string `{"detail":"unauthorized"}`. This leaks into feedback elements. The fix: detect 401 in `api()`, show a clean toast, and redirect to the auth gate if on the config page.

**Files:**
- Modify: `document_search/web/static/app.js:1-12`

- [ ] **Step 1: Replace the `api()` function**

Replace lines 1–12 of `app.js` (the `api` function) with:

```js
let token = localStorage.getItem('documentSearchToken');

async function api(path, method = 'GET', body = null) {
  const headers = { 'X-Auth-Token': token ?? '' };
  if (body !== null) headers['Content-Type'] = 'application/json';
  const res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : null });
  if (res.status === 401) {
    showToast('Session expired — please sign in again', 'err');
    // Re-show auth gate on the config page
    const configPanel = document.getElementById('configPanel');
    const authGate = document.getElementById('authGate');
    if (configPanel) configPanel.classList.add('hidden');
    if (authGate) authGate.classList.remove('hidden');
    throw new Error('Unauthorized');
  }
  if (!res.ok) {
    const text = await res.text();
    let msg = text;
    try { msg = JSON.parse(text)?.detail ?? text; } catch (_) {}
    throw new Error(msg || `Request failed (${res.status})`);
  }
  return await res.json();
}
```

The second `if (!res.ok)` block also now parses FastAPI's `{"detail":"..."}` envelope and surfaces only the human-readable `detail` string instead of the raw JSON.

- [ ] **Step 2: Run backend tests**

```
pytest -q
```

Expected: all tests pass.

- [ ] **Step 3: Commit**

```
git add document_search/web/static/app.js
git commit -m "fix: surface clean error messages from 401/error API responses"
```

---

## Task 4: Add Swagger iframe to wiki.html + fix inline CSS variables

The wiki's inline `<style>` block uses undefined variables (`--bg-2`, `--border`, `--accent`) and needs updating to match the new design system variables. Also add the Swagger iframe card.

**Files:**
- Modify: `document_search/web/templates/wiki.html:9-33` (inline style block)
- Modify: `document_search/web/templates/wiki.html:92-95` (insert Swagger card before wiki-content)

- [ ] **Step 1: Replace the wiki `<style>` block**

Replace the entire `<style>...</style>` block in `wiki.html` (lines 9–34) with:

```html
  <style>
    .wiki-layout { display:grid; grid-template-columns:220px 1fr; gap:1.5rem; align-items:start; }
    @media(max-width:700px){ .wiki-layout{ grid-template-columns:1fr; } .wiki-toc{ display:none; } }
    .wiki-toc { position:sticky; top:1rem; background:var(--surface); border:1px solid var(--b-lo); border-radius:10px; padding:1rem; font-size:.83rem; box-shadow:var(--sh); }
    .wiki-toc h3 { font-size:.72rem; text-transform:uppercase; letter-spacing:.06em; color:var(--txt-3); margin:0 0 .6rem; font-weight:600; }
    .wiki-toc a { display:block; color:var(--txt-2); text-decoration:none; padding:.25rem 0; border-left:2px solid transparent; padding-left:.5rem; font-size:.82rem; }
    .wiki-toc a:hover, .wiki-toc a.active { color:var(--blue); border-left-color:var(--blue); }
    .wiki-toc .toc-sub { padding-left:1.2rem; }
    .wiki-toc hr { border:none; border-top:1px solid var(--b-lo); margin:.6rem 0; }
    .wiki-content h2 { font-size:1.1rem; font-weight:700; margin:2rem 0 .6rem; padding-bottom:.35rem; border-bottom:1px solid var(--b-lo); color:var(--txt-1); }
    .wiki-content h3 { font-size:.93rem; font-weight:600; margin:1.2rem 0 .4rem; color:var(--txt-1); }
    .wiki-content p { font-size:.88rem; color:var(--txt-2); line-height:1.65; margin:.4rem 0; }
    .wiki-content ul, .wiki-content ol { font-size:.88rem; color:var(--txt-2); line-height:1.7; padding-left:1.4rem; margin:.4rem 0; }
    .wiki-content code { background:var(--overlay); border:1px solid var(--b-lo); border-radius:3px; padding:.1em .35em; font-size:.82rem; color:var(--txt-1); }
    .wiki-content pre { background:var(--overlay); border:1px solid var(--b-lo); border-radius:8px; padding:.75rem 1rem; overflow-x:auto; font-size:.8rem; line-height:1.5; margin:.6rem 0; }
    .wiki-content pre code { background:none; border:none; padding:0; color:var(--txt-2); }
    .wiki-content table { width:100%; border-collapse:collapse; font-size:.83rem; margin:.6rem 0; }
    .wiki-content th { background:var(--overlay); color:var(--txt-2); font-weight:600; padding:.4rem .6rem; text-align:left; border-bottom:1px solid var(--b-lo); font-size:.78rem; text-transform:uppercase; letter-spacing:.04em; }
    .wiki-content td { padding:.4rem .6rem; border-bottom:1px solid var(--b-lo); color:var(--txt-2); vertical-align:top; }
    .wiki-content .callout { background:var(--blue-a); border-left:3px solid var(--blue); border-radius:0 8px 8px 0; padding:.6rem .9rem; margin:.6rem 0; font-size:.85rem; color:var(--txt-2); }
    .wiki-content .callout.warn { background:#fef9c3; border-left-color:var(--amber); }
    .wiki-section { scroll-margin-top:1rem; }
    .wiki-badge { display:inline-block; background:var(--blue); color:#fff; font-size:.7rem; font-weight:700; border-radius:3px; padding:.1em .4em; vertical-align:middle; margin-left:.3rem; }
    .wiki-badge.get  { background:#16a34a; }
    .wiki-badge.post { background:#2563eb; }
    .wiki-badge.del  { background:#dc2626; }
    .swagger-frame-wrap { border-radius:10px; overflow:hidden; border:1px solid var(--b-lo); margin-top:.75rem; }
    .swagger-frame-wrap iframe { display:block; width:100%; height:700px; border:none; }
  </style>
```

- [ ] **Step 2: Add the Swagger iframe card above the wiki content**

Locate this comment in `wiki.html`:

```html
        <!-- ── Wiki content ── -->
        <div class="wiki-content">
```

Insert the following **before** that comment (inside the `wiki-layout` div, as a sibling of `.wiki-toc`):

Actually, the layout is a 2-column grid (TOC | content). The Swagger card should span the full width **above** the grid. Insert it **before** the `<div class="wiki-layout">` opening tag:

```html
      <!-- ── Swagger iframe ── -->
      <div class="card" style="margin-bottom:1.5rem;">
        <div class="card-head">
          <div class="card-ico">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
          </div>
          <div class="card-titles">
            <h2>Interactive API Reference</h2>
            <p>Try endpoints directly — authentication is handled via the <code>X-Auth-Token</code> header for UI routes, or <code>X-Api-Key</code> for Home Assistant routes.</p>
          </div>
          <a href="/docs" target="_blank" rel="noopener" class="btn btn-g btn-sm" style="flex-shrink:0;">Open in new tab ↗</a>
        </div>
        <div class="card-body" style="padding:0;">
          <div class="swagger-frame-wrap">
            <iframe src="/docs" title="Swagger UI — Seekr API" loading="lazy"></iframe>
          </div>
        </div>
      </div>
```

- [ ] **Step 3: Run backend tests**

```
pytest -q
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```
git add document_search/web/templates/wiki.html
git commit -m "feat: add Swagger iframe card and fix wiki inline CSS variables"
```

---

## Task 5: Visual verification across all pages

No code changes — this task confirms every page renders correctly in the browser.

- [ ] **Step 1: Start the dev server**

```
PYTHONPATH=. uvicorn document_search.app:app --reload --port 8000
```

Or use whichever start command is configured in the project.

- [ ] **Step 2: Check each page**

| Page | URL | What to verify |
|---|---|---|
| Dashboard | `http://localhost:8000/` | White topbar, light cards, stat numbers, sign-in form centred |
| Dashboard (signed in) | — | Stats populated, quick-access cards, recent searches |
| Search | `http://localhost:8000/search` | Light filter card, result cards with white bg, tag chips blue-tinted |
| Ingest | `http://localhost:8000/ingest` | Dashed dropzone, progress bar visible, light card layout |
| Config | `http://localhost:8000/config` | Pill tab bar on light track, tabs switch correctly |
| Config (session expired) | Sign in, wait or clear token, try any tab action | Toast "Session expired", auth gate re-appears, no raw JSON in DOM |
| Wiki | `http://localhost:8000/wiki` | No 500 — page loads, Swagger iframe visible above TOC, HA YAML renders as text |
| Swagger | `http://localhost:8000/docs` | Unaffected (CSP exception already in place) |

- [ ] **Step 3: Run the full test suite one final time**

```
pytest -q
```

Expected: all tests pass.

- [ ] **Step 4: Final commit**

```
git add -A
git commit -m "feat: complete light UI redesign across all Seekr pages"
```

---

## Self-review notes

**Spec coverage check:**
- Wiki 500 fix → Task 1 ✓
- styles.css full rewrite → Task 2 ✓
- 401 handling + clean error messages → Task 3 ✓
- Swagger iframe in wiki → Task 4 ✓
- Pill tab bar → Task 2 (CSS-only `.tab-bar` / `.tab`) ✓
- Light badges (badges, fit badges, rec banners) → Task 2 ✓
- All 5 pages covered by CSS rewrite → Task 2 ✓
- Wiki inline style variable fix → Task 4 ✓
- Visual verification → Task 5 ✓

**Type consistency:** No shared function signatures changed. `api()` signature unchanged; callers unaffected. CSS class names unchanged; JS selectors stay valid.

**Placeholder scan:** No TBDs, no "similar to above" steps, all CSS is complete in Task 2.
