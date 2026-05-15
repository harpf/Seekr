# Search Fixes + Smart Autocomplete + Grouped Results Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix four search bugs, add three backend endpoints, introduce a reusable ChipInput component, upgrade tag filtering to multi-tag AND logic, and redesign search results to show grouped hits per document with full highlighting.

**Architecture:** Backend fixes live in `search_service.py` (query building, browse-all fallback) and `app.py` (request model, endpoint logic, new routes). Frontend changes add a `ChipInput` class at the top of `app.js`, new CSS in `styles.css`, and HTML updates to `search.html` and `ingest.html`. No new dependencies.

**Tech Stack:** Python 3.11, FastAPI, SQLite FTS5, vanilla JavaScript (ES2020), CSS custom properties

---

## File Map

| File | Change |
|---|---|
| `document_search/index/search_service.py` | Fix `build_match_query` (None for empty/wildcard, dot-strip, comma→OR), add `_browse_all`, update `search()` with tags list |
| `document_search/app.py` | Fix `SearchRequest` (query optional, tag→tags), rewrite `api_search` (grouped response, error handling), add path guard in `api_index_start`, add 3 new GET endpoints |
| `document_search/web/static/app.js` | Fix `api()` error parsing, add `ChipInput` class, wire filters, redesign result rendering, update `saveTags`/`filterByTag`/`startIndex`/`uploadDocument` |
| `document_search/web/static/styles.css` | Add `.chip-wrap`, `.chip`, `.chip-x`, `.chip-input`, `.rc-hits`, `.rc-hit`, `.rc-hit-label`, `.rc-more-btn` |
| `document_search/web/templates/search.html` | Replace filetype and tag filter inputs with chip-wrap markup |
| `document_search/web/templates/ingest.html` | Chip-wrap for upload tags, datalist on folder input, replace paths input with `<select multiple>` |
| `tests/test_search_service.py` | Update existing dot test, add comma-split and empty-query tests |
| `tests/test_app_search.py` | New: path-block tests, new endpoint smoke tests, grouped response shape test |

---

## Task 1: Fix `[object Object]` Error Display

**Files:**
- Modify: `document_search/web/static/app.js` (line 23, inside `api()`)

- [ ] **Step 1: Update the error parsing in `api()`**

Find line 23 in `app.js`:
```js
try { msg = String(JSON.parse(text)?.detail ?? text); } catch (_) {}
```

Replace with:
```js
try {
  const detail = JSON.parse(text)?.detail;
  if (Array.isArray(detail)) {
    msg = detail.map(e => e.msg || String(e)).join('; ');
  } else {
    msg = String(detail ?? text);
  }
} catch (_) {}
```

- [ ] **Step 2: Verify manually**

Start the app. On the search page, submit with `modifiedFrom` set to `not-a-date`. The toast should show a readable field-validation message instead of `[object Object]`.

- [ ] **Step 3: Commit**

```bash
git add document_search/web/static/app.js
git commit -m "fix: parse FastAPI 422 detail arrays into readable error messages"
```

---

## Task 2: Fix Wildcard `*` → 500 and Add Browse-All Mode

**Files:**
- Modify: `document_search/index/search_service.py` (full rewrite — also covers Tasks 3 and 6)
- Modify: `document_search/app.py`
- Modify: `tests/test_search_service.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_search_service.py`:
```python
def test_build_match_query_empty_returns_none():
    result = build_match_query("", filetype=None, path_filter=None, block_type=None)
    assert result is None

def test_build_match_query_wildcard_returns_none():
    result = build_match_query("*", filetype=None, path_filter=None, block_type=None)
    assert result is None

def test_build_match_query_whitespace_only_returns_none():
    result = build_match_query("   ", filetype=None, path_filter=None, block_type=None)
    assert result is None
```

Run: `pytest -q tests/test_search_service.py::test_build_match_query_empty_returns_none -v`
Expected: FAIL (currently returns `""` not `None`)

- [ ] **Step 2: Rewrite `search_service.py`**

Replace `document_search/index/search_service.py` entirely:
```python
from __future__ import annotations

from document_search.index.sqlite_store import SqliteStore


def build_match_query(
    query: str,
    filetype: str | None = None,
    path_filter: str | None = None,
    block_type: str | None = None,
) -> str | None:
    q = (query or "").strip()
    if not q or q == "*":
        return None
    clauses = [q]
    if filetype:
        extensions = [e.strip().lstrip(".") for e in filetype.split(",") if e.strip()]
        if len(extensions) == 1:
            clauses.append(f"extension:{extensions[0]}")
        elif len(extensions) > 1:
            or_parts = " OR ".join(f"extension:{e}" for e in extensions)
            clauses.append(f"({or_parts})")
    if path_filter:
        clauses.append(f"path:{path_filter}*")
    if block_type:
        clauses.append(f"block_type:{block_type}")
    return " AND ".join(clauses)


def _browse_all(
    store: SqliteStore,
    filetype: str | None,
    path_filter: str | None,
    block_type: str | None,
    modified_from: str | None,
    modified_to: str | None,
    tags: list[str],
    user_id: int | None,
    limit: int,
):
    where = ["1=1"]
    params: list = []

    if filetype:
        exts = ["." + e.strip().lstrip(".") for e in filetype.split(",") if e.strip()]
        placeholders = ",".join("?" * len(exts))
        where.append(f"d.extension IN ({placeholders})")
        params.extend(exts)
    if path_filter:
        where.append("d.path LIKE ?")
        params.append(path_filter + "%")
    if block_type:
        where.append("cb.block_type = ?")
        params.append(block_type)
    if modified_from:
        where.append("d.modified_at >= ?")
        params.append(modified_from)
    if modified_to:
        where.append("d.modified_at <= ?")
        params.append(modified_to)
    if tags and user_id is not None:
        tag_ph = ",".join("?" * len(tags))
        where.append(
            f"""d.id IN (
                SELECT dt.document_id FROM document_tags dt
                JOIN user_tags ut ON ut.id = dt.tag_id
                WHERE dt.user_id = ? AND ut.name IN ({tag_ph})
                GROUP BY dt.document_id
                HAVING COUNT(DISTINCT ut.name) = ?
            )"""
        )
        params.extend([user_id] + tags + [len(tags)])

    sql = f"""
        SELECT NULL AS rank,
               d.id AS document_id, d.filename, d.path, d.extension,
               d.modified_at, d.indexed_at,
               cb.block_type, cb.block_number,
               NULL AS snippet
        FROM documents d
        JOIN content_blocks cb ON cb.document_id = d.id
        WHERE {" AND ".join(where)}
        ORDER BY d.modified_at DESC
        LIMIT ?
    """
    params.append(limit)
    return store.conn.execute(sql, tuple(params)).fetchall()


def search(
    store: SqliteStore,
    query: str,
    limit: int = 20,
    filetype: str | None = None,
    path_filter: str | None = None,
    block_type: str | None = None,
    modified_from: str | None = None,
    modified_to: str | None = None,
    tags: list[str] | None = None,
    user_id: int | None = None,
):
    tags = [t.lower().strip() for t in (tags or [])]
    match_query = build_match_query(query, filetype, path_filter, block_type)

    if match_query is None:
        return _browse_all(
            store, filetype, path_filter, block_type,
            modified_from, modified_to, tags, user_id, limit,
        )

    sql = """
        SELECT c.rank, d.id as document_id, d.filename, d.path, d.extension,
               d.modified_at, d.indexed_at,
               b.block_type, b.block_number,
               snippet(content_fts, 7, '[', ']', ' … ', 12) AS snippet
        FROM content_fts c
        JOIN documents d ON d.id = c.document_id
        JOIN content_blocks b ON b.id = c.block_id
        WHERE content_fts MATCH ?
    """
    params: list = [match_query]
    if modified_from:
        sql += " AND d.modified_at >= ?"
        params.append(modified_from)
    if modified_to:
        sql += " AND d.modified_at <= ?"
        params.append(modified_to)
    if tags and user_id is not None:
        tag_ph = ",".join("?" * len(tags))
        sql += f"""
            AND d.id IN (
                SELECT dt.document_id FROM document_tags dt
                JOIN user_tags ut ON ut.id = dt.tag_id
                WHERE dt.user_id = ? AND ut.name IN ({tag_ph})
                GROUP BY dt.document_id
                HAVING COUNT(DISTINCT ut.name) = ?
            )"""
        params.extend([user_id] + tags + [len(tags)])
    sql += " ORDER BY c.rank LIMIT ?"
    params.append(limit)
    return store.conn.execute(sql, tuple(params)).fetchall()
```

This single rewrite also covers Tasks 3 (extension comma-split/dot-strip) and 6 (multi-tag AND).

- [ ] **Step 3: Update existing test for dot-stripping**

In `tests/test_search_service.py`, update `test_build_match_query_with_filters`:
```python
def test_build_match_query_with_filters():
    q = build_match_query("wartung", filetype=".pdf", path_filter="/documents", block_type="page")
    assert "extension:pdf" in q        # dot stripped
    assert "extension:.pdf" not in q
    assert "path:/documents*" in q
    assert "block_type:page" in q
```

- [ ] **Step 4: Update `SearchRequest` in `app.py`**

Find `SearchRequest` (line ~104). Change:
```python
query: str = Field(min_length=1)
tag: str | None = None
```
to:
```python
query: str = ""
tags: list[str] = Field(default_factory=list)
```

- [ ] **Step 5: Add `import sqlite3` to `app.py`**

Check whether `import sqlite3` is already at the top of `app.py`. If not, add it after the existing stdlib imports.

- [ ] **Step 6: Run tests**

```bash
pytest -q tests/test_search_service.py -v
```
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add document_search/index/search_service.py document_search/app.py tests/test_search_service.py
git commit -m "fix: handle empty/wildcard query with browse-all fallback; catch FTS syntax errors as 400"
```

---

## Task 3: Fix Extension Filter (comma-split + dot-strip)

Already implemented in Task 2 (`build_match_query` rewrite). This task only adds the explicit tests.

- [ ] **Step 1: Add comma and dot tests**

In `tests/test_search_service.py`, add:
```python
def test_build_match_query_comma_separated_extensions():
    q = build_match_query("report", filetype="pdf,docx", path_filter=None, block_type=None)
    assert "(extension:pdf OR extension:docx)" in q

def test_build_match_query_single_extension_no_dot():
    q = build_match_query("report", filetype=".pdf", path_filter=None, block_type=None)
    assert "extension:pdf" in q
    assert "OR" not in q
```

Run: `pytest -q tests/test_search_service.py -v`
Expected: all pass.

- [ ] **Step 2: Commit**

```bash
git add tests/test_search_service.py
git commit -m "test: add explicit comma-split and dot-strip tests for build_match_query"
```

---

## Task 4: Block Dangerous Index Paths

**Files:**
- Modify: `document_search/app.py`
- Create: `tests/test_app_search.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_app_search.py`:
```python
import pytest
from fastapi.testclient import TestClient
from document_search.app import app


@pytest.fixture
def client():
    return TestClient(app)


def auth_headers(client):
    r = client.post("/api/login", json={"username": "admin", "password": "admin"})
    return {"X-Auth-Token": r.json()["token"]}


def test_index_start_blocks_root(client):
    resp = client.post("/api/index/start", json={"paths": ["/"]}, headers=auth_headers(client))
    assert resp.status_code == 400
    assert "not allowed" in resp.json()["detail"].lower()


def test_index_start_blocks_proc(client):
    resp = client.post("/api/index/start", json={"paths": ["/proc/sys"]}, headers=auth_headers(client))
    assert resp.status_code == 400


def test_index_start_blocks_sys(client):
    resp = client.post("/api/index/start", json={"paths": ["/sys/kernel"]}, headers=auth_headers(client))
    assert resp.status_code == 400


def test_index_start_blocks_dev(client):
    resp = client.post("/api/index/start", json={"paths": ["/dev/null"]}, headers=auth_headers(client))
    assert resp.status_code == 400
```

Run: `pytest -q tests/test_app_search.py::test_index_start_blocks_root -v`
Expected: FAIL (currently returns 202/200)

- [ ] **Step 2: Add path guard to `api_index_start`**

In `app.py`, find `api_index_start` (line ~612). Add before `job_id = uuid.uuid4().hex`:
```python
_BLOCKED_EXACT = {"/", "/proc", "/sys", "/dev"}
_BLOCKED_PREFIXES = ("/proc/", "/sys/", "/dev/")
for p in req.paths:
    norm = os.path.normpath(p)
    if norm in _BLOCKED_EXACT or norm.startswith(_BLOCKED_PREFIXES):
        raise HTTPException(
            status_code=400,
            detail=f"Path '{p}' is not allowed. Select a specific subdirectory.",
        )
```

- [ ] **Step 3: Run tests**

```bash
pytest -q tests/test_app_search.py -v
```
Expected: all four path-block tests pass.

- [ ] **Step 4: Commit**

```bash
git add document_search/app.py tests/test_app_search.py
git commit -m "fix: reject root and system paths in /api/index/start"
```

---

## Task 5: New Backend Endpoints

**Files:**
- Modify: `document_search/app.py`

- [ ] **Step 1: Add `GET /api/index/extensions`**

Inside `create_app()`, near the other `/api/index/*` routes:
```python
@app.get("/api/index/extensions")
def api_index_extensions(x_auth_token: str | None = Header(default=None)):
    require_user(x_auth_token)
    db = store()
    rows = db.conn.execute(
        "SELECT extension, COUNT(*) AS cnt FROM documents "
        "GROUP BY extension ORDER BY cnt DESC"
    ).fetchall()
    return [row["extension"] for row in rows if row["extension"]]
```

- [ ] **Step 2: Add `GET /api/folders`**

```python
@app.get("/api/folders")
def api_folders(x_auth_token: str | None = Header(default=None)):
    require_user(x_auth_token)
    root = str(upload_root)   # upload_root is defined in create_app() scope
    if not os.path.isdir(root):
        return []
    results = []
    for dirpath, dirnames, _ in os.walk(root):
        depth = os.path.relpath(dirpath, root).count(os.sep)
        if depth >= 2:
            dirnames.clear()
            continue
        rel = os.path.relpath(dirpath, root)
        if rel != ".":
            results.append(rel)
    return results
```

- [ ] **Step 3: Add `GET /api/source-folders`**

```python
@app.get("/api/source-folders")
def api_source_folders(x_auth_token: str | None = Header(default=None)):
    require_user(x_auth_token)
    cfg = load_effective_config()
    results = []
    for sp in cfg.source_paths:
        path = sp.path
        label = sp.label or os.path.basename(path.rstrip("/"))
        if not os.path.isdir(path):
            continue
        results.append({"path": path, "label": label, "is_root": True})
        try:
            for entry in sorted(os.scandir(path), key=lambda e: e.name):
                if entry.is_dir():
                    results.append({
                        "path": entry.path,
                        "label": entry.name,
                        "is_root": False,
                    })
        except PermissionError:
            pass
    return results
```

`cfg.source_paths` is a list of `SourcePath` objects (defined at line ~116 in `app.py`) with `.path` and `.label` string attributes.

- [ ] **Step 4: Add smoke tests**

In `tests/test_app_search.py`, add:
```python
def test_index_extensions_endpoint(client):
    resp = client.get("/api/index/extensions", headers=auth_headers(client))
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_folders_endpoint_no_root(client, monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_UPLOAD_ROOT", "/nonexistent_seekr_test")
    resp = client.get("/api/folders", headers=auth_headers(client))
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_source_folders_endpoint(client):
    resp = client.get("/api/source-folders", headers=auth_headers(client))
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
```

Run: `pytest -q tests/test_app_search.py -v`

- [ ] **Step 5: Commit**

```bash
git add document_search/app.py tests/test_app_search.py
git commit -m "feat: add /api/index/extensions, /api/folders, /api/source-folders endpoints"
```

---

## Task 6: Multi-Tag Search (AND Logic)

Already implemented in Task 2 (`search()` rewrite and `SearchRequest.tags` change). This task only adds verification.

- [ ] **Step 1: Confirm `api_search` passes `req.tags`**

Find `api_search` (line ~1419). The call to `search(...)` must pass `req.tags` (list) not `req.tag`. It should read:
```python
rows = search(
    db, req.query, req.limit, req.filetype, req.path,
    req.block_type, req.modified_from, req.modified_to, req.tags, user_id,
)
```

- [ ] **Step 2: Add API test**

In `tests/test_app_search.py`, add:
```python
def test_search_accepts_tags_list(client):
    resp = client.post(
        "/api/search",
        json={"query": "", "tags": ["tag1", "tag2"]},
        headers=auth_headers(client),
    )
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
```

Run: `pytest -q tests/test_app_search.py::test_search_accepts_tags_list -v`

- [ ] **Step 3: Commit**

```bash
git add document_search/app.py tests/test_app_search.py
git commit -m "feat: multi-tag search with AND semantics via HAVING COUNT DISTINCT"
```

---

## Task 7: Grouped Search Response

**Files:**
- Modify: `document_search/app.py` (`api_search` only)

- [ ] **Step 1: Rewrite `api_search`**

Replace the body of `api_search` (lines ~1420–1430):
```python
@app.post("/api/search")
def api_search(req: SearchRequest, x_auth_token: str | None = Header(default=None)):
    user_id = require_user(x_auth_token)
    db = store()
    try:
        rows = search(
            db, req.query, req.limit, req.filetype, req.path,
            req.block_type, req.modified_from, req.modified_to, req.tags, user_id,
        )
    except sqlite3.OperationalError as e:
        raise HTTPException(status_code=400, detail=f"Search query error: {e}")

    grouped: dict[int, dict] = {}
    order: list[int] = []
    for row in rows:
        r = dict(row)
        doc_id = r["document_id"]
        if doc_id not in grouped:
            order.append(doc_id)
            grouped[doc_id] = {
                "document_id": doc_id,
                "filename": r["filename"],
                "path": r["path"],
                "extension": r["extension"],
                "modified_at": r["modified_at"],
                "hits": [],
            }
        grouped[doc_id]["hits"].append({
            "block_type": r["block_type"],
            "block_number": r["block_number"],
            "snippet_html": highlight_terms(r.get("snippet") or "", req.query) or None,
        })

    marks = db.get_doc_marks_and_tags(user_id, order)
    output = []
    for doc_id in order:
        doc = grouped[doc_id]
        m = marks.get(doc_id, {"is_marked": False, "tags": []})
        output.append({
            **doc,
            "is_marked": m["is_marked"],
            "tags": m["tags"],
            "open_url": f"/api/files/open?document_id={doc_id}",
            "hit_count": len(doc["hits"]),
        })
    return output
```

- [ ] **Step 2: Add grouped-shape test**

In `tests/test_app_search.py`, add:
```python
def test_search_response_is_grouped(client):
    resp = client.post("/api/search", json={"query": ""}, headers=auth_headers(client))
    assert resp.status_code == 200
    data = resp.json()
    if data:
        doc = data[0]
        assert "hits" in doc
        assert "hit_count" in doc
        assert "document_id" in doc
        assert isinstance(doc["hits"], list)
        if doc["hits"]:
            hit = doc["hits"][0]
            assert "block_type" in hit
            assert "block_number" in hit
            assert "snippet_html" in hit
```

Run: `pytest -q tests/test_app_search.py::test_search_response_is_grouped -v`

- [ ] **Step 3: Commit**

```bash
git add document_search/app.py tests/test_app_search.py
git commit -m "feat: group search results by document with per-document hits array"
```

---

## Task 8: ChipInput Component and CSS

**Files:**
- Modify: `document_search/web/static/styles.css`
- Modify: `document_search/web/static/app.js`

- [ ] **Step 1: Add chip and hit styles to `styles.css`**

Find the `.tag-chip` rule block. After it, add:
```css
/* ── ChipInput ── */
.chip-wrap {
  display: flex;
  flex-wrap: wrap;
  gap: .3rem;
  align-items: center;
  min-height: 2.25rem;
  padding: .3rem .5rem;
  border: 1px solid var(--b-md);
  border-radius: var(--r2);
  background: var(--surface);
  cursor: text;
}
.chip-wrap:focus-within {
  outline: 2px solid var(--blue);
  outline-offset: 1px;
}
.chip {
  display: inline-flex;
  align-items: center;
  gap: .25rem;
  padding: .15rem .45rem;
  border-radius: 999px;
  background: var(--blue-a);
  color: var(--blue);
  font-size: .8rem;
  white-space: nowrap;
}
.chip-x {
  cursor: pointer;
  font-size: .9rem;
  line-height: 1;
  opacity: .7;
}
.chip-x:hover { opacity: 1; }
.chip-input {
  flex: 1;
  min-width: 6rem;
  border: none;
  outline: none;
  background: transparent;
  font-size: .875rem;
  color: var(--txt-1);
}

/* ── Result hit list ── */
.rc-hits { margin: .5rem 0 .25rem; }
.rc-hit {
  display: flex;
  gap: .5rem;
  align-items: baseline;
  padding: .25rem 0;
  font-size: .85rem;
  border-top: 1px solid var(--b-lo);
}
.rc-hit-label {
  flex-shrink: 0;
  font-size: .72rem;
  font-weight: 600;
  color: var(--txt-3);
  background: var(--bg-2);
  padding: .1rem .35rem;
  border-radius: var(--r2);
  white-space: nowrap;
}
.rc-more-btn {
  font-size: .8rem;
  color: var(--blue);
  cursor: pointer;
  background: none;
  border: none;
  padding: .25rem 0;
}
```

- [ ] **Step 2: Add `ChipInput` class to `app.js`**

Insert at the very top of `app.js`, before line 1 (`let token = ...`):
```js
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
```

- [ ] **Step 3: Commit**

```bash
git add document_search/web/static/app.js document_search/web/static/styles.css
git commit -m "feat: add ChipInput JS class and chip/result-hit CSS styles"
```

---

## Task 9: Wire ChipInput to Search Page Filters

**Files:**
- Modify: `document_search/web/templates/search.html`
- Modify: `document_search/web/static/app.js`

- [ ] **Step 1: Replace filetype input in `search.html`**

Find (around line 131):
```html
<div class="f-col">
  <label class="f-label" for="filetype">File type</label>
  <input id="filetype" placeholder="pdf, docx, pptx, txt" />
  <span class="f-hint">Comma-separated or single value</span>
</div>
```

Replace with:
```html
<div class="f-col">
  <label class="f-label">File type</label>
  <div class="chip-wrap" id="filetypeWrap">
    <input id="filetypeInput" class="chip-input" placeholder="pdf, docx…" list="filetypeList" autocomplete="off" />
    <datalist id="filetypeList"></datalist>
  </div>
  <span class="f-hint">Press Enter or comma to add each type</span>
</div>
```

- [ ] **Step 2: Replace tag filter block in `search.html`**

Find (around line 158):
```html
<div class="f-col f-full">
  <label class="f-label">Filter by tag</label>
  <div id="tagFilterWrap" style="display:flex;gap:.4rem;align-items:center;flex-wrap:wrap;">
    <input id="tagFilterInput" placeholder="tag name" style="width:160px;" />
    <button class="btn btn-g btn-sm" onclick="clearTagFilter()">Clear tag</button>
  </div>
</div>
```

Replace with:
```html
<div class="f-col f-full">
  <label class="f-label">Filter by tag</label>
  <div class="chip-wrap" id="tagFilterWrap">
    <input id="tagFilterInput" class="chip-input" placeholder="tag name…" list="tagFilterList" autocomplete="off" />
    <datalist id="tagFilterList"></datalist>
  </div>
</div>
```

- [ ] **Step 3: Declare chip globals near top of `app.js`**

After `let token = localStorage.getItem(...)`, add:
```js
let chipFiletype, chipTagFilter, chipUploadTags;
const _resultTagChips = {};
```

- [ ] **Step 4: Initialize search chips after login**

In `app.js`, find the `login()` function. After `showAuthedPanels()`, add:
```js
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
}
```

There is also a `bootstrap()` function at the bottom of `app.js` that auto-logins from stored token. Add the same block there (same `data-page="search"` guard), after `showAuthedPanels()` is called.

- [ ] **Step 5: Add `loadFilterOptions()` function**

After `loadTagCloud()`:
```js
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
```

- [ ] **Step 6: Update `runSearch()` to read chip values**

In `runSearch()`, replace:
```js
filetype: filetype.value || null,
tag: document.getElementById('tagFilterInput')?.value?.trim() || null,
```
with:
```js
filetype: chipFiletype?.values().join(',') || null,
tags: chipTagFilter?.values() ?? [],
```

- [ ] **Step 7: Update `filterByTag()` to use chip**

Replace the body of `filterByTag`:
```js
function filterByTag(name) {
  chipTagFilter?.setValues([name]);
  const filterBody = document.getElementById('filterBody');
  if (filterBody?.classList.contains('hidden')) toggleFilters();
  runSearch();
}
```

- [ ] **Step 8: Update `clearSearch()` to clear chips**

In `clearSearch()`, after clearing the query input, add:
```js
chipFiletype?.clear();
chipTagFilter?.clear();
```

- [ ] **Step 9: Commit**

```bash
git add document_search/web/templates/search.html document_search/web/static/app.js
git commit -m "feat: replace filetype and tag filter inputs with ChipInput on search page"
```

---

## Task 10: Result Card Redesign (Grouped Hits)

**Files:**
- Modify: `document_search/web/static/app.js`

The API now returns grouped documents (Task 7). The frontend renders one card per document with a hits list.

- [ ] **Step 1: Add `buildHitEl()` helper**

After `runSearch()`, add:
```js
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
```

- [ ] **Step 2: Add `renderResults()` function**

After `buildHitEl()`, add:
```js
const HITS_SHOW_MAX = 5;

function renderResults(docs) {
  const el = document.getElementById('results');
  if (!el) return;
  el.replaceChildren();

  docs.forEach(doc => {
    const card = document.createElement('div');
    card.className = 'rc';

    // Head: filename link + star button
    const head = document.createElement('div');
    head.className = 'rc-head';
    const nameLink = document.createElement('a');
    nameLink.className = 'rc-name';
    nameLink.href = doc.open_url;
    nameLink.target = '_blank';
    nameLink.textContent = doc.filename;
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

    const openLink = document.createElement('a');
    openLink.className = 'btn btn-g btn-sm';
    openLink.href = doc.open_url;
    openLink.target = '_blank';
    openLink.textContent = 'Open file';

    foot.appendChild(tagWrap);
    foot.appendChild(saveBtn);
    foot.appendChild(markBtn);
    foot.appendChild(reindexBtn);
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
```

- [ ] **Step 3: Replace result rendering in `runSearch()`**

In `runSearch()`, find the block that starts `resultsEl.innerHTML = data.map(r => ...` (lines ~187–206). Replace it with:
```js
renderResults(data);
```

Keep the existing empty-state block that sets the "No results" SVG — place `renderResults(data)` in the `else` branch after that check.

- [ ] **Step 4: Update `saveTags()` to use chip values**

Replace:
```js
async function saveTags(documentId) {
  const input = document.getElementById(`tags-${documentId}`);
  const tags = input.value.split(',').map(s => s.trim()).filter(Boolean);
  await api('/api/documents/tags', 'POST', { document_id: documentId, tags });
  showToast('Tags saved', 'ok');
}
```
with:
```js
async function saveTags(documentId) {
  const chip = _resultTagChips[documentId];
  const tags = chip ? chip.values() : [];
  await api('/api/documents/tags', 'POST', { document_id: documentId, tags });
  showToast('Tags saved', 'ok');
}
```

- [ ] **Step 5: Simplify `toggleMark()` to avoid full re-render**

`toggleMark` currently calls `await runSearch()` which resets all chip state. Change to just update the visual and data:
```js
async function toggleMark(documentId, current) {
  await api('/api/documents/mark', 'POST', { document_id: documentId, is_marked: !current });
  showToast(current ? 'Unmarked' : 'Marked', 'ok');
}
```

- [ ] **Step 6: Verify manually**

Start the app. Run a search. Confirm:
- One card per document
- Each card shows snippets with block-type labels
- "Show N more" expander works inline without network call
- Tag chips in footer are pre-filled from `doc.tags`
- Save tags updates correctly
- Clicking a tag chip in the badges row sets the filter

- [ ] **Step 7: Commit**

```bash
git add document_search/web/static/app.js
git commit -m "feat: redesign result cards — grouped hits, show-more expander, chip tag editors"
```

---

## Task 11: Ingest Page Improvements

**Files:**
- Modify: `document_search/web/templates/ingest.html`
- Modify: `document_search/web/static/app.js`

- [ ] **Step 1: Replace upload tags input in `ingest.html`**

Find `id="uploadTags"`. Replace the `<input>` with:
```html
<div class="chip-wrap" id="uploadTagsWrap">
  <input id="uploadTagsInput" class="chip-input" placeholder="add tag…" list="uploadTagList" autocomplete="off" />
  <datalist id="uploadTagList"></datalist>
</div>
```

- [ ] **Step 2: Add datalist to folder input in `ingest.html`**

Find `id="uploadPath"`. Add `list` attribute and a datalist after:
```html
<input id="uploadPath" placeholder="subfolder/name" list="uploadFolderList" autocomplete="off" />
<datalist id="uploadFolderList"></datalist>
```

- [ ] **Step 3: Replace paths input with `<select multiple>` in `ingest.html`**

Find `id="paths"`. Replace with:
```html
<select id="pathsSelect" multiple size="6" style="width:100%;font-size:.875rem;">
  <option disabled value="">Loading source folders…</option>
</select>
```

- [ ] **Step 4: Initialize ingest chips after login in `app.js`**

In `login()` and in `bootstrap()`, add (guarded by `data-page="ingest"`):
```js
if (document.body?.dataset?.page === 'ingest') {
  chipUploadTags = new ChipInput(
    document.getElementById('uploadTagsWrap'),
    document.getElementById('uploadTagsInput'),
    document.getElementById('uploadTagList'),
  );
  initDropZone();
  loadIngestOptions();
}
```

Note: remove any existing `initDropZone()` call that runs unconditionally, since it's now inside the page guard.

- [ ] **Step 5: Add `loadIngestOptions()` function**

```js
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
```

- [ ] **Step 6: Update `uploadDocument()` to use chip values**

Find `fd.append('tags', uploadTags.value || '')`. Replace with:
```js
fd.append('tags', chipUploadTags?.values().join(',') || '');
```

- [ ] **Step 7: Update `startIndex()` to read from `<select multiple>`**

In `startIndex()`, replace:
```js
paths: paths.value.split(',').map(s => s.trim()).filter(Boolean),
```
with:
```js
paths: Array.from(document.getElementById('pathsSelect')?.selectedOptions ?? [])
             .map(o => o.value).filter(Boolean),
```

Add a guard at the start of the function:
```js
const selectedPaths = Array.from(
  document.getElementById('pathsSelect')?.selectedOptions ?? []
).map(o => o.value).filter(Boolean);
if (!selectedPaths.length) {
  showToast('Select at least one folder to index.', 'err');
  return;
}
```

Then use `selectedPaths` in the `api(...)` call instead of reading the select again.

- [ ] **Step 8: Verify manually**

Open the Ingest page after login:
- Tags field shows autocomplete suggestions; Enter adds a chip
- Folder input shows datalist suggestions from `/api/folders`
- Paths to index shows source folders in grouped optgroups
- Selecting a folder and clicking Start Index passes the correct path
- Uploading a file with chip tags works

- [ ] **Step 9: Commit**

```bash
git add document_search/web/templates/ingest.html document_search/web/static/app.js
git commit -m "feat: ingest page — ChipInput for tags, folder datalist, source-folder multi-select"
```

---

## Done

Run the full test suite:
```bash
pytest -q
```

UI smoke test:
1. Search with empty query → browse-all, grouped cards
2. Search with `*` → same browse-all result, no 500
3. Add `pdf,docx` chips to filetype filter → correctly filtered
4. Add two tag chips → AND logic applied
5. Result cards show hits with block labels, "Show N more" expander
6. Ingest page: tags chip, folder datalist, source-folder multi-select
