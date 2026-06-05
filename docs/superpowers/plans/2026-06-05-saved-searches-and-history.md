# Saved Searches & Server-Side Search History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move "recent searches" off the browser's `localStorage` and onto the server, scoped per user. Add two persistent SQLite tables — `search_history` (capped, rolling, auto-recorded on every `/api/search`) and `saved_searches` (named, stores the full query + serialized filter set as JSON) — plus per-user CRUD endpoints, and rewire the frontend to use them with toast feedback.

**Architecture:** Two new tables live next to the existing per-user tables (`user_tags`, `document_tags`, `user_document_marks`). `SqliteStore` gains CRUD helpers that mirror the existing tag helpers (every method takes `user_id` first and scopes every query to it). `app.py` gains `require_user`-scoped routes that mirror the tags routes (`GET /api/tags`, `POST /api/documents/tags`) and the HA-key DELETE-by-id route (`DELETE /api/ha/keys/{key_id}`). The history row is recorded automatically inside the existing `/api/search` handler so the frontend never has to call a separate "record" endpoint. The frontend's `saveRecentSearch`/`renderRecentSearches` localStorage functions are replaced by server calls, and a new "Save this search" control plus a saved-searches list (which restores query **and** filters on click) are added to the search page.

**Tech Stack:** Python 3.11, SQLite (WAL, FTS5 — unchanged here), FastAPI, pytest, vanilla JS. No new third-party dependencies.

**Scope boundaries:**

In scope:
- `search_history` table: per user, rolling cap (newest N kept), de-duplicated on identical query+filters, auto-recorded by `/api/search`.
- `saved_searches` table: per user, named, unique name per user, stores query + serialized filters JSON.
- `SqliteStore` CRUD: `record_search_history`, `list_search_history`, `clear_search_history`, `create_saved_search`, `list_saved_searches`, `delete_saved_search`.
- Endpoints (all `require_user`, scoped to caller):
  - `GET /api/search/history` — list recent history.
  - `DELETE /api/search/history` — clear caller's history.
  - `GET /api/search/saved` — list saved searches.
  - `POST /api/search/saved` — create a saved search.
  - `DELETE /api/search/saved/{saved_id}` — delete one saved search (404 if not the caller's).
  - History auto-record wired into the existing `POST /api/search` handler.
- Frontend: replace localStorage recent-searches with `GET /api/search/history`; add a "Save this search" button + name prompt; render a saved-searches list whose entries restore the full query+filter set into the form on click; toast feedback on save/delete/clear.

Out of scope (deferred):
- Sharing saved searches between users / global saved searches.
- Editing (rename / re-define) an existing saved search — delete + re-create only.
- Server-side execution of a saved search (the frontend restores the form and re-runs the existing `/api/search`).
- A "history" page or pagination beyond the rolling cap.
- Migrating the legacy `localStorage['seekr_recent']` data into the DB (a fresh server-side history starts empty; the old key is simply ignored/removed).

---

## File Structure

**Create:**
- `tests/test_saved_searches_store.py` — unit tests for the six `SqliteStore` CRUD helpers.
- `tests/test_app_saved_searches.py` — TestClient endpoint tests, scoped per user (two-user isolation).

**Modify:**
- `document_search/index/sqlite_store.py` — add `search_history` + `saved_searches` tables to the `executescript` block in `_init_schema`; add the six CRUD helpers.
- `document_search/app.py` — add Pydantic models; add the five new routes; auto-record history inside `api_search`.
- `document_search/web/static/app.js` — replace `saveRecentSearch`/`renderRecentSearches`; capture/restore the full filter set; add "Save this search" + saved-searches list rendering.
- `document_search/web/templates/search.html` — add a "Saved searches" card and a "Save this search" button.
- `document_search/web/templates/index.html` — `#recentSearches` becomes server-backed (no markup change required; only the JS that fills it changes — verified in Task 6).

---

## Key design decisions (locked)

- **History de-duplication + cap:** `record_search_history` deletes any existing row for the same user with an identical `query` + `filters_json`, then inserts a fresh row (so the most-recent timestamp wins and the entry floats to the top), then trims the user's history to the newest `HISTORY_CAP = 20` rows. This mirrors the old localStorage behaviour (`[q, ...others.filter(s=>s!==q)].slice(0,5)`) but server-side and filter-aware.
- **Empty queries are not recorded.** The old code only saved non-empty queries (`if (payload.query?.trim())`). We preserve that: `record_search_history` is a no-op when `query.strip()` is empty. Browsing-by-tag with an empty query is still a valid search — it just isn't added to history (matching today's UX).
- **Filters are stored as a single JSON object** with a fixed, stable key set so the frontend can round-trip them: `{"filetype","path","block_type","modified_from","modified_to","tags"}`. `tags` is a list; everything else is a string-or-null. The store treats the JSON as an opaque blob; only the frontend interprets it.
- **`saved_searches.name` is unique per user** (`UNIQUE(user_id, name)`), so re-saving under an existing name returns a 409 rather than silently duplicating. The frontend surfaces this as a toast.
- **All helpers take `user_id` first and scope every statement to it** — identical to the `set_tags`/`get_user_tags` pattern. There is no cross-user read path.
- **Deletion is ownership-checked at the SQL level:** `delete_saved_search(user_id, saved_id)` deletes `WHERE id=? AND user_id=?` and returns the affected row count; the route turns `0` into a 404. This is the same shape as the HA-key DELETE-by-id route but with per-user scoping.
- **IDs are integer autoincrement**, exposed to the API as integers (saved searches) — consistent with `documents.id`. History rows are never addressed individually (list + clear-all only), so their IDs stay internal.

---

## Task 1: Schema for `search_history` and `saved_searches`

**Files:**
- Modify: `document_search/index/sqlite_store.py` (extend the `executescript` block in `_init_schema`)
- Test: `tests/test_saved_searches_store.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_saved_searches_store.py`:

```python
from pathlib import Path
import pytest
from document_search.index.sqlite_store import SqliteStore


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "test.db")


def test_search_history_table_exists(store):
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='search_history'"
    ).fetchall()
    assert len(rows) == 1


def test_saved_searches_table_exists(store):
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='saved_searches'"
    ).fetchall()
    assert len(rows) == 1


def test_search_history_columns(store):
    cols = {c[1] for c in store.conn.execute("PRAGMA table_info(search_history)").fetchall()}
    expected = {"id", "user_id", "query", "filters_json", "created_at"}
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_saved_searches_columns(store):
    cols = {c[1] for c in store.conn.execute("PRAGMA table_info(saved_searches)").fetchall()}
    expected = {"id", "user_id", "name", "query", "filters_json", "created_at"}
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_saved_searches_unique_name_per_user(store):
    from datetime import UTC, datetime
    now = datetime.now(tz=UTC).isoformat()
    uid = store.create_user("alice", "alice-password")
    store.conn.execute(
        "INSERT INTO saved_searches(user_id, name, query, filters_json, created_at) "
        "VALUES(?,?,?,?,?)", (uid, "My search", "foo", "{}", now),
    )
    store.conn.commit()
    # Same name for the same user is rejected
    with pytest.raises(Exception):
        store.conn.execute(
            "INSERT INTO saved_searches(user_id, name, query, filters_json, created_at) "
            "VALUES(?,?,?,?,?)", (uid, "My search", "bar", "{}", now),
        )
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_saved_searches_store.py -v
```

Expected: `test_search_history_table_exists` and `test_saved_searches_table_exists` fail — tables don't exist yet.

- [ ] **Step 3: Add schema in `_init_schema`**

In `document_search/index/sqlite_store.py`, inside the `self.conn.executescript("""...""")` block in `_init_schema`, **append** the following just before the closing `"""` (i.e. immediately after the last `CREATE INDEX IF NOT EXISTS idx_jobs_owner ...` line at ~line 158):

```sql
CREATE TABLE IF NOT EXISTS search_history (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  query TEXT NOT NULL,
  filters_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS saved_searches (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  query TEXT NOT NULL,
  filters_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE(user_id, name),
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_search_history_user ON search_history(user_id, id);
CREATE INDEX IF NOT EXISTS idx_saved_searches_user ON saved_searches(user_id, id);
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_saved_searches_store.py -v
```

Expected: 5 passing.

- [ ] **Step 5: Full-suite check**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: all previously-passing tests still pass, plus the 5 new ones.

- [ ] **Step 6: Commit**

```powershell
git add document_search/index/sqlite_store.py tests/test_saved_searches_store.py
git commit -m "feat(search): add search_history and saved_searches tables"
```

---

## Task 2: `SqliteStore` history CRUD (record / list / clear)

**Files:**
- Modify: `document_search/index/sqlite_store.py` (add helper methods)
- Test: `tests/test_saved_searches_store.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_saved_searches_store.py`:

```python
def test_record_history_inserts_row(store):
    uid = store.create_user("alice", "alice-password")
    store.record_search_history(uid, "invoices", {"filetype": "pdf", "tags": ["finance"]})
    rows = store.list_search_history(uid)
    assert len(rows) == 1
    assert rows[0]["query"] == "invoices"
    assert rows[0]["filters"]["filetype"] == "pdf"
    assert rows[0]["filters"]["tags"] == ["finance"]


def test_record_history_skips_empty_query(store):
    uid = store.create_user("alice", "alice-password")
    store.record_search_history(uid, "   ", {})
    store.record_search_history(uid, "", {"tags": ["x"]})
    assert store.list_search_history(uid) == []


def test_record_history_dedupes_identical_query_and_filters(store):
    uid = store.create_user("alice", "alice-password")
    store.record_search_history(uid, "report", {"path": "/docs"})
    store.record_search_history(uid, "report", {"path": "/docs"})
    rows = store.list_search_history(uid)
    assert len(rows) == 1  # identical entry de-duplicated


def test_record_history_distinct_filters_kept_separately(store):
    uid = store.create_user("alice", "alice-password")
    store.record_search_history(uid, "report", {"path": "/a"})
    store.record_search_history(uid, "report", {"path": "/b"})
    rows = store.list_search_history(uid)
    assert len(rows) == 2  # same query, different filters → two entries


def test_record_history_floats_repeat_to_top(store):
    uid = store.create_user("alice", "alice-password")
    store.record_search_history(uid, "first", {})
    store.record_search_history(uid, "second", {})
    store.record_search_history(uid, "first", {})  # repeat → should move to top
    rows = store.list_search_history(uid)
    assert [r["query"] for r in rows] == ["first", "second"]


def test_record_history_caps_to_twenty(store):
    uid = store.create_user("alice", "alice-password")
    for i in range(25):
        store.record_search_history(uid, f"q{i}", {})
    rows = store.list_search_history(uid)
    assert len(rows) == 20
    # Newest first: q24 .. q5
    assert rows[0]["query"] == "q24"
    assert rows[-1]["query"] == "q5"


def test_history_is_scoped_per_user(store):
    alice = store.create_user("alice", "alice-password")
    bob = store.create_user("bob", "bob-password")
    store.record_search_history(alice, "alice-query", {})
    store.record_search_history(bob, "bob-query", {})
    alice_rows = store.list_search_history(alice)
    bob_rows = store.list_search_history(bob)
    assert [r["query"] for r in alice_rows] == ["alice-query"]
    assert [r["query"] for r in bob_rows] == ["bob-query"]


def test_clear_history_only_affects_caller(store):
    alice = store.create_user("alice", "alice-password")
    bob = store.create_user("bob", "bob-password")
    store.record_search_history(alice, "alice-query", {})
    store.record_search_history(bob, "bob-query", {})
    store.clear_search_history(alice)
    assert store.list_search_history(alice) == []
    assert len(store.list_search_history(bob)) == 1
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_saved_searches_store.py -v
```

Expected: `AttributeError: 'SqliteStore' object has no attribute 'record_search_history'`.

- [ ] **Step 3: Implement the history helpers**

In `document_search/index/sqlite_store.py`, add a module-level constant near the top of the file (just below the imports, ~line 10):

```python
HISTORY_CAP = 20
```

Then add these methods to `SqliteStore` (place them directly after `get_user_tags`, ~line 453, so they sit with the other per-user helpers). The `import json` is already at module scope? It is not — add `import json` to the top imports block (next to `import os`):

```python
    def record_search_history(self, user_id: int, query: str, filters: dict) -> None:
        """Record a search in the user's rolling history.

        No-op for empty queries (mirrors the old localStorage behaviour). The
        entry is de-duplicated on identical query+filters (re-running an
        identical search floats it back to the top), and the user's history is
        trimmed to the newest HISTORY_CAP rows.
        """
        if not query or not query.strip():
            return
        filters_json = json.dumps(filters or {}, sort_keys=True)
        now = datetime.now(tz=UTC).isoformat()
        self.conn.execute(
            "DELETE FROM search_history WHERE user_id=? AND query=? AND filters_json=?",
            (user_id, query, filters_json),
        )
        self.conn.execute(
            "INSERT INTO search_history(user_id, query, filters_json, created_at) VALUES(?,?,?,?)",
            (user_id, query, filters_json, now),
        )
        # Trim to the newest HISTORY_CAP rows for this user
        self.conn.execute(
            """
            DELETE FROM search_history
            WHERE user_id=? AND id NOT IN (
                SELECT id FROM search_history WHERE user_id=? ORDER BY id DESC LIMIT ?
            )
            """,
            (user_id, user_id, HISTORY_CAP),
        )
        self.conn.commit()

    def list_search_history(self, user_id: int, limit: int = HISTORY_CAP) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, query, filters_json, created_at FROM search_history "
            "WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        out = []
        for r in rows:
            try:
                filters = json.loads(r["filters_json"]) if r["filters_json"] else {}
            except (ValueError, TypeError):
                filters = {}
            out.append({
                "id": r["id"],
                "query": r["query"],
                "filters": filters,
                "created_at": r["created_at"],
            })
        return out

    def clear_search_history(self, user_id: int) -> int:
        cur = self.conn.execute("DELETE FROM search_history WHERE user_id=?", (user_id,))
        self.conn.commit()
        return cur.rowcount
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_saved_searches_store.py -v
```

Expected: 13 passing (5 from Task 1 + 8 new).

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: all green.

- [ ] **Step 6: Commit**

```powershell
git add document_search/index/sqlite_store.py tests/test_saved_searches_store.py
git commit -m "feat(search): SqliteStore search-history record/list/clear helpers"
```

---

## Task 3: `SqliteStore` saved-search CRUD (create / list / delete)

**Files:**
- Modify: `document_search/index/sqlite_store.py` (add helper methods)
- Test: `tests/test_saved_searches_store.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_saved_searches_store.py`:

```python
import sqlite3


def test_create_saved_search_returns_id_and_row(store):
    uid = store.create_user("alice", "alice-password")
    sid = store.create_saved_search(uid, "Finance PDFs", "invoices", {"filetype": "pdf"})
    assert isinstance(sid, int)
    rows = store.list_saved_searches(uid)
    assert len(rows) == 1
    assert rows[0]["id"] == sid
    assert rows[0]["name"] == "Finance PDFs"
    assert rows[0]["query"] == "invoices"
    assert rows[0]["filters"]["filetype"] == "pdf"


def test_create_saved_search_duplicate_name_raises(store):
    uid = store.create_user("alice", "alice-password")
    store.create_saved_search(uid, "Dup", "a", {})
    with pytest.raises(sqlite3.IntegrityError):
        store.create_saved_search(uid, "Dup", "b", {})


def test_same_name_allowed_for_different_users(store):
    alice = store.create_user("alice", "alice-password")
    bob = store.create_user("bob", "bob-password")
    a = store.create_saved_search(alice, "Shared name", "a", {})
    b = store.create_saved_search(bob, "Shared name", "b", {})
    assert a != b
    assert len(store.list_saved_searches(alice)) == 1
    assert len(store.list_saved_searches(bob)) == 1


def test_list_saved_searches_scoped_per_user(store):
    alice = store.create_user("alice", "alice-password")
    bob = store.create_user("bob", "bob-password")
    store.create_saved_search(alice, "A1", "qa", {})
    store.create_saved_search(bob, "B1", "qb", {})
    assert [r["name"] for r in store.list_saved_searches(alice)] == ["A1"]
    assert [r["name"] for r in store.list_saved_searches(bob)] == ["B1"]


def test_delete_saved_search_removes_only_callers_row(store):
    alice = store.create_user("alice", "alice-password")
    bob = store.create_user("bob", "bob-password")
    a_sid = store.create_saved_search(alice, "A1", "qa", {})
    b_sid = store.create_saved_search(bob, "B1", "qb", {})
    # Alice cannot delete Bob's saved search
    assert store.delete_saved_search(alice, b_sid) == 0
    assert len(store.list_saved_searches(bob)) == 1
    # Alice can delete her own
    assert store.delete_saved_search(alice, a_sid) == 1
    assert store.list_saved_searches(alice) == []
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_saved_searches_store.py -v
```

Expected: `AttributeError: 'SqliteStore' object has no attribute 'create_saved_search'`.

- [ ] **Step 3: Implement the saved-search helpers**

In `document_search/index/sqlite_store.py`, add these methods directly after `clear_search_history` (added in Task 2):

```python
    def create_saved_search(self, user_id: int, name: str, query: str, filters: dict) -> int:
        """Create a named saved search for the user.

        Raises sqlite3.IntegrityError if the user already has a saved search
        with this name (UNIQUE(user_id, name)).
        """
        now = datetime.now(tz=UTC).isoformat()
        cur = self.conn.execute(
            "INSERT INTO saved_searches(user_id, name, query, filters_json, created_at) "
            "VALUES(?,?,?,?,?)",
            (user_id, name, query, json.dumps(filters or {}, sort_keys=True), now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_saved_searches(self, user_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, name, query, filters_json, created_at FROM saved_searches "
            "WHERE user_id=? ORDER BY name COLLATE NOCASE",
            (user_id,),
        ).fetchall()
        out = []
        for r in rows:
            try:
                filters = json.loads(r["filters_json"]) if r["filters_json"] else {}
            except (ValueError, TypeError):
                filters = {}
            out.append({
                "id": r["id"],
                "name": r["name"],
                "query": r["query"],
                "filters": filters,
                "created_at": r["created_at"],
            })
        return out

    def delete_saved_search(self, user_id: int, saved_id: int) -> int:
        """Delete a saved search. Scoped to the caller — deleting another
        user's row affects zero rows. Returns the affected row count."""
        cur = self.conn.execute(
            "DELETE FROM saved_searches WHERE id=? AND user_id=?",
            (saved_id, user_id),
        )
        self.conn.commit()
        return cur.rowcount
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_saved_searches_store.py -v
```

Expected: 18 passing (13 + 5 new).

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: all green.

- [ ] **Step 6: Commit**

```powershell
git add document_search/index/sqlite_store.py tests/test_saved_searches_store.py
git commit -m "feat(search): SqliteStore saved-search create/list/delete helpers"
```

---

## Task 4: History endpoints + auto-record on `/api/search`

**Files:**
- Modify: `document_search/app.py` — add `GET /api/search/history`, `DELETE /api/search/history`; auto-record history inside `api_search`.
- Test: `tests/test_app_saved_searches.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_app_saved_searches.py`:

```python
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from document_search.app import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "index.db"))
    return TestClient(app)


def _token(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _headers(client, **kw):
    return {"X-Auth-Token": _token(client, **kw)}


def _make_second_user(client, admin_headers, username="bob", password="bob-password"):
    r = client.post(
        "/api/users",
        headers=admin_headers,
        json={"username": username, "password": password, "role": "user"},
    )
    assert r.status_code in (200, 201), r.text


def test_history_starts_empty(client):
    h = _headers(client)
    r = client.get("/api/search/history", headers=h)
    assert r.status_code == 200
    assert r.json() == []


def test_search_auto_records_history(client):
    h = _headers(client)
    r = client.post("/api/search", headers=h, json={"query": "report", "tags": ["x"]})
    assert r.status_code == 200, r.text
    hist = client.get("/api/search/history", headers=h).json()
    assert len(hist) == 1
    assert hist[0]["query"] == "report"
    assert hist[0]["filters"]["tags"] == ["x"]


def test_search_empty_query_not_recorded(client):
    h = _headers(client)
    r = client.post("/api/search", headers=h, json={"query": "   "})
    assert r.status_code == 200, r.text
    assert client.get("/api/search/history", headers=h).json() == []


def test_clear_history(client):
    h = _headers(client)
    client.post("/api/search", headers=h, json={"query": "report"})
    assert len(client.get("/api/search/history", headers=h).json()) == 1
    r = client.delete("/api/search/history", headers=h)
    assert r.status_code == 200
    assert client.get("/api/search/history", headers=h).json() == []


def test_history_is_per_user(client):
    admin_h = _headers(client)
    _make_second_user(client, admin_h)
    bob_h = {"X-Auth-Token": _token(client, "bob", "bob-password")}
    client.post("/api/search", headers=admin_h, json={"query": "admin-query"})
    client.post("/api/search", headers=bob_h, json={"query": "bob-query"})
    admin_hist = client.get("/api/search/history", headers=admin_h).json()
    bob_hist = client.get("/api/search/history", headers=bob_h).json()
    assert [r["query"] for r in admin_hist] == ["admin-query"]
    assert [r["query"] for r in bob_hist] == ["bob-query"]


def test_history_requires_auth(client):
    r = client.get("/api/search/history")
    assert r.status_code == 401
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_saved_searches.py -v
```

Expected: 404 on `/api/search/history` (route doesn't exist) and history-record assertions fail.

- [ ] **Step 3: Add the history routes and wire auto-record into `api_search`**

In `document_search/app.py`, add the route definitions immediately **after** the `api_search` function (it ends at the `return output` line, ~line 1621, with `@app.get("/api/status")` following). Insert:

```python
    @app.get("/api/search/history")
    def api_search_history(x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        return store().list_search_history(user_id)

    @app.delete("/api/search/history")
    def api_clear_search_history(x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        removed = store().clear_search_history(user_id)
        return {"status": "ok", "removed": removed}
```

Then wire auto-record into the existing `api_search` handler. Locate the `return output` line at the end of `api_search` (~line 1621). **Immediately before** it, insert the record call (the `db` and `user_id` locals are already in scope):

```python
        db.record_search_history(
            user_id,
            req.query,
            {
                "filetype": req.filetype,
                "path": req.path,
                "block_type": req.block_type,
                "modified_from": req.modified_from,
                "modified_to": req.modified_to,
                "tags": req.tags,
            },
        )
        return output
```

(Replace the bare `return output` with the two statements above. The `record_search_history` helper is a no-op for empty queries, so no extra guard is needed.)

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_saved_searches.py -v
```

Expected: the 6 history tests pass.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: all green. Note: `tests/test_app_search.py` exercises `/api/search` — confirm those still pass (auto-record runs on every call but is transparent to the response shape).

- [ ] **Step 6: Commit**

```powershell
git add document_search/app.py tests/test_app_saved_searches.py
git commit -m "feat(search): server-side history endpoints + auto-record on /api/search"
```

---

## Task 5: Saved-search endpoints (list / create / delete)

**Files:**
- Modify: `document_search/app.py` — add `SavedSearchRequest` model; add `GET /api/search/saved`, `POST /api/search/saved`, `DELETE /api/search/saved/{saved_id}`.
- Test: `tests/test_app_saved_searches.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_app_saved_searches.py`:

```python
def test_saved_starts_empty(client):
    h = _headers(client)
    r = client.get("/api/search/saved", headers=h)
    assert r.status_code == 200
    assert r.json() == []


def test_create_and_list_saved_search(client):
    h = _headers(client)
    payload = {
        "name": "Finance PDFs",
        "query": "invoices",
        "filters": {"filetype": "pdf", "tags": ["finance"]},
    }
    r = client.post("/api/search/saved", headers=h, json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body["id"], int)
    assert body["name"] == "Finance PDFs"

    listed = client.get("/api/search/saved", headers=h).json()
    assert len(listed) == 1
    assert listed[0]["name"] == "Finance PDFs"
    assert listed[0]["query"] == "invoices"
    assert listed[0]["filters"]["filetype"] == "pdf"
    assert listed[0]["filters"]["tags"] == ["finance"]


def test_create_saved_search_rejects_blank_name(client):
    h = _headers(client)
    r = client.post("/api/search/saved", headers=h, json={"name": "   ", "query": "x", "filters": {}})
    assert r.status_code == 400


def test_create_saved_search_duplicate_name_conflicts(client):
    h = _headers(client)
    body = {"name": "Dup", "query": "a", "filters": {}}
    assert client.post("/api/search/saved", headers=h, json=body).status_code == 200
    r = client.post("/api/search/saved", headers=h, json={"name": "Dup", "query": "b", "filters": {}})
    assert r.status_code == 409


def test_delete_saved_search(client):
    h = _headers(client)
    sid = client.post(
        "/api/search/saved", headers=h, json={"name": "Temp", "query": "a", "filters": {}}
    ).json()["id"]
    r = client.delete(f"/api/search/saved/{sid}", headers=h)
    assert r.status_code == 200
    assert client.get("/api/search/saved", headers=h).json() == []


def test_delete_saved_search_unknown_id_404(client):
    h = _headers(client)
    r = client.delete("/api/search/saved/999999", headers=h)
    assert r.status_code == 404


def test_saved_search_is_per_user_and_delete_isolated(client):
    admin_h = _headers(client)
    _make_second_user(client, admin_h)
    bob_h = {"X-Auth-Token": _token(client, "bob", "bob-password")}

    admin_sid = client.post(
        "/api/search/saved", headers=admin_h, json={"name": "A", "query": "qa", "filters": {}}
    ).json()["id"]
    client.post("/api/search/saved", headers=bob_h, json={"name": "B", "query": "qb", "filters": {}})

    # Bob's list never shows admin's saved search
    assert [s["name"] for s in client.get("/api/search/saved", headers=bob_h).json()] == ["B"]
    # Bob cannot delete admin's saved search → 404, and admin's row survives
    r = client.delete(f"/api/search/saved/{admin_sid}", headers=bob_h)
    assert r.status_code == 404
    assert [s["name"] for s in client.get("/api/search/saved", headers=admin_h).json()] == ["A"]


def test_saved_requires_auth(client):
    assert client.get("/api/search/saved").status_code == 401
    assert client.post("/api/search/saved", json={"name": "x", "query": "y"}).status_code == 401
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_saved_searches.py -v
```

Expected: 404 on `/api/search/saved` (routes don't exist).

- [ ] **Step 3: Add the model and routes in `app.py`**

First, add the Pydantic model next to the other request models. Place it directly after `TagsRequest` (~line 205):

```python
class SavedSearchRequest(BaseModel):
    name: str
    query: str = ""
    filters: dict = Field(default_factory=dict)
```

Then add the three routes immediately after the `api_clear_search_history` route added in Task 4:

```python
    @app.get("/api/search/saved")
    def api_list_saved_searches(x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        return store().list_saved_searches(user_id)

    @app.post("/api/search/saved")
    def api_create_saved_search(req: SavedSearchRequest, x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        name = req.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Name must not be empty.")
        try:
            saved_id = store().create_saved_search(user_id, name, req.query, req.filters)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail=f"A saved search named '{name}' already exists.")
        return {
            "id": saved_id,
            "name": name,
            "query": req.query,
            "filters": req.filters,
        }

    @app.delete("/api/search/saved/{saved_id}")
    def api_delete_saved_search(saved_id: int, x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        removed = store().delete_saved_search(user_id, saved_id)
        if removed == 0:
            raise HTTPException(status_code=404, detail="Saved search not found")
        return {"status": "deleted", "id": saved_id}
```

(`sqlite3` is already imported at the top of `app.py` — verified by the `except sqlite3.OperationalError` in `api_search`.)

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_saved_searches.py -v
```

Expected: all 14 tests pass (6 history + 8 saved).

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: all green.

- [ ] **Step 6: Commit**

```powershell
git add document_search/app.py tests/test_app_saved_searches.py
git commit -m "feat(search): saved-search list/create/delete endpoints (per-user)"
```

---

## Task 6: Frontend — server-backed history + filter round-trip

**Files:**
- Modify: `document_search/web/static/app.js` — replace `saveRecentSearch`/`renderRecentSearches`; add `captureFilters`/`applyFilters` helpers; record/restore the full filter set.

There are no automated tests for the JS (the project has no JS test harness). This task is verified manually in Task 8. Keep the diffs surgical and follow the existing patterns (`api(...)`, `showToast(...)`, `escHtml(...)`).

- [ ] **Step 1: Replace the recent-searches functions**

In `document_search/web/static/app.js`, replace the entire `// ── Recent searches ──` block (the `saveRecentSearch` and `renderRecentSearches` functions, ~lines 208–230) with:

```javascript
// ── Search history (server-backed) ─────────────────────────────────
function captureFilters() {
  // Snapshot the current filter form into the same shape the backend stores.
  return {
    filetype: chipFiletype?.values().join(',') || null,
    path: document.getElementById('pathFilter')?.value || null,
    block_type: document.getElementById('blockType')?.value || null,
    modified_from: document.getElementById('modifiedFrom')?.value || null,
    modified_to: document.getElementById('modifiedTo')?.value || null,
    tags: chipTagFilter?.values() ?? [],
  };
}

function applyFilters(query, filters) {
  // Restore a saved/history search into the form, then re-run it.
  filters = filters || {};
  const q = document.getElementById('query');
  if (q) q.value = query ?? '';
  chipFiletype?.setValues((filters.filetype || '').split(',').map(s => s.trim()).filter(Boolean));
  chipTagFilter?.setValues(Array.isArray(filters.tags) ? filters.tags : []);
  const setVal = (id, v) => { const el = document.getElementById(id); if (el) el.value = v ?? ''; };
  setVal('pathFilter', filters.path);
  setVal('blockType', filters.block_type);
  setVal('modifiedFrom', filters.modified_from);
  setVal('modifiedTo', filters.modified_to);
  // If any filter is set, make sure the filter panel is visible.
  const anyFilter = (filters.filetype || filters.path || filters.block_type ||
    filters.modified_from || filters.modified_to || (filters.tags || []).length);
  const filterBody = document.getElementById('filterBody');
  if (anyFilter && filterBody?.classList.contains('hidden')) toggleFilters();
  runSearch();
}

async function renderRecentSearches() {
  const el = document.getElementById('recentSearches');
  if (!el || !token) return;
  let searches = [];
  try {
    searches = await api('/api/search/history');
  } catch (_) { return; }
  if (!searches.length) {
    el.innerHTML = '<p class="muted" style="font-size:.82rem;">No recent searches yet.</p>';
    return;
  }
  el.innerHTML = `<div class="recent-list">${searches.map((s, i) => `
    <a href="#" class="recent-item" data-hist="${i}">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="13" height="13"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
      ${escHtml(s.query)}
    </a>`).join('')}</div>`;
  el.querySelectorAll('[data-hist]').forEach(a => {
    a.addEventListener('click', ev => {
      ev.preventDefault();
      const item = searches[Number(a.dataset.hist)];
      // On the dashboard there is no search form — navigate to /search with the query.
      if (document.body?.dataset?.page !== 'search') {
        location.href = `/search?q=${encodeURIComponent(item.query)}`;
        return;
      }
      applyFilters(item.query, item.filters);
    });
  });
}
```

Note: history is now recorded server-side by `/api/search`, so the old client-side `saveRecentSearch(...)` call in `runSearch` must be removed (Step 2). The dashboard (`index.html`) keeps its `#recentSearches` card and now renders server data; on the dashboard there is no search form, so clicking an entry navigates to `/search?q=...` (matching the old behaviour), where the URL `q` param re-runs the search.

- [ ] **Step 2: Remove the now-dead client-side record call and refresh history after each search**

In `runSearch` (~line 254), find:

```javascript
    const data = await api('/api/search', 'POST', payload);
    if (payload.query?.trim()) saveRecentSearch(payload.query);
```

Replace those two lines with:

```javascript
    const data = await api('/api/search', 'POST', payload);
    renderRecentSearches();  // history is recorded server-side by /api/search
```

- [ ] **Step 3: Make `bootstrap` await the (now async) `renderRecentSearches`**

In `bootstrap` (~line 1716), the call is currently `renderRecentSearches();` near the top. It runs before `token`/chips may be ready. Move/guard it:

- Leave the early `renderRecentSearches();` call only for the dashboard (it tolerates missing chips because the dashboard has no filter form). It is now `async` but fire-and-forget is fine here.
- It already returns early when `!token`. After login on the search page, the existing `await loadFilterOptions(); await loadTagCloud();` block runs; **append** a `renderRecentSearches();` call there so the search page's recent list (if present) is populated once chips exist.

Concretely, inside the `if (document.body?.dataset?.page === 'search') { ... }` block in `bootstrap` (~line 1730), after the `await loadTagCloud();` line and before the `const q = new URLSearchParams...` line, add:

```javascript
      renderSavedSearches();
```

(`renderSavedSearches` is added in Task 7; adding the call here now keeps the two tasks' edits in the same region. If executing Task 6 in isolation, you may temporarily stub `function renderSavedSearches(){}` and replace it in Task 7.)

Also update the `login()` flow's search-page block (~line 180) the same way: after `await loadTagCloud();` is **not** present there — `login()` only sets up chips and calls `loadFilterOptions()`/`loadTagCloud()` is absent. Add `renderRecentSearches();` after `await loadTagCloud();` inside `bootstrap` only (login() already falls through to a reload-free path; the dashboard `renderRecentSearches()` at the top of `bootstrap` covers the dashboard). Keep this edit minimal — the single `renderRecentSearches();` at the top of `bootstrap` plus the one inside `runSearch` are sufficient for correctness.

- [ ] **Step 4: Manual smoke (deferred to Task 8)**

No commit yet — Task 7 adds the saved-searches UI in the same files. Commit at the end of Task 7.

---

## Task 7: Frontend — "Save this search" + saved-searches list

**Files:**
- Modify: `document_search/web/templates/search.html` — add a "Save this search" button + a saved-searches card.
- Modify: `document_search/web/static/app.js` — add `saveCurrentSearch()`, `renderSavedSearches()`, `deleteSavedSearch()`.

- [ ] **Step 1: Add the UI to `search.html`**

In `document_search/web/templates/search.html`, add a "Save this search" button to the search-row button group. Locate the `<button class="btn btn-p" onclick="runSearch()">` block (~lines 110–113) and insert, **after** its closing `</button>`:

```html
                <button class="btn btn-g" onclick="saveCurrentSearch()" title="Save this query and filters">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 01-2-2V5a2 2 0 012-2h11l5 5v11a2 2 0 01-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
                  Save
                </button>
```

Then add a "Saved searches" card. Insert it **before** the `<!-- Tag cloud -->` card (~line 173):

```html
        <!-- Saved searches -->
        <div id="savedSearchCard" class="card hidden" style="margin-bottom:.75rem;">
          <div class="card-body" style="padding:.75rem 1rem;">
            <div style="display:flex;align-items:center;gap:.5rem;margin-bottom:.5rem;">
              <span style="font-size:.75rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--txt-3);">Saved searches</span>
            </div>
            <div id="savedSearchList" style="display:flex;gap:.4rem;flex-wrap:wrap;"></div>
          </div>
        </div>
```

- [ ] **Step 2: Add the JS handlers**

In `document_search/web/static/app.js`, add these functions directly after the `renderRecentSearches` function added in Task 6 (in the "Search history" section):

```javascript
// ── Saved searches ─────────────────────────────────────────────────
async function saveCurrentSearch() {
  const query = document.getElementById('query')?.value ?? '';
  const filters = captureFilters();
  const name = prompt('Name this saved search:', query.trim() || 'My search');
  if (name === null) return;            // user cancelled
  if (!name.trim()) { showToast('Name must not be empty', 'err'); return; }
  try {
    await api('/api/search/saved', 'POST', { name: name.trim(), query, filters });
    showToast('Search saved', 'ok');
    renderSavedSearches();
  } catch (e) {
    showToast(e.message || 'Could not save search', 'err');
  }
}

async function renderSavedSearches() {
  const card = document.getElementById('savedSearchCard');
  const list = document.getElementById('savedSearchList');
  if (!card || !list || !token) return;
  let saved = [];
  try {
    saved = await api('/api/search/saved');
  } catch (_) { return; }
  if (!saved.length) { card.classList.add('hidden'); return; }
  card.classList.remove('hidden');
  list.replaceChildren();
  saved.forEach(s => {
    const chip = document.createElement('span');
    chip.className = 'tag-chip';
    chip.title = s.query || '(empty query)';

    const run = document.createElement('button');
    run.style.cssText = 'background:none;border:none;cursor:pointer;color:inherit;font:inherit;padding:0;';
    run.textContent = s.name;
    run.addEventListener('click', () => applyFilters(s.query, s.filters));

    const del = document.createElement('span');
    del.className = 'chip-x';
    del.textContent = '×';
    del.title = 'Delete';
    del.addEventListener('click', ev => { ev.stopPropagation(); deleteSavedSearch(s.id, s.name); });

    chip.appendChild(run);
    chip.appendChild(del);
    list.appendChild(chip);
  });
}

async function deleteSavedSearch(savedId, name) {
  try {
    await api(`/api/search/saved/${savedId}`, 'DELETE');
    showToast(`Deleted "${name}"`, 'ok');
    renderSavedSearches();
  } catch (e) {
    showToast(e.message || 'Could not delete', 'err');
  }
}
```

- [ ] **Step 3: Ensure `renderSavedSearches` runs on the search page**

If you stubbed `renderSavedSearches` in Task 6 Step 3, that stub is now the real function — confirm the call you added inside the `bootstrap` search-page block (after `await loadTagCloud();`) is present:

```javascript
      renderSavedSearches();
```

- [ ] **Step 4: Verify no stale references**

```powershell
$env:PYTHONPATH = "."; Select-String -Path document_search\web\static\app.js -Pattern "saveRecentSearch|seekr_recent"
```

Expected: no matches (the old localStorage function and key are fully removed).

- [ ] **Step 5: Commit (frontend Tasks 6 + 7 together)**

```powershell
git add document_search/web/static/app.js document_search/web/templates/search.html
git commit -m "feat(search): server-backed history UI + saved-searches list with filter restore"
```

---

## Task 8: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Full test suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: green, zero failures. New tests: 18 in `test_saved_searches_store.py` + 14 in `test_app_saved_searches.py`.

- [ ] **Step 2: Endpoint smoke test via TestClient**

```powershell
$env:PYTHONPATH = "."; python -c "
from fastapi.testclient import TestClient
from document_search.app import create_app
import tempfile, pathlib
tmp = pathlib.Path(tempfile.mkdtemp())
app = create_app(str(tmp / 'smoke.db'))
with TestClient(app) as c:
    tok = c.post('/api/login', json={'username':'admin','password':'admin'}).json()['token']
    H = {'X-Auth-Token': tok}
    c.post('/api/search', headers=H, json={'query':'alpha','tags':['x']})
    c.post('/api/search', headers=H, json={'query':'beta','filetype':'pdf'})
    hist = c.get('/api/search/history', headers=H).json()
    print('history =', [(h['query'], h['filters']) for h in hist])
    assert [h['query'] for h in hist] == ['beta','alpha']
    s = c.post('/api/search/saved', headers=H, json={'name':'My PDFs','query':'beta','filters':{'filetype':'pdf'}}).json()
    print('saved id =', s['id'])
    saved = c.get('/api/search/saved', headers=H).json()
    assert saved[0]['name'] == 'My PDFs' and saved[0]['filters']['filetype'] == 'pdf'
    assert c.delete(f\"/api/search/saved/{s['id']}\", headers=H).status_code == 200
    assert c.get('/api/search/saved', headers=H).json() == []
    assert c.delete('/api/search/history', headers=H).status_code == 200
    assert c.get('/api/search/history', headers=H).json() == []
print('OK')
"
```

Expected: prints history newest-first, a saved id, then `OK`.

- [ ] **Step 3: Manual UI check (optional, if a browser is available)**

```powershell
$env:PYTHONPATH = "."; uvicorn document_search.app:app --port 8080
```

Then in a browser at `http://localhost:8080/search`: sign in (admin/admin), run a search with a tag filter, confirm it appears under "Recent Searches" on the dashboard and that clicking it restores query + filters; click "Save", give it a name, confirm it appears in the "Saved searches" card; click the saved chip to restore + re-run; click the `×` to delete it (toast appears, chip disappears).

- [ ] **Step 4: No final commit needed** (verification only).

---

## Definition of Done

- [ ] `pytest -q` is green on a clean clone.
- [ ] `search_history` and `saved_searches` tables exist with the documented columns; `saved_searches` enforces `UNIQUE(user_id, name)`.
- [ ] `SqliteStore` exposes `record_search_history`, `list_search_history`, `clear_search_history`, `create_saved_search`, `list_saved_searches`, `delete_saved_search` — every one scoped by `user_id`.
- [ ] `POST /api/search` auto-records non-empty queries into the caller's rolling history (cap 20, de-duplicated on query+filters).
- [ ] `GET/DELETE /api/search/history` and `GET/POST /api/search/saved` + `DELETE /api/search/saved/{id}` exist, all `require_user`, all scoped to the caller (cross-user reads/deletes are impossible; deleting another user's saved search returns 404).
- [ ] Duplicate saved-search names return 409; blank names return 400.
- [ ] Frontend reads recent searches from `GET /api/search/history` (no `localStorage['seekr_recent']`, no `saveRecentSearch`); clicking a history or saved entry restores the full query + filter set and re-runs the search.
- [ ] "Save this search" stores the current query + filters with a user-supplied name, with toast feedback; saved searches can be deleted from the list.
- [ ] Two-user isolation verified by tests in `test_app_saved_searches.py`.

---

## Notes for the executing agent

- **Why auto-record in `api_search` instead of a separate endpoint:** the old frontend recorded history as a side-effect of running a search (`if (payload.query?.trim()) saveRecentSearch(...)`). Recording server-side inside `api_search` preserves that exact UX with one fewer round-trip and guarantees history reflects what was actually searched. The `db` and `user_id` locals are already in scope at the `return output` line — no signature change.
- **Why store filters as a single opaque JSON blob:** the backend never needs to query *inside* the filter set; it only needs to round-trip it to the frontend for restore. A blob keeps the schema stable if filter fields are added later (the frontend's `captureFilters`/`applyFilters` are the single source of truth for the key set).
- **Why `record_search_history` deletes-then-inserts rather than `UPDATE ... created_at`:** insert-after-delete gives the repeated entry a fresh autoincrement `id`, so `ORDER BY id DESC` floats it to the top without a separate timestamp comparison, and the cap-trim (`id NOT IN (... LIMIT 20)`) works on the same key. This mirrors the old `[q, ...filter(s!==q)]` reordering semantics exactly.
- **Why `delete_saved_search` returns a row count instead of raising:** the route maps `0 → 404`, which uniformly handles both "doesn't exist" and "belongs to another user" without leaking which case occurred — the same privacy property the job-queue plan used for `owner_user_id` 404s.
- **Why blank-name validation is in the route, not the model:** Pydantic `str` allows whitespace; trimming + the empty check belong with the request handler so the 400 message is explicit (`"Name must not be empty."`), consistent with how `api_ha_create_key` validates `path_filter`.
- **Frontend edit ordering caution:** Tasks 6 and 7 both touch `app.js` in the same region (the recent/saved section and `bootstrap`). If executing them as separate sessions, the `renderSavedSearches()` call added in Task 6 Step 3 references a function defined in Task 7 — stub it (`function renderSavedSearches(){}`) at the end of Task 6 so the page doesn't throw a `ReferenceError`, then replace the stub with the real implementation in Task 7. Both are committed together at the end of Task 7.
- **`index.html` needs no markup change:** its `#recentSearches` card already exists; only the JS that fills it changed (from localStorage to `GET /api/search/history`). On the dashboard there is no search form, so a clicked history entry navigates to `/search?q=...` (the search page's existing URL-`q` handling re-runs it) — preserving the pre-existing dashboard behaviour.
- **Legacy localStorage data is intentionally dropped:** there is no migration of `localStorage['seekr_recent']` into the DB. A user's server-side history simply starts empty; this is acceptable for a per-user convenience feature and avoids a brittle one-time client migration.
