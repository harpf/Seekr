# Search Pagination, FTS Error Handling, and Optional Scheduled Re-index Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Seekr search paginate (the frontend currently hard-caps at 25 results with no "load more"), turn unbalanced-quote FTS5 parse errors into a friendly 400 instead of a raw 500, and add an **opt-in** scheduled re-index that reuses the existing persistent job queue (`index_paths`) — no new infrastructure.

**Architecture:**
- `search_service.search()` and `_browse_all()` gain an `offset: int = 0` parameter that is appended to the existing `LIMIT ?` clause as `LIMIT ? OFFSET ?`. The ACL subquery params already sit *before* the trailing `LIMIT` params, so the only ordering rule is: append `offset` to `params` *after* `limit` and *after* the ACL params — exactly where `limit` is appended today.
- `build_match_query()` produces an FTS5 MATCH string that can be syntactically invalid (e.g. unbalanced quote). The raised `sqlite3.OperationalError` is already partly caught in `api_search`; we add a dedicated, **catchable** exception (`FtsQueryError`) raised from a single guarded `execute` in the search path so both the FTS branch and any caller get a consistent, friendly error. `/api/search` maps it to HTTP 400 with a fixed user-facing message.
- `/api/search` keeps returning a **JSON array** body (existing tests assert `isinstance(resp.json(), list)`), but the request model gains `offset`, and pagination metadata is returned in **response headers** (`X-Total-Count`, `X-Has-More`, `X-Next-Offset`). This is fully backward-compatible: old clients ignore the headers; the new frontend reads them.
- A `Scheduler` helper thread (in `job_worker.py`) periodically calls `job_store.enqueue("index_paths", ...)` for configured `source_paths`. It is **off by default**, gated by a config flag (`scheduled_reindex`) and only started when an interval > 0 is configured. It reuses the already-registered `index_paths` handler — zero new job kinds, zero new tables.

**Tech Stack:** Python 3.11, SQLite (FTS5), FastAPI, threading, pytest. No new third-party dependencies.

**Scope boundaries:**

In scope:
- `offset` in `search()` / `_browse_all()` (LIMIT/OFFSET, justified below).
- `FtsQueryError` + graceful 400 on malformed queries.
- `/api/search` request `offset` field + `X-Total-Count` / `X-Has-More` / `X-Next-Offset` response headers + a `total` count query.
- Frontend "Load more" pager: appends pages, preserves filters, resets on a new search, respects ACL (it just calls the same endpoint).
- Opt-in scheduled re-index: `AppConfig.scheduled_reindex` (minutes; `0` = disabled) + a lightweight `Scheduler` thread that enqueues `index_paths` for `config.json`'s `source_paths` on the interval, wired into app startup/shutdown.

Out of scope (deferred):
- Keyset/cursor pagination (justified below — FTS rank ordering makes a stable opaque cursor awkward; LIMIT/OFFSET is correct and simple at our result scale).
- Per-document (vs per-block) total counts beyond a single `COUNT(DISTINCT document_id)` — the response `total` is the document count.
- Infinite scroll / virtual lists — a single "Load more" button is enough.
- Cron-style schedules, multiple independent schedules, or per-source intervals — one global interval over all configured source paths.
- Push/WebSocket notification of scheduled-run completion — the existing job-status polling already covers it.

---

## File Structure

**Modify:**
- `document_search/index/search_service.py` — add `offset` to `search()` and `_browse_all()`; add `FtsQueryError`; guard the FTS `execute`; add a `count_documents()` helper for the total.
- `document_search/app.py` — `SearchRequest.offset`; `api_search` reads `offset`, sets response headers, computes total, maps `FtsQueryError` → 400; `AppConfig` flag plumbed; wire `Scheduler` at startup/shutdown.
- `document_search/config.py` — `AppConfig.scheduled_reindex: int = 0`.
- `document_search/services/job_worker.py` — add a small `Scheduler` class.
- `document_search/web/static/app.js` — pagination state, "Load more" button, `renderResults(docs, append)`.
- `document_search/web/templates/search.html` — a "Load more" button container under `#results`.

**Create:**
- `tests/test_search_pagination.py` — unit tests for `offset` and `FtsQueryError` in `search_service`.
- `tests/test_app_search_paging.py` — TestClient tests for `/api/search` paging + headers + 400 on bad query.
- `tests/test_scheduler.py` — unit test for the `Scheduler` enqueue-on-tick behaviour.

**Untouched:**
- The ACL subquery in `acl_service.py` (unchanged; we only append `offset` after its params).
- The persistent job queue tables and `JobStore` (we reuse `enqueue`, no schema change).
- `/api/ha/search` (its `limit` is `ge=1, le=20`; no paging needed for Home Assistant).

---

## Key design decisions (locked)

- **LIMIT/OFFSET over keyset.** The FTS branch orders by `c.rank` (BM25), which is a *floating-point relevance score*, not a monotone unique key — two blocks can share a rank, so a keyset cursor on `rank` is neither stable nor unique without a tiebreaker, and exposing BM25 floats as an opaque cursor is fragile across SQLite versions. The `_browse_all` branch orders by `d.modified_at DESC`, which is also non-unique. Result sets here are small (hundreds, not millions); `LIMIT ? OFFSET ?` is O(offset) but offset stays tiny (page size 25, a handful of pages). We therefore use LIMIT/OFFSET. **The page size and offset count *blocks*, not documents** — the same as today's `LIMIT` (see the existing comment in `_browse_all`). The API groups blocks into documents *after* fetching, exactly as it does now; `has_more` is derived from whether the DB returned a full page of blocks.
- **Response stays a JSON array.** Existing tests assert `isinstance(resp.json(), list)`. Pagination metadata goes in headers (`X-Total-Count`, `X-Has-More`, `X-Next-Offset`). No body shape change, no breaking change.
- **`total` is the document count** matching the filters/ACL/query, via `COUNT(DISTINCT document_id)`. It is advisory (shown as "N results"); the pager itself relies on `X-Has-More`, not on `total`, so a slightly stale total never breaks paging.
- **`FtsQueryError`** is a new exception subclassing `ValueError`, raised by wrapping the FTS `execute` in a `try/except sqlite3.OperationalError`. We keep the existing `except sqlite3.OperationalError` in `api_search` as a belt-and-suspenders fallback, but the dedicated type lets us return a *fixed, friendly* message ("Could not parse your search query. Check quotes and operators.") instead of leaking the raw SQLite error text.
- **Scheduler is opt-in and reuses `index_paths`.** It enqueues the *same* job kind the manual button uses, so the already-registered handler runs it. Default interval `0` = disabled. When enabled, it reads `source_paths` from `config.json` at each tick (so config edits take effect without restart) and enqueues one `index_paths` job covering all of them, with `owner_user_id=None` (system-owned) and `max_retries=0`.

---

## Task 1: `offset` parameter in `search()` and `_browse_all()`

**Files:**
- Modify: `document_search/index/search_service.py`
- Test: `tests/test_search_pagination.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_search_pagination.py`:

```python
from pathlib import Path

import pytest

from document_search.index.sqlite_store import SqliteStore
from document_search.index.search_service import search, count_documents, FtsQueryError


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "test.db")


def _seed_docs(store, n):
    """Insert n documents each with one content block containing the word 'alpha'.

    Inserts directly to avoid extractor dependencies. Column lists match the real
    schema in sqlite_store.py: content_blocks requires NOT NULL `extractor` and
    `text_length`; content_fts column order is
    (document_id, block_id, path, filename, extension, block_type, block_number, text)."""
    for i in range(n):
        cur = store.conn.execute(
            "INSERT INTO documents(path, filename, extension, file_size, sha256, "
            "modified_at, indexed_at, status) "
            "VALUES(?,?,?,?,?,?,?, 'ok')",
            (f"/docs/doc{i}.txt", f"doc{i}.txt", ".txt", 10, f"hash{i}",
             f"2026-01-{(i % 28) + 1:02d}T00:00:00", "2026-01-01T00:00:00"),
        )
        doc_id = cur.lastrowid
        bcur = store.conn.execute(
            "INSERT INTO content_blocks(document_id, block_type, block_number, text, "
            "extractor, text_length) VALUES(?, 'paragraph', 0, ?, 'test', ?)",
            (doc_id, "alpha beta gamma", len("alpha beta gamma")),
        )
        block_id = bcur.lastrowid
        store.conn.execute(
            "INSERT INTO content_fts(rowid, document_id, block_id, path, filename, "
            "extension, block_type, block_number, text) VALUES(?,?,?,?,?,?,?,?,?)",
            (block_id, doc_id, block_id, f"/docs/doc{i}.txt", f"doc{i}.txt",
             ".txt", "paragraph", 0, "alpha beta gamma"),
        )
    store.conn.commit()


def test_search_offset_returns_disjoint_pages(store):
    _seed_docs(store, 30)
    page1 = search(store, "alpha", limit=10, offset=0, bypass_acl=True)
    page2 = search(store, "alpha", limit=10, offset=10, bypass_acl=True)
    ids1 = {r["document_id"] for r in page1}
    ids2 = {r["document_id"] for r in page2}
    assert len(page1) == 10
    assert len(page2) == 10
    assert ids1.isdisjoint(ids2), "pages must not overlap"


def test_browse_all_offset_paginates(store):
    _seed_docs(store, 30)
    # Empty query -> browse-all branch
    page1 = search(store, "", limit=10, offset=0, bypass_acl=True)
    page2 = search(store, "", limit=10, offset=10, bypass_acl=True)
    ids1 = {r["document_id"] for r in page1}
    ids2 = {r["document_id"] for r in page2}
    assert len(page1) == 10
    assert len(page2) == 10
    assert ids1.isdisjoint(ids2)


def test_offset_beyond_end_returns_empty(store):
    _seed_docs(store, 5)
    page = search(store, "alpha", limit=10, offset=100, bypass_acl=True)
    assert page == []


def test_count_documents_matches_total(store):
    _seed_docs(store, 7)
    total = count_documents(store, "alpha", bypass_acl=True)
    assert total == 7
    total_browse = count_documents(store, "", bypass_acl=True)
    assert total_browse == 7
```

> The seed column lists were verified against `sqlite_store.py:_init_schema`. If the schema changes later, adjust the INSERTs accordingly. The *behaviour* asserted (disjoint pages, empty past-end, count) is what matters.

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_search_pagination.py -v
```

Expected: `ImportError` (no `count_documents` / `FtsQueryError`) and/or `TypeError` (no `offset` kwarg).

- [ ] **Step 3: Add `offset` to `_browse_all` and `search`, plus `count_documents` and `FtsQueryError`**

In `document_search/index/search_service.py`:

First, at the top of the file (after the existing import), add:

```python
import sqlite3


class FtsQueryError(ValueError):
    """Raised when an FTS5 MATCH query is syntactically invalid (e.g. unbalanced
    quote). Callers should translate this into a user-facing 400, not a 500."""
```

Change the `_browse_all` signature to accept `offset` (add it right after `limit`):

```python
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
    offset: int = 0,
    bypass_acl: bool = False,
):
```

At the **end** of `_browse_all`, change the `LIMIT ?` / `params.append(limit)` / `execute` block from:

```python
        ORDER BY d.modified_at DESC
        LIMIT ?
    """
    params.append(limit)
    return store.conn.execute(sql, tuple(params)).fetchall()
```

to:

```python
        ORDER BY d.modified_at DESC
        LIMIT ? OFFSET ?
    """
    params.append(limit)
    params.append(offset)
    return store.conn.execute(sql, tuple(params)).fetchall()
```

Change the `search` signature to accept `offset` (add it right after `limit`):

```python
def search(
    store: SqliteStore,
    query: str,
    limit: int = 20,
    offset: int = 0,
    filetype: str | None = None,
    path_filter: str | None = None,
    block_type: str | None = None,
    modified_from: str | None = None,
    modified_to: str | None = None,
    tags: list[str] | None = None,
    user_id: int | None = None,
    bypass_acl: bool = False,
):
```

> **Positional-arg caution:** today's only positional callers pass `(store, query, limit, ...)`. Inserting `offset` as the 4th positional parameter shifts `filetype` and friends. The web caller (`app.py:api_search`) and the HA caller (`app.py:970`) pass `filetype`/`path_filter` **positionally**. Task 3 updates those call sites to pass `offset` explicitly. The CLI (`main.py`) uses keyword args or only `query`/`limit`; verify in Task 3 Step 5. Do **not** skip the call-site audit.

Update the `_browse_all` call inside `search` to forward `offset`:

```python
    if match_query is None:
        return _browse_all(
            store, filetype, path_filter, block_type,
            modified_from, modified_to, tags, user_id, limit, offset,
            bypass_acl=bypass_acl,
        )
```

At the **end** of `search`, change the FTS `LIMIT`/`execute` block from:

```python
    sql += " ORDER BY c.rank LIMIT ?"
    params.append(limit)
    return store.conn.execute(sql, tuple(params)).fetchall()
```

to:

```python
    sql += " ORDER BY c.rank LIMIT ? OFFSET ?"
    params.append(limit)
    params.append(offset)
    try:
        return store.conn.execute(sql, tuple(params)).fetchall()
    except sqlite3.OperationalError as exc:
        # FTS5 raises OperationalError on malformed MATCH strings
        # (e.g. unbalanced quotes). Surface a catchable, friendly type.
        raise FtsQueryError(str(exc)) from exc
```

Finally, append a `count_documents` helper at the end of the file:

```python
def count_documents(
    store: SqliteStore,
    query: str,
    filetype: str | None = None,
    path_filter: str | None = None,
    block_type: str | None = None,
    modified_from: str | None = None,
    modified_to: str | None = None,
    tags: list[str] | None = None,
    user_id: int | None = None,
    bypass_acl: bool = False,
) -> int:
    """Return the number of DISTINCT documents matching the query + filters + ACL.

    Mirrors the filter/ACL logic of `search`/`_browse_all` but counts documents
    (not blocks), so the UI can show a total. Used by `/api/search` for the
    X-Total-Count header. Advisory only — paging relies on X-Has-More."""
    if user_id is None and not bypass_acl:
        raise ValueError("user_id is required unless bypass_acl=True is set explicitly")
    tags = [t.lower().strip() for t in (tags or [])]
    match_query = build_match_query(query, filetype, block_type)

    where = ["1=1"]
    params: list = []
    if match_query is not None:
        base = (
            "SELECT COUNT(DISTINCT d.id) FROM content_fts c "
            "JOIN documents d ON d.id = c.document_id "
            "JOIN content_blocks b ON b.id = c.block_id "
            "WHERE content_fts MATCH ?"
        )
        params.append(match_query)
        if path_filter:
            base += " AND d.path LIKE ?"
            params.append(path_filter + "%")
        if modified_from:
            base += " AND d.modified_at >= ?"
            params.append(modified_from)
        if modified_to:
            base += " AND d.modified_at <= ?"
            params.append(modified_to)
        if tags and user_id is not None:
            tag_ph = ",".join("?" * len(tags))
            base += (
                " AND d.id IN (SELECT dt.document_id FROM document_tags dt "
                "JOIN user_tags ut ON ut.id = dt.tag_id "
                f"WHERE dt.user_id = ? AND ut.name IN ({tag_ph}) "
                "GROUP BY dt.document_id HAVING COUNT(DISTINCT ut.name) = ?)"
            )
            params.extend([user_id] + tags + [len(tags)])
        if not bypass_acl:
            from document_search.services.acl_service import visible_document_ids_subquery
            acl_sql, acl_params = visible_document_ids_subquery(user_id)
            base += f" AND d.id IN ({acl_sql})"
            params.extend(acl_params)
        try:
            row = store.conn.execute(base, tuple(params)).fetchone()
        except sqlite3.OperationalError as exc:
            raise FtsQueryError(str(exc)) from exc
        return int(row[0]) if row else 0

    # Browse-all branch (no MATCH).
    if filetype:
        exts = ["." + e.strip().lstrip(".") for e in filetype.split(",") if e.strip()]
        placeholders = ",".join("?" * len(exts))
        where.append(f"d.extension IN ({placeholders})")
        params.extend(exts)
    if path_filter:
        where.append("d.path LIKE ?")
        params.append(path_filter + "%")
    if block_type:
        where.append(
            "d.id IN (SELECT document_id FROM content_blocks WHERE block_type = ?)"
        )
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
            "d.id IN (SELECT dt.document_id FROM document_tags dt "
            "JOIN user_tags ut ON ut.id = dt.tag_id "
            f"WHERE dt.user_id = ? AND ut.name IN ({tag_ph}) "
            "GROUP BY dt.document_id HAVING COUNT(DISTINCT ut.name) = ?)"
        )
        params.extend([user_id] + tags + [len(tags)])
    if not bypass_acl:
        from document_search.services.acl_service import visible_document_ids_subquery
        acl_sql, acl_params = visible_document_ids_subquery(user_id)
        where.append(f"d.id IN ({acl_sql})")
        params.extend(acl_params)
    sql = f"SELECT COUNT(DISTINCT d.id) FROM documents d WHERE {' AND '.join(where)}"
    row = store.conn.execute(sql, tuple(params)).fetchone()
    return int(row[0]) if row else 0
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_search_pagination.py -v
```

Expected: the 4 offset/count tests pass. (`FtsQueryError` import resolves; the malformed-query test lives in Task 2.)

- [ ] **Step 5: Full suite (will reveal call-site breakage)**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: existing search tests may FAIL here because positional callers now pass `offset` into the `filetype` slot. **Do not fix call sites yet** — Task 3 fixes `app.py`/`main.py`. If you prefer green-at-every-step, jump to Task 3 Step 3 before running the full suite. (TDD-wise this is the "integration not yet wired" gap; it closes in Task 3.)

- [ ] **Step 6: Commit**

```powershell
git add document_search/index/search_service.py tests/test_search_pagination.py
git commit -m @'
feat(search): add offset pagination and count_documents to search_service

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 2: Graceful FTS parse-error handling in `search_service`

**Files:**
- Modify: `document_search/index/search_service.py` (already raises `FtsQueryError` from Task 1 — this task only adds the test proving it)
- Test: `tests/test_search_pagination.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_search_pagination.py`:

```python
def test_unbalanced_quote_raises_fts_query_error(store):
    _seed_docs(store, 3)
    # An unbalanced double-quote is invalid FTS5 syntax.
    with pytest.raises(FtsQueryError):
        search(store, 'alpha "beta', limit=10, offset=0, bypass_acl=True)


def test_fts_query_error_is_value_error_subclass():
    assert issubclass(FtsQueryError, ValueError)


def test_count_documents_unbalanced_quote_raises(store):
    _seed_docs(store, 3)
    with pytest.raises(FtsQueryError):
        count_documents(store, 'alpha "beta', bypass_acl=True)
```

- [ ] **Step 2: Run, expect PASS**

Because Task 1 already wraps both `execute` calls, these should pass immediately:

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_search_pagination.py::test_unbalanced_quote_raises_fts_query_error tests/test_search_pagination.py::test_fts_query_error_is_value_error_subclass tests/test_search_pagination.py::test_count_documents_unbalanced_quote_raises -v
```

Expected: 3 passing. If `test_unbalanced_quote_raises_fts_query_error` *doesn't* raise, FTS5 may have tolerated the input on this SQLite build — make the query unambiguously invalid by using a NEAR with a bad arg, e.g. `'NEAR(alpha, )'`, and re-run. (Unbalanced quote is the documented common case and triggers on stock SQLite ≥ 3.40.)

- [ ] **Step 3: Commit**

```powershell
git add document_search/index/search_service.py tests/test_search_pagination.py
git commit -m @'
fix(search): raise catchable FtsQueryError on malformed FTS5 queries

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 3: Wire `offset`, total, headers, and 400 into `/api/search`

**Files:**
- Modify: `document_search/app.py` — `SearchRequest.offset`; update `api_search` to pass `offset` (keyword), compute `total`, set headers, map `FtsQueryError` → 400; fix the other positional `search(...)` call sites.
- Test: `tests/test_app_search_paging.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_app_search_paging.py`:

```python
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from document_search.app import create_app
from document_search.index.sqlite_store import SqliteStore


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "index.db"))
    return app, TestClient(app), tmp_path / "index.db"


def _auth(c):
    r = c.post("/api/login", json={"username": "admin", "password": "admin"})
    return {"X-Auth-Token": r.json()["token"]}


def _seed(db_path, n):
    store = SqliteStore(db_path)
    for i in range(n):
        cur = store.conn.execute(
            "INSERT INTO documents(path, filename, extension, file_size, sha256, "
            "modified_at, indexed_at, status) VALUES(?,?,?,?,?,?,?, 'ok')",
            (f"/docs/doc{i}.txt", f"doc{i}.txt", ".txt", 10, f"h{i}",
             f"2026-01-{(i % 28) + 1:02d}T00:00:00", "2026-01-01T00:00:00"),
        )
        doc_id = cur.lastrowid
        bcur = store.conn.execute(
            "INSERT INTO content_blocks(document_id, block_type, block_number, text, "
            "extractor, text_length) VALUES(?, 'paragraph', 0, ?, 'test', ?)",
            (doc_id, "alpha beta gamma", len("alpha beta gamma")),
        )
        bid = bcur.lastrowid
        store.conn.execute(
            "INSERT INTO content_fts(rowid, document_id, block_id, path, filename, "
            "extension, block_type, block_number, text) VALUES(?,?,?,?,?,?,?,?,?)",
            (bid, doc_id, bid, f"/docs/doc{i}.txt", f"doc{i}.txt", ".txt",
             "paragraph", 0, "alpha beta gamma"),
        )
    store.conn.commit()
    store.conn.close()


def test_search_returns_list_and_pagination_headers(client):
    app, c, db = client
    _seed(db, 30)
    h = _auth(c)
    r = c.post("/api/search", json={"query": "alpha", "limit": 10, "offset": 0}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list)        # backward-compatible shape
    assert len(body) == 10
    assert r.headers["X-Total-Count"] == "30"
    assert r.headers["X-Has-More"] == "true"
    assert r.headers["X-Next-Offset"] == "10"


def test_search_second_page_disjoint(client):
    app, c, db = client
    _seed(db, 30)
    h = _auth(c)
    p1 = c.post("/api/search", json={"query": "alpha", "limit": 10, "offset": 0}, headers=h).json()
    p2 = c.post("/api/search", json={"query": "alpha", "limit": 10, "offset": 10}, headers=h).json()
    ids1 = {d["document_id"] for d in p1}
    ids2 = {d["document_id"] for d in p2}
    assert ids1.isdisjoint(ids2)


def test_search_last_page_has_more_false(client):
    app, c, db = client
    _seed(db, 12)
    h = _auth(c)
    r = c.post("/api/search", json={"query": "alpha", "limit": 10, "offset": 10}, headers=h)
    assert r.status_code == 200
    assert len(r.json()) == 2
    assert r.headers["X-Has-More"] == "false"


def test_search_unbalanced_quote_returns_400(client):
    app, c, db = client
    _seed(db, 3)
    h = _auth(c)
    r = c.post("/api/search", json={"query": 'alpha "beta'}, headers=h)
    assert r.status_code == 400, r.text
    assert "query" in r.json()["detail"].lower()
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_search_paging.py -v
```

Expected: header assertions fail (headers not set yet) and/or 400 test fails (raw 500 / different detail).

- [ ] **Step 3: Add `offset` to `SearchRequest`**

In `document_search/app.py`, extend `SearchRequest` (around line 110):

```python
class SearchRequest(BaseModel):
    query: str = ""
    tags: list[str] = Field(default_factory=list)
    limit: int = 20
    offset: int = 0
    filetype: str | None = None
    path: str | None = None
    block_type: str | None = None
    modified_from: str | None = None
    modified_to: str | None = None
```

- [ ] **Step 4: Update `api_search` to page, count, set headers, and map the error**

Replace the whole `api_search` function (lines ~1579–1621) with:

```python
    @app.post("/api/search")
    def api_search(
        req: SearchRequest,
        response: Response,
        x_auth_token: str | None = Header(default=None),
    ):
        user_id = require_user(x_auth_token)
        db = store()
        limit = max(1, min(req.limit, 100))
        offset = max(0, req.offset)
        try:
            rows = search(
                db, req.query, limit, offset,
                req.filetype, req.path, req.block_type,
                req.modified_from, req.modified_to, req.tags, user_id,
            )
            total = count_documents(
                db, req.query,
                filetype=req.filetype, path_filter=req.path,
                block_type=req.block_type, modified_from=req.modified_from,
                modified_to=req.modified_to, tags=req.tags, user_id=user_id,
            )
        except FtsQueryError:
            raise HTTPException(
                status_code=400,
                detail="Could not parse your search query. Check quotes and operators.",
            )
        except sqlite3.OperationalError as e:
            # Defensive fallback for any other SQLite parse error.
            raise HTTPException(status_code=400, detail=f"Search query error: {e}")

        # Group flat rows by document_id, preserving rank order
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

        # Pagination metadata in headers (body stays a JSON array for compat).
        # has_more is true when the DB returned a full page of *blocks* — i.e.
        # there may be more rows past this offset. This mirrors the block-level
        # LIMIT/OFFSET; the document grouping happens after fetching.
        has_more = len(rows) >= limit
        response.headers["X-Total-Count"] = str(total)
        response.headers["X-Has-More"] = "true" if has_more else "false"
        response.headers["X-Next-Offset"] = str(offset + limit) if has_more else str(offset)
        # Make the custom headers readable by the browser fetch() client.
        response.headers["Access-Control-Expose-Headers"] = (
            "X-Total-Count, X-Has-More, X-Next-Offset"
        )
        return output
```

Add the imports this needs. Find the existing `from document_search.index.search_service import ...` line and extend it; if `search` is imported alone, change it to:

```python
from document_search.index.search_service import search, count_documents, FtsQueryError
```

Ensure `Response` is imported from FastAPI. The existing import line is:

```python
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
```

Add `Response` to it:

```python
from fastapi import FastAPI, File, Form, Header, HTTPException, Response, UploadFile
```

> `json` is already imported at module top (`app.py:2`), so the scheduler config-read in Task 5 needs no new import.

- [ ] **Step 5: Fix the other positional `search(...)` call sites**

Two other call sites pass args positionally and will now misalign because `offset` is the new 4th positional:

1. `app.py:970` (HA search):
```python
        rows = search(db, query, limit, None, path_filter, None, None, None, None, None, bypass_acl=True)
```
Change to insert `offset=0` in the new 4th slot (after `limit`):
```python
        rows = search(db, query, limit, 0, None, path_filter, None, None, None, None, None, bypass_acl=True)
```

2. `document_search/main.py` (CLI). Open it and inspect the `search(` call. If it passes `limit` positionally followed by more positionals, insert `0` after `limit` the same way; if it uses keyword args, no change is needed. Run the CLI tests after fixing:

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_search_service.py tests/test_search_acl.py
```

> `tests/test_search_service.py` only tests `build_match_query` (untouched) and should stay green. `tests/test_search_acl.py` calls `search` — check whether it passes `filetype` positionally; if so, those calls also need the `0` inserted, OR convert them to keyword args. Prefer the smallest diff that keeps them green.

- [ ] **Step 6: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_search_paging.py tests/test_app_search.py -v
```

Expected: new paging tests pass; the existing `test_app_search.py` (which asserts `isinstance(resp.json(), list)`) stays green.

- [ ] **Step 7: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: all green. If `test_search_acl.py` fails on arg alignment, fix per Step 5.

- [ ] **Step 8: Commit**

```powershell
git add document_search/app.py document_search/main.py tests/test_app_search_paging.py
git commit -m @'
feat(search): paginate /api/search with offset and pagination headers, 400 on bad query

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 4: Frontend "Load more" pager

**Files:**
- Modify: `document_search/web/templates/search.html` — add a "Load more" container.
- Modify: `document_search/web/static/app.js` — pagination state, append rendering, button wiring.

This task has no automated test (no JS test harness in the repo). Verify manually in Step 5.

- [ ] **Step 1: Add the "Load more" container to the template**

In `document_search/web/templates/search.html`, replace the results block (lines ~184–186):

```html
        <!-- Results -->
        <div class="results-meta" id="resultsMeta"></div>
        <div id="results"></div>
```

with:

```html
        <!-- Results -->
        <div class="results-meta" id="resultsMeta"></div>
        <div id="results"></div>
        <div id="loadMoreWrap" style="display:none;text-align:center;margin:1rem 0;">
          <button id="loadMoreBtn" class="btn btn-g" type="button">Load more</button>
        </div>
```

- [ ] **Step 2: Add pagination state + a fetch helper near the top of the search section in `app.js`**

In `document_search/web/static/app.js`, just **above** `async function runSearch()` (line ~254), insert the pager state and a low-level fetch that exposes headers (the existing `api()` helper returns parsed JSON only and hides headers, so we use `fetch` directly here):

```javascript
// ---- Search pagination state ----
const PAGE_SIZE = 25;
let _searchState = { offset: 0, hasMore: false, total: 0, payloadBase: null, loading: false };

function _currentSearchPayload(offset) {
  return {
    query: query.value,
    limit: PAGE_SIZE,
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
  // Use fetch directly so we can read pagination headers.
  const token = localStorage.getItem('token') || '';
  const res = await fetch('/api/search', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Auth-Token': token },
    body: JSON.stringify(_currentSearchPayload(offset)),
  });
  if (!res.ok) {
    let detail = `Search failed (${res.status})`;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  const docs = await res.json();
  return {
    docs,
    total: Number(res.headers.get('X-Total-Count') || docs.length),
    hasMore: (res.headers.get('X-Has-More') || 'false') === 'true',
    nextOffset: Number(res.headers.get('X-Next-Offset') || offset + PAGE_SIZE),
  };
}

function _updateLoadMore() {
  const wrap = document.getElementById('loadMoreWrap');
  const btn = document.getElementById('loadMoreBtn');
  if (!wrap || !btn) return;
  wrap.style.display = _searchState.hasMore ? '' : 'none';
  btn.disabled = _searchState.loading;
  btn.textContent = _searchState.loading ? 'Loading…' : 'Load more';
}
```

> The token key is `localStorage.getItem('token')`. If this codebase stores the session token under a different key, match what `api()` uses — open `app.js` line ~72 (`async function api(...)`) and copy its exact token-header logic.

- [ ] **Step 3: Rewrite `runSearch()` to drive the first page, and make `renderResults` append-capable**

Replace `runSearch()` (lines ~254–291) with:

```javascript
async function runSearch() {
  const resultsEl = document.getElementById('results');
  _searchState.loading = true;
  _updateLoadMore();
  try {
    if (query.value?.trim()) saveRecentSearch(query.value);
    const page = await _fetchSearchPage(0);

    _searchState.offset = page.nextOffset;
    _searchState.hasMore = page.hasMore;
    _searchState.total = page.total;

    const metaEl = document.getElementById('resultsMeta');
    if (metaEl) {
      metaEl.textContent = page.total
        ? `${page.total} result${page.total !== 1 ? 's' : ''}`
        : '';
    }

    if (!page.docs.length) {
      resultsEl.innerHTML = `
        <div class="empty">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
          </svg>
          <p>No results found for this query.</p>
        </div>`;
      _searchState.hasMore = false;
      return;
    }

    renderResults(page.docs, false);
  } catch (e) {
    if (resultsEl) resultsEl.textContent = e.message;
    _searchState.hasMore = false;
  } finally {
    _searchState.loading = false;
    _updateLoadMore();
  }
}

async function loadMoreResults() {
  if (_searchState.loading || !_searchState.hasMore) return;
  _searchState.loading = true;
  _updateLoadMore();
  try {
    const page = await _fetchSearchPage(_searchState.offset);
    _searchState.offset = page.nextOffset;
    _searchState.hasMore = page.hasMore;
    renderResults(page.docs, true);
  } catch (e) {
    showToast(e.message, 'err');
  } finally {
    _searchState.loading = false;
    _updateLoadMore();
  }
}
```

Then change the `renderResults` signature and its clearing logic. Replace the head of `renderResults` (lines ~328–332):

```javascript
function renderResults(docs) {
  const el = document.getElementById('results');
  if (!el) return;
  el.replaceChildren();
  for (const k in _resultTagChips) delete _resultTagChips[k];
```

with:

```javascript
function renderResults(docs, append = false) {
  const el = document.getElementById('results');
  if (!el) return;
  if (!append) {
    el.replaceChildren();
    for (const k in _resultTagChips) delete _resultTagChips[k];
  }
```

Everything after that (the `docs.forEach(...)` body) is unchanged — when `append` is true the new cards are added after the existing ones because we no longer call `replaceChildren()`.

- [ ] **Step 4: Wire the button and reset the pager on `clearSearch`**

Find the event-wiring block near the bottom (around line 1713, where `queryEl.addEventListener('keydown', ...)` lives) and add a click handler for the button:

```javascript
  document.getElementById('loadMoreBtn')?.addEventListener('click', loadMoreResults);
```

In `clearSearch()` (lines ~243–252), hide the pager and reset state. After the line that clears `#results`, add:

```javascript
  _searchState = { offset: 0, hasMore: false, total: 0, payloadBase: null, loading: false };
  const lmWrap = document.getElementById('loadMoreWrap');
  if (lmWrap) lmWrap.style.display = 'none';
```

- [ ] **Step 5: Manual verification**

```powershell
$env:PYTHONPATH = "."; python -c "
from fastapi.testclient import TestClient
from document_search.app import create_app
from document_search.index.sqlite_store import SqliteStore
import tempfile, pathlib
tmp = pathlib.Path(tempfile.mkdtemp()); db = tmp / 'p.db'
app = create_app(str(db))
s = SqliteStore(db)
for i in range(60):
    cur = s.conn.execute(\"INSERT INTO documents(path,filename,extension,file_size,sha256,modified_at,indexed_at,status) VALUES(?,?,?,?,?,?,?, 'ok')\", (f'/d/doc{i}.txt', f'doc{i}.txt', '.txt', 10, f'h{i}', '2026-01-01T00:00:00', '2026-01-01T00:00:00'))
    did = cur.lastrowid
    b = s.conn.execute(\"INSERT INTO content_blocks(document_id,block_type,block_number,text,extractor,text_length) VALUES(?, 'paragraph', 0, 'alpha beta', 'test', 10)\", (did,)); bid = b.lastrowid
    s.conn.execute(\"INSERT INTO content_fts(rowid,document_id,block_id,path,filename,extension,block_type,block_number,text) VALUES(?,?,?,?,?,?,?,?,?)\", (bid, did, bid, f'/d/doc{i}.txt', f'doc{i}.txt', '.txt', 'paragraph', 0, 'alpha beta'))
s.conn.commit(); s.conn.close()
with TestClient(app) as c:
    tok = c.post('/api/login', json={'username':'admin','password':'admin'}).json()['token']
    r = c.post('/api/search', json={'query':'alpha','limit':25,'offset':0}, headers={'X-Auth-Token':tok})
    print('page1 len', len(r.json()), 'total', r.headers['X-Total-Count'], 'has_more', r.headers['X-Has-More'], 'next', r.headers['X-Next-Offset'])
    r2 = c.post('/api/search', json={'query':'alpha','limit':25,'offset':25}, headers={'X-Auth-Token':tok})
    print('page2 len', len(r2.json()), 'has_more', r2.headers['X-Has-More'])
print('OK')
"
```

Expected: `page1 len 25 total 60 has_more true next 25`, `page2 len 25 has_more true`, `OK`. (If you can run the server, open Search, query something with > 25 hits, and confirm the "Load more" button appears and appends a second page without losing filters.)

- [ ] **Step 6: Commit**

```powershell
git add document_search/web/static/app.js document_search/web/templates/search.html
git commit -m @'
feat(search): frontend Load more pager wired to offset pagination

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 5: Opt-in scheduled re-index (`Scheduler` reusing `index_paths`)

**Files:**
- Modify: `document_search/config.py` — add `scheduled_reindex: int = 0` (minutes; `0` = disabled).
- Modify: `document_search/services/job_worker.py` — add `Scheduler`.
- Modify: `document_search/app.py` — instantiate + start/stop the scheduler when enabled.
- Test: `tests/test_scheduler.py` (new).

- [ ] **Step 1: Write the failing test**

Create `tests/test_scheduler.py`:

```python
import time
from pathlib import Path

import pytest

from document_search.index.sqlite_store import SqliteStore
from document_search.services.job_store import JobStore
from document_search.services.job_worker import Scheduler


@pytest.fixture
def js(tmp_path):
    store = SqliteStore(tmp_path / "test.db")
    return JobStore(store)


def test_scheduler_tick_enqueues_index_paths_job(js):
    calls = {"n": 0}

    def paths_provider():
        return ["/docs/a", "/docs/b"]

    sched = Scheduler(js, paths_provider, interval_s=999, owner_user_id=None)
    enqueued = sched.tick()
    assert enqueued is not None  # returns the new job id
    job = js.get(enqueued)
    assert job["kind"] == "index_paths"
    import json
    assert json.loads(job["payload_json"])["paths"] == ["/docs/a", "/docs/b"]
    assert job["state"] == "pending"


def test_scheduler_tick_skips_when_no_paths(js):
    sched = Scheduler(js, lambda: [], interval_s=999)
    assert sched.tick() is None
    rows = js.list_jobs(kind="index_paths")
    assert rows == []


def test_scheduler_start_stop_is_clean(js):
    sched = Scheduler(js, lambda: ["/docs/a"], interval_s=0.05, owner_user_id=None)
    sched.start()
    # Give it time to fire at least once.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if js.list_jobs(kind="index_paths"):
            break
        time.sleep(0.02)
    sched.stop()
    assert js.list_jobs(kind="index_paths"), "scheduler should have enqueued at least one job"
```

- [ ] **Step 2: Run, expect ImportError**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_scheduler.py -v
```

Expected: `ImportError` — `Scheduler` doesn't exist yet.

- [ ] **Step 3: Implement `Scheduler` in `job_worker.py`**

Append to `document_search/services/job_worker.py`:

```python
class Scheduler:
    """Opt-in periodic enqueuer. Reuses the existing `index_paths` job kind, so
    the already-registered worker handler runs scheduled re-indexes — no new job
    kind, no new infrastructure.

    `paths_provider` is called at each tick so config edits (source_paths) take
    effect without a restart. If it returns an empty list, the tick is a no-op.
    """

    def __init__(
        self,
        job_store: "JobStore",
        paths_provider: Callable[[], list[str]],
        interval_s: float,
        owner_user_id: int | None = None,
    ):
        self.job_store = job_store
        self.paths_provider = paths_provider
        self.interval_s = interval_s
        self.owner_user_id = owner_user_id
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> int | None:
        """Enqueue one `index_paths` job covering all configured paths.
        Returns the job id, or None if there are no paths."""
        try:
            paths = [p for p in (self.paths_provider() or []) if p and p.strip()]
        except Exception:
            log.exception("Scheduler paths_provider failed")
            return None
        if not paths:
            return None
        job_id = self.job_store.enqueue(
            "index_paths",
            payload={"paths": paths, "config_path": None},
            owner_user_id=self.owner_user_id,
            max_retries=0,
        )
        log.info("Scheduler enqueued index_paths job %s for %d path(s)", job_id, len(paths))
        return job_id

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="ReindexScheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _loop(self) -> None:
        # Wait one interval before the first run (don't index on every boot).
        while not self._stop_event.wait(self.interval_s):
            try:
                self.tick()
            except Exception:
                log.exception("Scheduler tick error")
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_scheduler.py -v
```

Expected: 3 passing.

- [ ] **Step 5: Add the config flag**

In `document_search/config.py`, add a field to `AppConfig` (after `follow_symlinks`):

```python
    follow_symlinks: bool = False
    scheduled_reindex: int = 0  # minutes between automatic re-index runs; 0 = disabled
```

`load_config` already copies any matching key via the `hasattr(cfg, key)` loop, so a `"scheduled_reindex": 60` entry in `config.json` is picked up automatically.

- [ ] **Step 6: Wire the scheduler into app startup/shutdown**

In `document_search/app.py`, inside `create_app`, locate the `@app.on_event("startup")`/`_start_worker` block (around line 278). Replace those startup/shutdown hooks with versions that also manage the scheduler:

```python
    # Optional opt-in scheduled re-index. Disabled unless config.scheduled_reindex > 0.
    def _scheduled_paths() -> list[str]:
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        except Exception:
            return []
        return [sp["path"] for sp in raw.get("source_paths", []) if sp.get("path")]

    from document_search.services.job_worker import Scheduler
    _reindex_minutes = 0
    try:
        if config_path.exists():
            _reindex_minutes = int(
                json.loads(config_path.read_text(encoding="utf-8")).get("scheduled_reindex", 0)
            )
    except Exception:
        _reindex_minutes = 0
    scheduler = (
        Scheduler(job_store, _scheduled_paths, interval_s=_reindex_minutes * 60, owner_user_id=None)
        if _reindex_minutes > 0
        else None
    )
    app.state.scheduler = scheduler

    @app.on_event("startup")
    def _start_worker() -> None:
        job_store.mark_interrupted_running_jobs()
        worker.start()
        if scheduler is not None:
            scheduler.start()

    @app.on_event("shutdown")
    def _stop_worker() -> None:
        if scheduler is not None:
            scheduler.stop(timeout=5.0)
        worker.stop(timeout=5.0)
```

> `json` and `config_path` are already in scope in `create_app` (config_path at line 249; `json` is imported at module top — verify). The `index_paths` handler registered earlier validates nothing extra, but the scheduler only feeds it `source_paths` from config, which an operator has already vetted.

- [ ] **Step 7: Write the startup integration test**

Append to `tests/test_scheduler.py`:

```python
def test_app_enables_scheduler_when_configured(tmp_path):
    pytest.importorskip("fastapi")
    import json
    from fastapi.testclient import TestClient
    from document_search.app import create_app

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "scheduled_reindex": 1,
        "source_paths": [{"path": str(tmp_path / "src")}],
    }), encoding="utf-8")
    (tmp_path / "src").mkdir()

    import os
    os.environ["DOCUMENT_SEARCH_CONFIG_PATH"] = str(cfg)
    try:
        app = create_app(str(tmp_path / "t.db"))
        with TestClient(app):
            assert app.state.scheduler is not None
    finally:
        os.environ.pop("DOCUMENT_SEARCH_CONFIG_PATH", None)


def test_app_scheduler_disabled_by_default(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from document_search.app import create_app

    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app):
        assert app.state.scheduler is None
```

- [ ] **Step 8: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_scheduler.py -v
```

Expected: 5 passing.

- [ ] **Step 9: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: all green.

- [ ] **Step 10: Commit**

```powershell
git add document_search/config.py document_search/services/job_worker.py document_search/app.py tests/test_scheduler.py
git commit -m @'
feat(search): opt-in scheduled re-index reusing the index_paths job queue

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 6: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Full suite, clean**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: zero failures, zero flaky reruns.

- [ ] **Step 2: Confirm backward-compatible search shape**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_search.py -v
```

Expected: the pre-existing `test_search_response_is_grouped` and `test_search_accepts_tags_list` (which assert a JSON **array**) still pass — proving the header-based pagination didn't break the body contract.

- [ ] **Step 3: Confirm the malformed-query path returns 400, not 500**

```powershell
$env:PYTHONPATH = "."; python -c "
from fastapi.testclient import TestClient
from document_search.app import create_app
import tempfile, pathlib
tmp = pathlib.Path(tempfile.mkdtemp())
app = create_app(str(tmp / 'q.db'))
with TestClient(app) as c:
    tok = c.post('/api/login', json={'username':'admin','password':'admin'}).json()['token']
    r = c.post('/api/search', json={'query':'foo \"bar'}, headers={'X-Auth-Token':tok})
    print('status', r.status_code, 'detail', r.json().get('detail'))
    assert r.status_code == 400
print('OK')
"
```

Expected: `status 400 detail Could not parse your search query. ...`, then `OK`.

- [ ] **Step 4: No commit (verification only).**

---

## Definition of Done

- [ ] `pytest -q` green (all pre-existing tests + the new pagination, FTS-error, and scheduler tests).
- [ ] `search()` and `_browse_all()` accept `offset` and paginate via `LIMIT ? OFFSET ?`; offset past the end returns `[]`.
- [ ] `count_documents()` returns the DISTINCT-document total for both the FTS and browse-all branches, honouring filters + ACL.
- [ ] Malformed FTS queries raise `FtsQueryError` (a `ValueError` subclass) from both `search()` and `count_documents()`; `/api/search` maps it to **HTTP 400** with a fixed friendly message — never a 500.
- [ ] `/api/search` request accepts `offset`; response **body stays a JSON array** (backward-compatible) and exposes `X-Total-Count`, `X-Has-More`, `X-Next-Offset` headers.
- [ ] Frontend shows a "Load more" button when `X-Has-More` is true, appends the next page preserving all active filters, resets on a new search / clear, and respects ACL (it only ever calls `/api/search`).
- [ ] Scheduled re-index is **off by default**; when `config.scheduled_reindex` (minutes) > 0, a `Scheduler` thread enqueues an `index_paths` job for the configured `source_paths` on the interval, reusing the existing worker handler. No new job kind, no new table.
- [ ] `/api/ha/search` and the CLI `search` call sites are updated for the new positional `offset` and still pass their tests.

---

## Notes for the executing agent

- **Why LIMIT/OFFSET and not keyset:** the FTS branch's `ORDER BY c.rank` is a non-unique BM25 float; `_browse_all` orders by non-unique `modified_at`. A correct keyset cursor needs a unique, monotone tiebreaker that neither ordering provides, and serialising BM25 floats as a cursor is fragile across SQLite builds. Result sets are small, so `OFFSET` is cheap. If result volumes ever grow to where `OFFSET` scans hurt, add a `document_id` tiebreaker to the ORDER BY and switch to keyset in a follow-up — the API's `X-Next-Offset` header can become an opaque cursor without changing the frontend contract much.
- **Why pagination metadata is in headers, not the body:** `tests/test_app_search.py` asserts `isinstance(resp.json(), list)`. Wrapping the body in `{results, total, ...}` would break that contract and every existing client. Headers are additive and invisible to old clients.
- **`has_more` counts blocks, not documents.** The DB pages *blocks*; the API groups them into documents afterward. `has_more = len(rows) >= limit` can occasionally be true on the very last page if it happened to fill exactly (the next fetch then returns `[]` and the button disappears). This is the standard, acceptable "one extra empty fetch" behaviour and is simpler than a separate look-ahead query. `X-Total-Count` (document count) is what we show the user.
- **The `offset` positional-arg shift is the riskiest part.** Inserting `offset` as `search()`'s 4th positional parameter changes every positional caller. Audit them all (Task 3 Step 5): `app.py:api_search`, `app.py:970` (HA), `main.py` (CLI), and `tests/test_search_acl.py`. The full suite in Task 3 Step 7 is the gate — do not declare Task 3 done until it's green.
- **Why the scheduler re-reads config each tick:** operators change `source_paths` from the UI; re-reading at tick time means they don't have to restart. The interval itself is read once at startup (changing the cadence requires a restart) — that's an acceptable simplification; document it if you surface it in the UI later.
- **Scheduler is deliberately minimal:** one global interval, one job per tick covering all paths, system-owned (`owner_user_id=None`), `max_retries=0` (same as the manual button). It reuses the registered `index_paths` handler, so there is genuinely no new infrastructure — just a timer that calls `job_store.enqueue`.
- **First scheduled run waits one interval** (`_stop_event.wait(interval)` before the first `tick`) so enabling it doesn't kick off a full index on every process boot.
- **CORS/header exposure:** `Access-Control-Expose-Headers` is set on the response so a browser `fetch()` can read the custom headers even under cross-origin conditions. For same-origin (the normal Seekr deployment) it's harmless; for any reverse-proxy setup it prevents the headers from being stripped from JS visibility.
