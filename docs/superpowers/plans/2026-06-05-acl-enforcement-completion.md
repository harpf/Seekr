# ACL Enforcement Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the half-built ACL layer (docs/ROADMAP.md P1 → "Complete ACL enforcement"). The foundation (`principals`, `user_groups`, `document_acl`, `documents.owner_principal_id`, `users.principal_id`, `_backfill_acl`, and `acl_service.visible_document_ids_subquery`) is landed and enforced **only** in `search_service.search` and `_browse_all`. This plan (a) sets `owner_principal_id` on ingest — uploads owned by the uploader, crawled docs owned by a configurable default (default = unset → falls back to the `public` group, preserving current behaviour) — and (b) applies the ACL filter (or an explicit owner-or-admin check) to **every other** document-returning or document-counting HTTP endpoint.

**Architecture:** No new tables. One new read-helper on the central `acl_service` (`can_read_document_subquery`) plus two new `SqliteStore` helpers (`get_user_principal_id`, `user_can_read_document`) used by single-document endpoints. `upsert_document` gains an optional `owner_principal_id` keyword that is written into the existing `documents.owner_principal_id` column. Every listing/count endpoint embeds `visible_document_ids_subquery(user_id)` exactly as `search_service` already does — the subquery stays the single source of truth. HA and CLI keep `bypass_acl=True`; admins go through ACL except for the existing management actions (mark/tags/reindex), which keep their current owner-or-admin semantics via the new `user_can_read_document` check.

**Tech Stack:** Python 3.11, SQLite (FTS5), FastAPI, pytest. No new third-party dependencies.

**Scope boundaries (out of scope for this plan, picked up by later plans):**
- No ACL **write** API / UI (`/api/acl/*`, Config → Access tab) — that is the `[L]` ROADMAP item; this plan only consumes `read` ACLs.
- No `write`-permission enforcement — only `read` is queried, matching the existing `acl_service` contract.
- No Nextcloud/WebDAV connector. Crawl ingestion gets a single configurable default owner principal; per-source owner mapping is a later connector plan.
- `bypass_acl=True` stays exactly where it is today (CLI `cmd_search`, `_ha_search_impl`). This plan adds **no** new bypass call sites.

---

## File Structure

**Modify:**
- `document_search/services/acl_service.py` — add `can_read_document_subquery(user_id, document_id)` (single-doc visibility check reusing the same rules).
- `document_search/index/sqlite_store.py` — add `get_user_principal_id`, `user_can_read_document`; add optional `owner_principal_id` kwarg to `upsert_document` and write it into the insert/upsert.
- `document_search/app.py` — set owner on `/api/upload` and on the crawl handler; ACL-filter `/api/status`, `/api/ha/status`, `/api/folders` (no-op: filesystem, documented), `/api/source-folders` (no-op: filesystem, documented), and the AI worker sampling SQL (`ai_suggest_structure`, `ai_reorganize`); add a read check to `/api/files/open`, `/api/documents/mark`, `/api/documents/tags`.

**Create:**
- `tests/test_acl_enforcement.py` — integration tests: two users, docs with differing owners/ACLs, assert each endpoint only returns/permits visible docs.

**Test fixtures touched:**
- `tests/test_app_search.py` — verify still green (admin user is `public`-group member + every doc gets `public` read on ingest, so behaviour is preserved).

---

## Task 1: `owner_principal_id` on ingest

**Files:**
- Modify: `document_search/index/sqlite_store.py:248-295` (`upsert_document`), and add `get_user_principal_id` helper after `get_user_by_id` (~line 320).
- Test: `tests/test_acl_enforcement.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_acl_enforcement.py`:

```python
from datetime import UTC, datetime
from pathlib import Path

import pytest

from document_search.index.sqlite_store import SqliteStore
from document_search.models import FileFingerprint, ExtractionResult, ContentBlock


def _fp(path: Path) -> FileFingerprint:
    return FileFingerprint(
        path=path,
        file_size=10,
        modified_at=datetime.now(tz=UTC),
        sha256="deadbeef",
    )


def _ext(path: Path, text: str = "hello world") -> ExtractionResult:
    return ExtractionResult(
        file_path=path,
        status="ok",
        document_metadata={},
        blocks=[
            ContentBlock(
                block_type="paragraph",
                block_number=1,
                text=text,
                extractor="txt",
                metadata={},
            )
        ],
        error_message=None,
    )


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "test.db")


def test_get_user_principal_id(store):
    uid = store.create_user("alice", "pw")
    pid = store.get_user_principal_id(uid)
    assert pid is not None
    row = store.conn.execute(
        "SELECT type, external_id FROM principals WHERE id=?", (pid,)
    ).fetchone()
    assert row["type"] == "user"
    assert row["external_id"] == "alice"


def test_get_user_principal_id_unknown_user(store):
    assert store.get_user_principal_id(99999) is None


def test_upsert_sets_owner_principal_id(store, tmp_path):
    uid = store.create_user("alice", "pw")
    alice_pid = store.get_user_principal_id(uid)
    doc_path = tmp_path / "owned.txt"
    doc_path.write_text("hello world", encoding="utf-8")
    doc_id = store.upsert_document(_fp(doc_path), _ext(doc_path), owner_principal_id=alice_pid)
    row = store.conn.execute(
        "SELECT owner_principal_id FROM documents WHERE id=?", (doc_id,)
    ).fetchone()
    assert row["owner_principal_id"] == alice_pid


def test_upsert_without_owner_leaves_null_and_public(store, tmp_path):
    """Crawled docs (no owner) keep owner_principal_id NULL and still get public read."""
    doc_path = tmp_path / "crawled.txt"
    doc_path.write_text("hello world", encoding="utf-8")
    doc_id = store.upsert_document(_fp(doc_path), _ext(doc_path))
    row = store.conn.execute(
        "SELECT owner_principal_id FROM documents WHERE id=?", (doc_id,)
    ).fetchone()
    assert row["owner_principal_id"] is None
    public_id = store.conn.execute(
        "SELECT id FROM principals WHERE type='group' AND external_id='public'"
    ).fetchone()["id"]
    acl = store.conn.execute(
        "SELECT 1 FROM document_acl WHERE document_id=? AND principal_id=? AND permission='read'",
        (doc_id, public_id),
    ).fetchone()
    assert acl is not None
```

> Before running: confirm the import names. Read `document_search/models.py` and adjust `FileFingerprint`/`ExtractionResult`/`ContentBlock` construction to the real dataclass fields if they differ (the existing `tests/test_search_acl.py` inserts rows by raw SQL instead — if the model constructors are awkward, copy that raw-SQL insert style and drop `_fp`/`_ext`).

- [ ] **Step 2: Run tests, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_acl_enforcement.py -v
```
Expected: FAIL — `get_user_principal_id` does not exist and `upsert_document` rejects the `owner_principal_id` keyword.

- [ ] **Step 3: Add `get_user_principal_id` to `SqliteStore`**

In `document_search/index/sqlite_store.py`, directly after `get_user_by_id` (ends at line 320), add:

```python
    def get_user_principal_id(self, user_id: int) -> int | None:
        row = self.conn.execute(
            "SELECT principal_id FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if not row:
            return None
        return row["principal_id"]
```

- [ ] **Step 4: Add the `owner_principal_id` kwarg to `upsert_document`**

In `document_search/index/sqlite_store.py`, change the signature at line 248 from:

```python
    def upsert_document(self, fp: FileFingerprint, ext: ExtractionResult) -> int:
```

to:

```python
    def upsert_document(
        self, fp: FileFingerprint, ext: ExtractionResult, owner_principal_id: int | None = None
    ) -> int:
```

Then replace the INSERT statement (lines 253-266) so the new column is written. Change:

```python
        self.conn.execute(
            """
            INSERT INTO documents(path, filename, extension, file_size, modified_at, sha256, indexed_at, status, error_message, page_count, slide_count, metadata_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
              filename=excluded.filename, extension=excluded.extension, file_size=excluded.file_size, modified_at=excluded.modified_at,
              sha256=excluded.sha256, indexed_at=excluded.indexed_at, status=excluded.status, error_message=excluded.error_message,
              page_count=excluded.page_count, slide_count=excluded.slide_count, metadata_json=excluded.metadata_json
            """,
            (
                str(fp.path), fp.path.name, fp.path.suffix.lower(), fp.file_size, fp.modified_at.isoformat(), fp.sha256, now,
                ext.status, ext.error_message, meta.get("page_count"), meta.get("slide_count"), str(meta),
            ),
        )
```

to:

```python
        self.conn.execute(
            """
            INSERT INTO documents(path, filename, extension, file_size, modified_at, sha256, indexed_at, status, error_message, page_count, slide_count, metadata_json, owner_principal_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
              filename=excluded.filename, extension=excluded.extension, file_size=excluded.file_size, modified_at=excluded.modified_at,
              sha256=excluded.sha256, indexed_at=excluded.indexed_at, status=excluded.status, error_message=excluded.error_message,
              page_count=excluded.page_count, slide_count=excluded.slide_count, metadata_json=excluded.metadata_json,
              owner_principal_id=COALESCE(excluded.owner_principal_id, documents.owner_principal_id)
            """,
            (
                str(fp.path), fp.path.name, fp.path.suffix.lower(), fp.file_size, fp.modified_at.isoformat(), fp.sha256, now,
                ext.status, ext.error_message, meta.get("page_count"), meta.get("slide_count"), str(meta), owner_principal_id,
            ),
        )
```

> The `COALESCE(excluded..., documents...)` on update means a re-crawl (owner `None`) never clears an owner that an upload previously set. This is deliberate: an explicitly-owned upload must not be silently de-owned by a later crawl pass.

- [ ] **Step 5: Run tests, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_acl_enforcement.py -v
```
Expected: the four Task-1 tests pass. The existing `public`-read backfill in `upsert_document` (lines 268-278) is untouched, so owner-less docs stay visible.

- [ ] **Step 6: Commit**

```powershell
git add document_search/index/sqlite_store.py tests/test_acl_enforcement.py
git commit -m "feat(acl): persist owner_principal_id on upsert_document"
```

---

## Task 2: Owner-or-read helpers in `acl_service` and `SqliteStore`

**Files:**
- Modify: `document_search/services/acl_service.py` (add `can_read_document_subquery`)
- Modify: `document_search/index/sqlite_store.py` (add `user_can_read_document`)
- Test: `tests/test_acl_enforcement.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_acl_enforcement.py`:

```python
def _restrict_to_owner(store: SqliteStore, doc_id: int):
    """Strip the public read ACL so only the owner can see a doc."""
    public_id = store.conn.execute(
        "SELECT id FROM principals WHERE type='group' AND external_id='public'"
    ).fetchone()["id"]
    store.conn.execute(
        "DELETE FROM document_acl WHERE document_id=? AND principal_id=?",
        (doc_id, public_id),
    )
    store.conn.commit()


def test_user_can_read_document_owner(store, tmp_path):
    uid = store.create_user("alice", "pw")
    alice_pid = store.get_user_principal_id(uid)
    p = tmp_path / "a.txt"; p.write_text("x", encoding="utf-8")
    doc_id = store.upsert_document(_fp(p), _ext(p), owner_principal_id=alice_pid)
    _restrict_to_owner(store, doc_id)
    assert store.user_can_read_document(uid, doc_id) is True


def test_user_cannot_read_others_private_document(store, tmp_path):
    alice = store.create_user("alice", "pw")
    bob = store.create_user("bob", "pw")
    alice_pid = store.get_user_principal_id(alice)
    p = tmp_path / "a.txt"; p.write_text("x", encoding="utf-8")
    doc_id = store.upsert_document(_fp(p), _ext(p), owner_principal_id=alice_pid)
    _restrict_to_owner(store, doc_id)
    assert store.user_can_read_document(alice, doc_id) is True
    assert store.user_can_read_document(bob, doc_id) is False


def test_user_can_read_public_document(store, tmp_path):
    bob = store.create_user("bob", "pw")
    p = tmp_path / "pub.txt"; p.write_text("x", encoding="utf-8")
    doc_id = store.upsert_document(_fp(p), _ext(p))  # no owner -> public read
    assert store.user_can_read_document(bob, doc_id) is True


def test_user_can_read_missing_document_is_false(store):
    bob = store.create_user("bob", "pw")
    assert store.user_can_read_document(bob, 999999) is False
```

- [ ] **Step 2: Run tests, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_acl_enforcement.py -k user_can_read -v
```
Expected: FAIL — `user_can_read_document` does not exist.

- [ ] **Step 3: Add `can_read_document_subquery` to `acl_service`**

In `document_search/services/acl_service.py`, append below `visible_document_ids_subquery`:

```python
def can_read_document_subquery(user_id: int, document_id: int) -> tuple[str, list]:
    """Return a SQL fragment + params that yields exactly one row (the
    `document_id`) iff `user_id` is allowed to read it. Same visibility rules as
    `visible_document_ids_subquery`, narrowed to a single document.

    Intended for single-document permission checks:
        sql, params = can_read_document_subquery(uid, doc_id)
        allowed = conn.execute(sql, params).fetchone() is not None
    """
    sql = """
        SELECT d.id AS document_id
        FROM documents d
        LEFT JOIN users u ON u.id = ?
        WHERE d.id = ?
          AND (
            d.owner_principal_id = u.principal_id
            OR d.id IN (
              SELECT a.document_id FROM document_acl a
              WHERE a.permission = 'read'
                AND (
                  a.principal_id = u.principal_id
                  OR a.principal_id IN (
                    SELECT g.principal_id FROM user_groups g WHERE g.user_id = ?
                  )
                )
            )
          )
    """
    return sql, [user_id, document_id, user_id]
```

- [ ] **Step 4: Add `user_can_read_document` to `SqliteStore`**

In `document_search/index/sqlite_store.py`, add directly after the new `get_user_principal_id` method:

```python
    def user_can_read_document(self, user_id: int, document_id: int) -> bool:
        from document_search.services.acl_service import can_read_document_subquery
        sql, params = can_read_document_subquery(user_id, document_id)
        return self.conn.execute(sql, tuple(params)).fetchone() is not None
```

- [ ] **Step 5: Run tests, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_acl_enforcement.py -v
```
Expected: all Task-1 + Task-2 tests pass.

- [ ] **Step 6: Commit**

```powershell
git add document_search/services/acl_service.py document_search/index/sqlite_store.py tests/test_acl_enforcement.py
git commit -m "feat(acl): add single-document read check (can_read_document_subquery)"
```

---

## Task 3: Set owner on `/api/upload`

**Files:**
- Modify: `document_search/app.py:661-722` (`api_upload`)
- Test: `tests/test_acl_enforcement.py` (extend)

- [ ] **Step 1: Write the failing integration test**

Append to `tests/test_acl_enforcement.py` (FastAPI client section — add the import at the top of the file):

```python
fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from document_search.app import create_app  # noqa: E402


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    # Hermetic: keep uploads + db inside tmp_path. DOCUMENT_SEARCH_UPLOAD_ROOT is
    # read at create_app() time (app.py:266), so set it before constructing the app.
    monkeypatch.setenv("DOCUMENT_SEARCH_UPLOAD_ROOT", str(tmp_path / "uploads"))
    monkeypatch.chdir(tmp_path)
    app = create_app(str(tmp_path / "index.db"))
    return TestClient(app), tmp_path


def _admin_headers(client):
    r = client.post("/api/login", json={"username": "admin", "password": "admin"})
    return {"X-Auth-Token": r.json()["token"]}


def _make_second_user(client, admin_headers, username="bob", password="bob-password"):
    r = client.post(
        "/api/users",
        json={"username": username, "password": password, "role": "user"},
        headers=admin_headers,
    )
    assert r.status_code in (200, 201), r.text
    r = client.post("/api/login", json={"username": username, "password": password})
    return {"X-Auth-Token": r.json()["token"]}


def test_upload_sets_uploader_as_owner(app_client):
    client, _ = app_client
    admin = _admin_headers(client)
    files = {"file": ("note.txt", b"hello upload owner test", "text/plain")}
    r = client.post("/api/upload", files=files, headers=admin)
    assert r.status_code == 200, r.text
    doc_id = r.json()["document_id"]
    assert doc_id is not None

    # The admin's principal must own the uploaded doc.
    from document_search.index.sqlite_store import SqliteStore
    from pathlib import Path
    import os
    store = SqliteStore(Path(os.path.join(os.getcwd(), "index.db")))
    owner = store.conn.execute(
        "SELECT owner_principal_id FROM documents WHERE id=?", (doc_id,)
    ).fetchone()["owner_principal_id"]
    admin_pid = store.conn.execute(
        "SELECT principal_id FROM users WHERE username='admin'"
    ).fetchone()["principal_id"]
    assert owner == admin_pid
```

> The `db_path` passed to `create_app` is `str(tmp_path / "index.db")` and the worker/store open it relative to cwd; the `monkeypatch.chdir(tmp_path)` keeps the path resolvable. If `/api/users` returns a different success shape in this codebase, read `api_create_user` (app.py:1086-1098) and adjust the assertion.

- [ ] **Step 2: Run test, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_acl_enforcement.py::test_upload_sets_uploader_as_owner -v
```
Expected: FAIL — `owner_principal_id` is NULL because `api_upload` calls `db.upsert_document(fp, result)` without an owner.

- [ ] **Step 3: Pass the uploader's principal into `upsert_document`**

In `document_search/app.py`, in `api_upload`, change lines 715-720 from:

```python
        if result:
            doc_id = db.upsert_document(fp, result)
            if tag_list:
                db.set_tags(user_id, doc_id, tag_list)
        else:
            doc_id = None
```

to:

```python
        if result:
            owner_pid = db.get_user_principal_id(user_id)
            doc_id = db.upsert_document(fp, result, owner_principal_id=owner_pid)
            if tag_list:
                db.set_tags(user_id, doc_id, tag_list)
        else:
            doc_id = None
```

- [ ] **Step 4: Run test, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_acl_enforcement.py::test_upload_sets_uploader_as_owner -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add document_search/app.py tests/test_acl_enforcement.py
git commit -m "feat(acl): upload sets the uploading user as document owner"
```

---

## Task 4: Set default owner on crawl ingest (configurable, default = public)

**Files:**
- Modify: `document_search/app.py:295-328` (the `index_paths` worker handler `_handle_index_paths`)
- Test: `tests/test_acl_enforcement.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_acl_enforcement.py`:

```python
def test_crawl_default_owner_env_sets_owner(tmp_path, monkeypatch):
    """When SEEKR_DEFAULT_OWNER_PRINCIPAL names a principal external_id, crawled
    docs are owned by it; unset (default) leaves owner NULL + public read."""
    from document_search.index.sqlite_store import SqliteStore
    store = SqliteStore(tmp_path / "index.db")
    # Create a group principal to act as the configured default owner.
    from datetime import UTC, datetime
    now = datetime.now(tz=UTC).isoformat()
    store.conn.execute(
        "INSERT OR IGNORE INTO principals(type, external_id, display_name, created_at) "
        "VALUES('group','staff','Staff',?)",
        (now,),
    )
    store.conn.commit()
    monkeypatch.setenv("SEEKR_DEFAULT_OWNER_PRINCIPAL", "staff")

    from document_search.app import _resolve_default_owner_principal_id
    pid = _resolve_default_owner_principal_id(store)
    expected = store.conn.execute(
        "SELECT id FROM principals WHERE type='group' AND external_id='staff'"
    ).fetchone()["id"]
    assert pid == expected


def test_crawl_default_owner_unset_is_none(tmp_path, monkeypatch):
    from document_search.index.sqlite_store import SqliteStore
    store = SqliteStore(tmp_path / "index.db")
    monkeypatch.delenv("SEEKR_DEFAULT_OWNER_PRINCIPAL", raising=False)
    from document_search.app import _resolve_default_owner_principal_id
    assert _resolve_default_owner_principal_id(store) is None
```

- [ ] **Step 2: Run tests, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_acl_enforcement.py -k default_owner -v
```
Expected: FAIL — `_resolve_default_owner_principal_id` does not exist.

- [ ] **Step 3: Add the resolver and wire it into the crawl handler**

In `document_search/app.py`, add a module-level helper near the other top-level helpers (e.g. just below `_check_api_key`, around line 90):

```python
def _resolve_default_owner_principal_id(db) -> int | None:
    """Resolve the configurable default owner for crawled documents.

    Controlled by the SEEKR_DEFAULT_OWNER_PRINCIPAL env var, whose value is a
    principal `external_id` (group or user). Unset/blank/unknown -> None, which
    preserves the legacy behaviour (no owner; document visible via the public
    group's read ACL applied in upsert_document/_backfill_acl).
    """
    import os as _os
    external_id = (_os.getenv("SEEKR_DEFAULT_OWNER_PRINCIPAL") or "").strip()
    if not external_id:
        return None
    row = db.conn.execute(
        "SELECT id FROM principals WHERE external_id=? ORDER BY (type='group') DESC LIMIT 1",
        (external_id,),
    ).fetchone()
    return row["id"] if row else None
```

Then in the `index_paths` worker handler (`_handle_index_paths`, the `for path in iter_documents(...)` loop at lines 304-327), resolve the default owner once before the loop and pass it to `upsert_document`. Change line 303-304 from:

```python
        db = SqliteStore(Path(db_path))
        for path in iter_documents([Path(p) for p in paths], cfg):
```

to:

```python
        db = SqliteStore(Path(db_path))
        default_owner_pid = _resolve_default_owner_principal_id(db)
        for path in iter_documents([Path(p) for p in paths], cfg):
```

And change line 319 from:

```python
            db.upsert_document(fp, result)
```

to:

```python
            db.upsert_document(fp, result, owner_principal_id=default_owner_pid)
```

> Default behaviour is unchanged: with the env var unset, `default_owner_pid` is `None`, owner stays NULL, and the existing `public` read-ACL keeps the doc visible to everyone. Reindex via `/api/documents/{id}/reindex` (app.py:622-639) deliberately keeps calling `upsert_document(fp, result)` without an owner so the `COALESCE` on update preserves any owner already set.

- [ ] **Step 4: Run tests, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_acl_enforcement.py -k default_owner -v
```
Expected: both pass.

- [ ] **Step 5: Commit**

```powershell
git add document_search/app.py tests/test_acl_enforcement.py
git commit -m "feat(acl): crawled docs get configurable default owner (default public)"
```

---

## Task 5: ACL-filter document counts (`/api/status`, `/api/ha/status`)

**Files:**
- Modify: `document_search/app.py:918-936` (`api_ha_status`), `document_search/app.py:1623-1630` (`api_status`)
- Test: `tests/test_acl_enforcement.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_acl_enforcement.py`:

```python
def _login(client, username, password):
    r = client.post("/api/login", json={"username": username, "password": password})
    return {"X-Auth-Token": r.json()["token"]}


def _seed_private_doc_for_admin(client, tmp_path):
    """Upload one doc as admin, then strip its public ACL so only admin sees it."""
    admin = _admin_headers(client)
    files = {"file": ("admin-only.txt", b"private admin content", "text/plain")}
    r = client.post("/api/upload", files=files, headers=admin)
    doc_id = r.json()["document_id"]
    from document_search.index.sqlite_store import SqliteStore
    from pathlib import Path
    import os
    store = SqliteStore(Path(os.path.join(os.getcwd(), "index.db")))
    public_id = store.conn.execute(
        "SELECT id FROM principals WHERE type='group' AND external_id='public'"
    ).fetchone()["id"]
    store.conn.execute(
        "DELETE FROM document_acl WHERE document_id=? AND principal_id=?",
        (doc_id, public_id),
    )
    store.conn.commit()
    return doc_id


def test_status_count_is_acl_filtered(app_client):
    client, tmp_path = app_client
    admin = _admin_headers(client)
    bob = _make_second_user(client, admin)
    _seed_private_doc_for_admin(client, tmp_path)

    admin_status = client.get("/api/status", headers=admin).json()
    bob_status = client.get("/api/status", headers=bob).json()
    assert admin_status["documents"] == 1
    assert bob_status["documents"] == 0
```

- [ ] **Step 2: Run test, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_acl_enforcement.py::test_status_count_is_acl_filtered -v
```
Expected: FAIL — both counts are `1` (count is unfiltered).

- [ ] **Step 3: Filter the `/api/status` count**

In `document_search/app.py`, change `api_status` (lines 1623-1630) from:

```python
    @app.get("/api/status")
    def api_status(x_auth_token: str | None = Header(default=None)):
        require_user(x_auth_token)
        db = store()
        docs = db.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        blocks = db.conn.execute("SELECT COUNT(*) FROM content_blocks").fetchone()[0]
        total_size = db.conn.execute("SELECT COALESCE(SUM(file_size), 0) FROM documents").fetchone()[0]
        return {"documents": docs, "content_blocks": blocks, "total_file_size_bytes": total_size, "db_path": db_path}
```

to:

```python
    @app.get("/api/status")
    def api_status(x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        db = store()
        from document_search.services.acl_service import visible_document_ids_subquery
        acl_sql, acl_params = visible_document_ids_subquery(user_id)
        docs = db.conn.execute(
            f"SELECT COUNT(*) FROM documents d WHERE d.id IN ({acl_sql})", acl_params
        ).fetchone()[0]
        blocks = db.conn.execute(
            f"SELECT COUNT(*) FROM content_blocks cb WHERE cb.document_id IN ({acl_sql})",
            acl_params,
        ).fetchone()[0]
        total_size = db.conn.execute(
            f"SELECT COALESCE(SUM(d.file_size), 0) FROM documents d WHERE d.id IN ({acl_sql})",
            acl_params,
        ).fetchone()[0]
        return {"documents": docs, "content_blocks": blocks, "total_file_size_bytes": total_size, "db_path": db_path}
```

- [ ] **Step 4: Filter the `/api/ha/status` count**

`/api/ha/status` is the HA channel — it has **no Seekr user identity**, only an API key (the same contract as `_ha_search_impl`, which uses `bypass_acl=True`). To stay consistent with that contract, scope the HA status counts by the key's `path_filter` (the HA equivalent of "what this key may see"), not by a user. Change `api_ha_status` (lines 918-936) from:

```python
    @app.get("/api/ha/status", tags=["ha"])
    def api_ha_status(x_api_key: str | None = Header(default=None)):
        key_cfg = _resolve_ha_key(x_api_key)
        if not key_cfg:
            raise HTTPException(
                status_code=401,
                detail="Invalid or missing API key. Configure one via Config → Home Assistant.",
            )
        db = store()
        doc_count = db.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        block_count = db.conn.execute("SELECT COUNT(*) FROM content_blocks").fetchone()[0]
        total_size = db.conn.execute("SELECT COALESCE(SUM(file_size), 0) FROM documents").fetchone()[0]
        return {
            "state": "online",
            "documents": doc_count,
            "content_blocks": block_count,
            "total_file_size_bytes": total_size,
            "app_version": app.version,
        }
```

to:

```python
    @app.get("/api/ha/status", tags=["ha"])
    def api_ha_status(x_api_key: str | None = Header(default=None)):
        key_cfg = _resolve_ha_key(x_api_key)
        if not key_cfg:
            raise HTTPException(
                status_code=401,
                detail="Invalid or missing API key. Configure one via Config → Home Assistant.",
            )
        db = store()
        # HA has no Seekr user identity (API-key channel, mirrors _ha_search_impl's
        # bypass_acl=True). Scope counts by the key's path_filter instead — that is the
        # only visibility boundary an HA key carries. See ACL Foundation plan, Task 10.
        path_filter: str | None = key_cfg.get("path_filter")
        where = ""
        params: list = []
        if path_filter:
            where = " WHERE d.path LIKE ?"
            params = [path_filter + "%"]
        doc_count = db.conn.execute(
            f"SELECT COUNT(*) FROM documents d{where}", params
        ).fetchone()[0]
        block_count = db.conn.execute(
            f"SELECT COUNT(*) FROM content_blocks cb WHERE cb.document_id IN "
            f"(SELECT d.id FROM documents d{where})",
            params,
        ).fetchone()[0]
        total_size = db.conn.execute(
            f"SELECT COALESCE(SUM(d.file_size), 0) FROM documents d{where}", params
        ).fetchone()[0]
        return {
            "state": "online",
            "documents": doc_count,
            "content_blocks": block_count,
            "total_file_size_bytes": total_size,
            "app_version": app.version,
        }
```

- [ ] **Step 5: Run tests, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_acl_enforcement.py::test_status_count_is_acl_filtered -v
```
Expected: PASS. (`/api/ha/status` path-filter behaviour is verified manually in Task 9; no fixture HA key is set up here.)

- [ ] **Step 6: Commit**

```powershell
git add document_search/app.py tests/test_acl_enforcement.py
git commit -m "feat(acl): filter /api/status counts by visibility; scope HA status by path_filter"
```

---

## Task 6: Read-check single-document endpoints (`/api/files/open`, mark, tags)

**Files:**
- Modify: `document_search/app.py:1632-1642` (`api_files_open`), `document_search/app.py:598-605` (`api_mark`), `document_search/app.py:607-614` (`api_tags`)
- Test: `tests/test_acl_enforcement.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_acl_enforcement.py`:

```python
def test_files_open_forbidden_for_non_reader(app_client):
    client, tmp_path = app_client
    admin = _admin_headers(client)
    bob = _make_second_user(client, admin)
    doc_id = _seed_private_doc_for_admin(client, tmp_path)

    # Admin owns it -> 200 (file exists on disk under upload_root).
    r_admin = client.get(f"/api/files/open?document_id={doc_id}", headers=admin)
    assert r_admin.status_code == 200, r_admin.text
    # Bob has no read grant -> 403, NOT 200 and NOT a leaky 404 path disclosure.
    r_bob = client.get(f"/api/files/open?document_id={doc_id}", headers=bob)
    assert r_bob.status_code == 403


def test_mark_forbidden_for_non_reader(app_client):
    client, tmp_path = app_client
    admin = _admin_headers(client)
    bob = _make_second_user(client, admin)
    doc_id = _seed_private_doc_for_admin(client, tmp_path)
    r = client.post(
        "/api/documents/mark",
        json={"document_id": doc_id, "is_marked": True},
        headers=bob,
    )
    assert r.status_code == 403


def test_tags_forbidden_for_non_reader(app_client):
    client, tmp_path = app_client
    admin = _admin_headers(client)
    bob = _make_second_user(client, admin)
    doc_id = _seed_private_doc_for_admin(client, tmp_path)
    r = client.post(
        "/api/documents/tags",
        json={"document_id": doc_id, "tags": ["x"]},
        headers=bob,
    )
    assert r.status_code == 403
```

- [ ] **Step 2: Run tests, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_acl_enforcement.py -k "forbidden_for_non_reader" -v
```
Expected: FAIL — all three currently return 200 (`mark`/`tags`) or serve the file (`files/open`) regardless of visibility.

- [ ] **Step 3: Add the read check to `/api/files/open`**

In `document_search/app.py`, change `api_files_open` (lines 1632-1642) from:

```python
    @app.get("/api/files/open")
    def api_files_open(document_id: int, x_auth_token: str | None = Header(default=None)):
        require_user(x_auth_token)
        db = store()
        doc = db.get_document_by_id(document_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        p = Path(doc["path"])
        if not p.exists() or not p.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(p)
```

to:

```python
    @app.get("/api/files/open")
    def api_files_open(document_id: int, x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        db = store()
        doc = db.get_document_by_id(document_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        if not db.user_can_read_document(user_id, document_id):
            raise HTTPException(status_code=403, detail="Not permitted to read this document")
        p = Path(doc["path"])
        if not p.exists() or not p.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(p)
```

- [ ] **Step 4: Add the read check to `/api/documents/mark` and `/api/documents/tags`**

Change `api_mark` (lines 598-605) from:

```python
    @app.post("/api/documents/mark")
    def api_mark(req: MarkRequest, x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        db = store()
        if not db.get_document_by_id(req.document_id):
            raise HTTPException(status_code=404, detail="Document not found")
        db.set_mark(user_id, req.document_id, req.is_marked)
        return {"status": "ok"}
```

to:

```python
    @app.post("/api/documents/mark")
    def api_mark(req: MarkRequest, x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        db = store()
        if not db.get_document_by_id(req.document_id):
            raise HTTPException(status_code=404, detail="Document not found")
        if not db.user_can_read_document(user_id, req.document_id):
            raise HTTPException(status_code=403, detail="Not permitted to read this document")
        db.set_mark(user_id, req.document_id, req.is_marked)
        return {"status": "ok"}
```

Change `api_tags` (lines 607-614) from:

```python
    @app.post("/api/documents/tags")
    def api_tags(req: TagsRequest, x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        db = store()
        if not db.get_document_by_id(req.document_id):
            raise HTTPException(status_code=404, detail="Document not found")
        db.set_tags(user_id, req.document_id, req.tags)
        return {"status": "ok"}
```

to:

```python
    @app.post("/api/documents/tags")
    def api_tags(req: TagsRequest, x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        db = store()
        if not db.get_document_by_id(req.document_id):
            raise HTTPException(status_code=404, detail="Document not found")
        if not db.user_can_read_document(user_id, req.document_id):
            raise HTTPException(status_code=403, detail="Not permitted to read this document")
        db.set_tags(user_id, req.document_id, req.tags)
        return {"status": "ok"}
```

- [ ] **Step 5: Run tests, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_acl_enforcement.py -k "forbidden_for_non_reader" -v
```
Expected: all three pass.

- [ ] **Step 6: Commit**

```powershell
git add document_search/app.py tests/test_acl_enforcement.py
git commit -m "feat(acl): gate files/open, mark, tags by per-document read permission"
```

---

## Task 7: ACL-filter the AI sampling SQL (`ai_suggest_structure`, `ai_reorganize`)

**Files:**
- Modify: `document_search/app.py:330-348` (`_handle_ai_suggest_structure`), `document_search/app.py:350-391` (`_handle_ai_reorganize`)
- Modify: `document_search/app.py:647-659` (`api_ai_suggest_structure`), `document_search/app.py:1489-1501` (`api_ai_reorganize_start`) — put the requesting user's id into the job payload so the worker can filter.
- Test: `tests/test_acl_enforcement.py` (extend)

- [ ] **Step 1: Write the failing test (worker-level, no Ollama needed)**

The handlers call `organizer.suggest_structure(...)` / `organizer.suggest(...)`, which need Ollama. Test the **sampling SQL** in isolation by asserting the handler only *sees* visible docs. Refactor the sampling into a tiny pure helper so it is testable without Ollama. Append to `tests/test_acl_enforcement.py`:

```python
def test_suggest_structure_sample_is_acl_filtered(tmp_path):
    from document_search.app import _sample_documents_for_user
    from document_search.index.sqlite_store import SqliteStore
    from datetime import UTC, datetime
    store = SqliteStore(tmp_path / "index.db")
    alice = store.create_user("alice", "pw")
    bob = store.create_user("bob", "pw")
    alice_pid = store.get_user_principal_id(alice)
    now = datetime.now(tz=UTC).isoformat()

    for path, owner in [("/d/alice.txt", alice_pid), ("/d/pub.txt", None)]:
        store.conn.execute(
            "INSERT INTO documents(path, filename, extension, file_size, modified_at, "
            "sha256, indexed_at, status, owner_principal_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (path, Path(path).name, ".txt", 1, now, path, now, "ok", owner),
        )
    store.conn.commit()
    # Re-open to backfill public read on /d/pub.txt; /d/alice.txt is owner-only.
    store = SqliteStore(tmp_path / "index.db")
    public_id = store.conn.execute(
        "SELECT id FROM principals WHERE type='group' AND external_id='public'"
    ).fetchone()["id"]
    alice_doc = store.conn.execute("SELECT id FROM documents WHERE path='/d/alice.txt'").fetchone()["id"]
    store.conn.execute(
        "DELETE FROM document_acl WHERE document_id=? AND principal_id=?",
        (alice_doc, public_id),
    )
    store.conn.commit()

    alice_paths = {r["path"] for r in _sample_documents_for_user(store, alice, 50)}
    bob_paths = {r["path"] for r in _sample_documents_for_user(store, bob, 50)}
    assert "/d/alice.txt" in alice_paths and "/d/pub.txt" in alice_paths
    assert "/d/alice.txt" not in bob_paths and "/d/pub.txt" in bob_paths
```

- [ ] **Step 2: Run test, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_acl_enforcement.py::test_suggest_structure_sample_is_acl_filtered -v
```
Expected: FAIL — `_sample_documents_for_user` does not exist.

- [ ] **Step 3: Add the `_sample_documents_for_user` helper and use it in both handlers**

In `document_search/app.py`, add a module-level helper near `_resolve_default_owner_principal_id` (added in Task 4):

```python
def _sample_documents_for_user(db, user_id: int, sample_size: int):
    """Random sample of documents the given user may read, with tags. Used by the
    AI suggest-structure / reorganize handlers so sampling never leaks docs the
    requesting user cannot see."""
    from document_search.services.acl_service import visible_document_ids_subquery
    acl_sql, acl_params = visible_document_ids_subquery(user_id)
    sql = f"""
        SELECT d.id, d.filename, d.extension, d.path,
               GROUP_CONCAT(ut.name, ', ') AS tags
        FROM documents d
        LEFT JOIN document_tags dt ON dt.document_id = d.id
        LEFT JOIN user_tags ut ON ut.id = dt.tag_id
        WHERE d.id IN ({acl_sql})
        GROUP BY d.id
        ORDER BY RANDOM()
        LIMIT ?
    """
    params = list(acl_params) + [min(sample_size, 100)]
    return db.conn.execute(sql, params).fetchall()
```

Now make both handlers read the requesting user's id from the job payload. First, **put the user id into the payload** at enqueue time.

Change `api_ai_suggest_structure` (lines 647-659) payload from:

```python
        job_id = job_store.enqueue(
            "ai_suggest_structure",
            payload={"sample_size": sample_size},
            owner_user_id=user_id,
            max_retries=0,
        )
```

to:

```python
        job_id = job_store.enqueue(
            "ai_suggest_structure",
            payload={"sample_size": sample_size, "user_id": user_id},
            owner_user_id=user_id,
            max_retries=0,
        )
```

Change `api_ai_reorganize_start` (lines 1489-1501) payload from:

```python
        job_id = job_store.enqueue(
            "ai_reorganize",
            payload={"limit": limit},
            owner_user_id=admin_id,
            max_retries=0,
        )
```

to:

```python
        job_id = job_store.enqueue(
            "ai_reorganize",
            payload={"limit": limit, "user_id": admin_id},
            owner_user_id=admin_id,
            max_retries=0,
        )
```

Then change `_handle_ai_suggest_structure` (lines 330-348) from:

```python
    @worker.handler("ai_suggest_structure")
    def _handle_ai_suggest_structure(payload: dict, progress_cb):
        sample_size = payload.get("sample_size", 50)
        db = SqliteStore(Path(db_path))
        rows = db.conn.execute(
            """
            SELECT d.id, d.filename, d.extension, d.path,
                   GROUP_CONCAT(ut.name, ', ') AS tags
            FROM documents d
            LEFT JOIN document_tags dt ON dt.document_id = d.id
            LEFT JOIN user_tags ut ON ut.id = dt.tag_id
            GROUP BY d.id
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (min(sample_size, 100),),
        ).fetchall()
        result = organizer.suggest_structure([dict(r) for r in rows])
        return result
```

to:

```python
    @worker.handler("ai_suggest_structure")
    def _handle_ai_suggest_structure(payload: dict, progress_cb):
        sample_size = payload.get("sample_size", 50)
        user_id = payload["user_id"]
        db = SqliteStore(Path(db_path))
        rows = _sample_documents_for_user(db, user_id, sample_size)
        result = organizer.suggest_structure([dict(r) for r in rows])
        return result
```

And change the sampling in `_handle_ai_reorganize` (lines 350-358) from:

```python
    @worker.handler("ai_reorganize")
    def _handle_ai_reorganize(payload: dict, progress_cb):
        limit = payload.get("limit", 10)
        db = SqliteStore(Path(db_path))
        rows = db.conn.execute(
            "SELECT id, path, filename, extension FROM documents LIMIT ?",
            (min(limit, 50),),
        ).fetchall()
        eligible = [r for r in rows if Path(r["path"]).is_relative_to(upload_root.resolve())]
```

to:

```python
    @worker.handler("ai_reorganize")
    def _handle_ai_reorganize(payload: dict, progress_cb):
        limit = payload.get("limit", 10)
        user_id = payload["user_id"]
        db = SqliteStore(Path(db_path))
        from document_search.services.acl_service import visible_document_ids_subquery
        acl_sql, acl_params = visible_document_ids_subquery(user_id)
        rows = db.conn.execute(
            f"SELECT d.id, d.path, d.filename, d.extension FROM documents d "
            f"WHERE d.id IN ({acl_sql}) LIMIT ?",
            list(acl_params) + [min(limit, 50)],
        ).fetchall()
        eligible = [r for r in rows if Path(r["path"]).is_relative_to(upload_root.resolve())]
```

> The rest of `_handle_ai_reorganize` (the `for doc in eligible` loop at lines 361-391) is unchanged. Existing in-flight jobs enqueued before this change won't have `user_id` in their payload; `payload["user_id"]` will `KeyError` and the job fails cleanly (visible in the job's `error_message`) rather than leaking. That is acceptable for a dev/single-instance deployment; note it in Task 9 verification.

- [ ] **Step 4: Run test, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_acl_enforcement.py::test_suggest_structure_sample_is_acl_filtered -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add document_search/app.py tests/test_acl_enforcement.py
git commit -m "feat(acl): AI suggest/reorganize sample only the requesting user's visible docs"
```

---

## Task 8: Document the filesystem-listing endpoints (`/api/folders`, `/api/source-folders`)

**Files:**
- Modify: `document_search/app.py:724-739` (`api_folders`), `document_search/app.py:741-769` (`api_source_folders`)

These two endpoints walk the **filesystem** (`upload_root` and configured `source_paths`), not the document index — they return directory names, never document rows, and carry no `document_id`. The ACL model governs *documents*, not raw folders, so there is nothing to filter through `visible_document_ids_subquery` here. They are already behind `require_user`. To prevent a future reviewer from "fixing" them incorrectly, leave a comment recording the deliberate decision.

- [ ] **Step 1: Add an explanatory comment to `api_folders`**

In `document_search/app.py`, change `api_folders` (lines 724-726) from:

```python
    @app.get("/api/folders")
    def api_folders(x_auth_token: str | None = Header(default=None)):
        require_user(x_auth_token)
```

to:

```python
    @app.get("/api/folders")
    def api_folders(x_auth_token: str | None = Header(default=None)):
        # Returns filesystem directory names under upload_root, not index documents.
        # The ACL model governs documents (document_id), not raw folders, so there is
        # no visible_document_ids_subquery to apply here. Auth gate only. See ACL
        # Enforcement Completion plan, Task 8.
        require_user(x_auth_token)
```

- [ ] **Step 2: Add an explanatory comment to `api_source_folders`**

Change `api_source_folders` (lines 741-743) from:

```python
    @app.get("/api/source-folders")
    def api_source_folders(x_auth_token: str | None = Header(default=None)):
        require_user(x_auth_token)
```

to:

```python
    @app.get("/api/source-folders")
    def api_source_folders(x_auth_token: str | None = Header(default=None)):
        # Filesystem listing of configured source_paths (admin-curated config), not
        # index documents. No document_id is returned, so the ACL document filter
        # does not apply. Auth gate only. See ACL Enforcement Completion plan, Task 8.
        require_user(x_auth_token)
```

- [ ] **Step 3: Verify the endpoints still behave**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_search.py -k "folders" -v
```
Expected: existing folder tests still pass (comment-only change).

- [ ] **Step 4: Commit**

```powershell
git add document_search/app.py
git commit -m "feat(acl): document why folder-listing endpoints are not document-ACL filtered"
```

---

## Task 9: Full suite + manual API verification

**Files:** none (verification only)

- [ ] **Step 1: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```
Expected: every test passes, including the pre-existing `tests/test_search_acl.py`, `tests/test_acl_service.py`, `tests/test_app_search.py`, and the new `tests/test_acl_enforcement.py`.

- [ ] **Step 2: Confirm no new `bypass_acl=True` call sites slipped in**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_search_acl.py::test_ha_search_uses_bypass_acl tests/test_search_acl.py::test_cli_uses_bypass_acl -v
```
Expected: PASS — the only `bypass_acl=True` sites remain `_ha_search_impl` and CLI `cmd_search`. Grep to be sure no others were added:

```powershell
Select-String -Path document_search\*.py, document_search\**\*.py -Pattern "bypass_acl=True"
```
Expected: exactly two matches (`document_search/app.py` `_ha_search_impl`, `document_search/main.py` `cmd_search`).

- [ ] **Step 3: Manual smoke test (two users)**

```powershell
$env:DOCUMENT_SEARCH_DB = ".\smoke.db"
uvicorn document_search.app:app --port 8080
```

In another shell:

```powershell
# Admin login
$admin = (Invoke-RestMethod -Uri http://localhost:8080/api/login -Method POST `
  -Body '{"username":"admin","password":"admin"}' -ContentType 'application/json').token
# Create a second user 'bob'
Invoke-RestMethod -Uri http://localhost:8080/api/users -Method POST `
  -Headers @{ "X-Auth-Token" = $admin } `
  -Body '{"username":"bob","password":"bob-password","role":"user"}' -ContentType 'application/json'
$bob = (Invoke-RestMethod -Uri http://localhost:8080/api/login -Method POST `
  -Body '{"username":"bob","password":"bob-password"}' -ContentType 'application/json').token

# Upload a file as admin (admin becomes owner)
Invoke-RestMethod -Uri http://localhost:8080/api/upload -Method POST `
  -Headers @{ "X-Auth-Token" = $admin } `
  -Form @{ file = Get-Item .\README.md }

# Strip its public ACL via sqlite3 so only admin can see it, then compare /api/status:
# admin should report documents=1, bob should report documents=0.
Invoke-RestMethod -Uri http://localhost:8080/api/status -Headers @{ "X-Auth-Token" = $admin }
Invoke-RestMethod -Uri http://localhost:8080/api/status -Headers @{ "X-Auth-Token" = $bob }
```

Expected: with the public ACL removed for the uploaded doc, admin's `/api/status` shows `documents: 1`, bob's shows `documents: 0`; `GET /api/files/open?document_id=<id>` returns 200 for admin and 403 for bob.

- [ ] **Step 4: Verify default-owner crawl env var (optional)**

```powershell
$env:SEEKR_DEFAULT_OWNER_PRINCIPAL = "public"   # owns crawled docs by the public group
# Run a crawl via /api/index/start, then check owner_principal_id is populated:
sqlite3 .\smoke.db "SELECT COUNT(*) FROM documents WHERE owner_principal_id IS NOT NULL;"
```
Expected: with the env var set to a real principal `external_id`, newly crawled docs have a non-NULL `owner_principal_id`. Unset → NULL (legacy behaviour). Remember to `Remove-Item Env:SEEKR_DEFAULT_OWNER_PRINCIPAL` afterwards.

- [ ] **Step 5: No further commit** (verification only).

---

## Definition of Done

- [ ] `$env:PYTHONPATH = "."; pytest -q` is green on a clean clone.
- [ ] `documents.owner_principal_id` is set to the uploader's principal on `/api/upload`, and to the configured default (or NULL) on crawl ingest via `upsert_document`.
- [ ] `SEEKR_DEFAULT_OWNER_PRINCIPAL` unset preserves legacy behaviour (owner NULL, doc visible through `public` read ACL).
- [ ] `/api/status` document/block/size counts are filtered through `visible_document_ids_subquery`; `/api/ha/status` counts are scoped by the key's `path_filter`.
- [ ] `/api/files/open`, `/api/documents/mark`, `/api/documents/tags` return 403 when the requesting user has no read permission on the document.
- [ ] `ai_suggest_structure` and `ai_reorganize` sample only documents the requesting user may read (user id carried in the job payload).
- [ ] `/api/folders` and `/api/source-folders` carry a comment recording why they are not document-ACL filtered (they list filesystem directories, not documents).
- [ ] All document-visibility logic still flows through `acl_service` (`visible_document_ids_subquery` for lists/counts, `can_read_document_subquery` for single docs) — no endpoint hand-rolls ACL SQL.
- [ ] Exactly two `bypass_acl=True` call sites remain: `_ha_search_impl` (HA) and CLI `cmd_search`. No new bypass added.
- [ ] Manual two-user smoke test confirms admin sees an owner-only doc and bob does not.

---

## Notes for the executing agent

- **The subquery stays the single source of truth.** Every list/count endpoint embeds `visible_document_ids_subquery(user_id)` verbatim; single-document checks embed `can_read_document_subquery(user_id, document_id)`. Do **not** write a fourth variant of the ACL SQL anywhere — if you need a new shape, add it to `acl_service.py` so a future audit has exactly one file to read.
- **`bypass_acl` is unchanged and security-critical.** This plan adds zero new bypass sites. `_ha_search_impl` and CLI `cmd_search` keep theirs; `/api/ha/status` deliberately uses `path_filter` scoping (consistent with `_ha_search_impl`) rather than a fabricated user identity. Never expose `bypass_acl` to a user-facing HTTP route.
- **Why `COALESCE` on `owner_principal_id` update.** A re-crawl or reindex passes `owner_principal_id=None`; without the `COALESCE`, an `INSERT ... ON CONFLICT DO UPDATE` would overwrite an upload's owner with NULL, silently de-owning it. The `COALESCE(excluded.owner_principal_id, documents.owner_principal_id)` keeps any owner already set.
- **NULL owner is intentional for legacy/crawled docs.** SQL `NULL = NULL` is false, so owner-based visibility silently doesn't fire for owner-less docs; the `public` read ACL (set by `_backfill_acl` and `upsert_document`) carries them. This matches the original ACL Foundation contract.
- **Admins go through ACL for listings/counts**, exactly like any user — there is no admin bypass in `/api/status` or the AI sampling. The only owner-or-admin-style escape hatches are the management endpoints (`mark`/`tags`/`files/open`), and those use `user_can_read_document`, which already grants the owner. If a future task wants admins to see everything, that belongs in `acl_service` (e.g. an `is_admin` short-circuit), not scattered across endpoints.
- **In-flight AI jobs.** Jobs enqueued before Task 7 lack `user_id` in their payload and will fail with `KeyError` rather than leak. Acceptable for single-instance dev; if you must drain a populated queue, clear pending `ai_*` jobs first.
- **Model constructors.** If `FileFingerprint`/`ExtractionResult`/`ContentBlock` in `document_search/models.py` don't match the `_fp`/`_ext` helpers, fall back to the raw-SQL insert style already used in `tests/test_search_acl.py` — both approaches exercise the same code paths.
