# ACL Management API & UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Seekr admins a way to *manage* the ACL layer that the ACL-foundation plan put in place. Today the schema exists (`principals`, `user_groups`, `document_acl`), `acl_service.visible_document_ids_subquery` filters every read, but there is no write-API and the `write` permission is never queried. This plan adds: CRUD helpers in `sqlite_store`, a `can_write(user_id, document_id)` helper in `acl_service`, admin REST endpoints `/api/groups/*` and `/api/acl/*`, enforcement of `write` on the document-mutating endpoints (reindex, move), and a Config → **Access** tab to drive it all.

**Architecture:** All ACL table writes funnel through new `SqliteStore` methods (the single DB layer, mirroring how `JobStore` owns the `jobs` table). `acl_service` stays the single source of truth for *read* visibility and gains the symmetric *write* check `can_write`. New FastAPI routes are thin: validate input (Pydantic), call the store method, return ACL-safe shapes; every route is `require_admin` except the read-only document-ACL listing which an owner may also see. The frontend follows the existing **Users** tab pattern exactly — tables rendered from JSON, create/delete buttons, `showToast` feedback, lazy load on `switchTab`.

**Tech Stack:** Python 3.11, SQLite (FTS5), FastAPI, pytest + `fastapi.testclient.TestClient`, vanilla JS + Jinja2. No new third-party dependencies.

**Scope boundaries:**

In scope:
- `SqliteStore` CRUD: groups (create/list/delete), membership (add/remove user↔group), grants (grant/revoke read|write on a document to a principal), and listing a document's ACL entries.
- `acl_service.can_write(user_id, document_id)` — owner OR a `write` ACL row on a principal the user holds.
- `/api/groups/*` (group CRUD + membership) and `/api/acl/*` (grant/revoke/list), all `require_admin` except `GET /api/acl/documents/{id}` (admin OR owner).
- Wire `can_write` into `/api/documents/{id}/reindex` and `/api/ai/reorganize/apply` (the move path) so non-owners without `write` get 403.
- Config → **Access** tab: manage groups, membership, and per-document grants.

Out of scope (deferred):
- Editing the `public` group or deleting built-in principals (guarded against).
- Nextcloud/WebDAV-driven ACL sync — owners are still set only by ingestion.
- Per-folder / inherited ACLs — grants are per-document only.
- A bulk "share these 50 docs" UI — single-document grant only.
- Changing the read-filter contract in `visible_document_ids_subquery` (left untouched).

---

## File Structure

**Create:**
- `tests/test_acl_management.py` — unit tests for the new `SqliteStore` ACL CRUD helpers + `can_write`.
- `tests/test_app_acl.py` — integration tests for `/api/groups/*`, `/api/acl/*`, and write-enforcement, via `TestClient` (mirrors `tests/test_app_jobs.py`).

**Modify:**
- `document_search/index/sqlite_store.py` — add ACL CRUD helper methods (after `_backfill_acl`, before `get_document`).
- `document_search/services/acl_service.py` — add `can_write`.
- `document_search/app.py` — Pydantic request models, `/api/groups/*` + `/api/acl/*` routes, write-enforcement on reindex + move.
- `document_search/web/templates/config.html` — Access tab button + panel.
- `document_search/web/static/app.js` — Access tab JS (load/render/create/delete groups, membership, grants), wire into `switchTab`.

**Untouched (contract preserved):**
- `acl_service.visible_document_ids_subquery` — read filtering unchanged.
- `_backfill_acl`, `create_user`, `upsert_document` — principal/`public` bootstrap unchanged.

---

## Key design decisions (locked)

- **Groups are `principals` rows with `type='group'`.** `create_group(name)` inserts `('group', name, name)`. The `external_id` is the human name (unique per type). Users are `type='user'` principals already created by `create_user`/`_backfill_acl`.
- **The `public` group is protected.** `delete_group` and `remove_user_from_group` refuse to touch `external_id='public'` (raises `ValueError`); the API maps that to HTTP 400. This preserves the "everyone sees legacy docs" invariant.
- **Grants are idempotent.** `grant(document_id, principal_id, permission)` uses `INSERT OR IGNORE`; `revoke` is a plain `DELETE`. Both validate `permission IN ('read','write')`.
- **`can_write` mirrors the read subquery** but on `permission='write'` plus owner. It returns a `bool` (not SQL) because callers are single-document mutation endpoints, not list queries.
- **Write-enforcement is additive, not breaking for the common case.** Legacy docs have `owner_principal_id IS NULL` and (today) no `write` ACL rows, so *no* user would pass `can_write`. To avoid locking everyone out of existing functionality, enforcement is gated on `documents.owner_principal_id IS NOT NULL` **OR** the existence of any `write` ACL row for that document: if a document has neither an owner nor any write grant, it is treated as unmanaged and mutation is allowed for any authenticated user (preserving today's behaviour). Admins always pass. This rule lives entirely inside `can_write` so there is one place to reason about it.
- **ACL-safe responses.** List endpoints return principal `id`, `type`, `display_name`/`external_id` and `permission` — never password hashes or session data. The document-ACL list joins through `principals` only.

---

## Task 1: `SqliteStore` group CRUD + membership helpers

**Files:**
- Modify: `document_search/index/sqlite_store.py` (add methods after `_backfill_acl`, ~line 240)
- Test: `tests/test_acl_management.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_acl_management.py`:

```python
from pathlib import Path
import pytest
from document_search.index.sqlite_store import SqliteStore


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "test.db")


def test_create_group_returns_principal_id(store):
    gid = store.create_group("editors", display_name="Editors")
    row = store.conn.execute(
        "SELECT type, external_id, display_name FROM principals WHERE id=?", (gid,)
    ).fetchone()
    assert row["type"] == "group"
    assert row["external_id"] == "editors"
    assert row["display_name"] == "Editors"


def test_create_group_is_idempotent_on_name(store):
    a = store.create_group("editors")
    b = store.create_group("editors")
    assert a == b  # same principal, no duplicate


def test_list_groups_excludes_users(store):
    store.create_group("editors")
    store.create_user("alice", "pw-alice")  # creates a 'user' principal
    names = {g["external_id"] for g in store.list_groups()}
    assert "editors" in names
    assert "public" in names      # from backfill
    assert "alice" not in names   # 'user' principals are not groups


def test_delete_group_removes_principal(store):
    gid = store.create_group("temp")
    store.delete_group(gid)
    row = store.conn.execute("SELECT id FROM principals WHERE id=?", (gid,)).fetchone()
    assert row is None


def test_delete_public_group_is_refused(store):
    public_id = store.conn.execute(
        "SELECT id FROM principals WHERE type='group' AND external_id='public'"
    ).fetchone()["id"]
    with pytest.raises(ValueError, match="public"):
        store.delete_group(public_id)


def test_add_and_remove_user_from_group(store):
    alice = store.create_user("alice", "pw")
    gid = store.create_group("editors")
    store.add_user_to_group(alice, gid)
    row = store.conn.execute(
        "SELECT 1 FROM user_groups WHERE user_id=? AND principal_id=?", (alice, gid)
    ).fetchone()
    assert row is not None
    store.remove_user_from_group(alice, gid)
    row = store.conn.execute(
        "SELECT 1 FROM user_groups WHERE user_id=? AND principal_id=?", (alice, gid)
    ).fetchone()
    assert row is None


def test_add_user_to_group_idempotent(store):
    alice = store.create_user("alice", "pw")
    gid = store.create_group("editors")
    store.add_user_to_group(alice, gid)
    store.add_user_to_group(alice, gid)  # must not raise on duplicate PK
    n = store.conn.execute(
        "SELECT COUNT(*) FROM user_groups WHERE user_id=? AND principal_id=?", (alice, gid)
    ).fetchone()[0]
    assert n == 1


def test_remove_from_public_group_is_refused(store):
    alice = store.create_user("alice", "pw")  # already in public
    public_id = store.conn.execute(
        "SELECT id FROM principals WHERE type='group' AND external_id='public'"
    ).fetchone()["id"]
    with pytest.raises(ValueError, match="public"):
        store.remove_user_from_group(alice, public_id)


def test_list_group_members(store):
    alice = store.create_user("alice", "pw")
    bob = store.create_user("bob", "pw")
    gid = store.create_group("editors")
    store.add_user_to_group(alice, gid)
    members = store.list_group_members(gid)
    usernames = {m["username"] for m in members}
    assert usernames == {"alice"}
    assert all("password_hash" not in m for m in members)  # ACL-safe shape


def test_create_group_rejects_user_type_clash(store):
    """A group named like an existing user principal is fine — type differs."""
    store.create_user("shared", "pw")
    gid = store.create_group("shared")  # type='group', distinct from type='user'
    row = store.conn.execute("SELECT type FROM principals WHERE id=?", (gid,)).fetchone()
    assert row["type"] == "group"
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_acl_management.py -v
```

Expected: `AttributeError: 'SqliteStore' object has no attribute 'create_group'`.

- [ ] **Step 3: Implement the helpers**

In `document_search/index/sqlite_store.py`, immediately after the `_backfill_acl` method (it ends at the line `self.conn.commit()` around line 240, before `def get_document`), insert:

```python
    # ── ACL management: groups & membership ────────────────────────────

    def create_group(self, name: str, display_name: str | None = None) -> int:
        """Create (or return existing) a 'group'-type principal. Idempotent on name."""
        now = datetime.now(tz=UTC).isoformat()
        self.conn.execute(
            "INSERT OR IGNORE INTO principals(type, external_id, display_name, created_at) "
            "VALUES('group', ?, ?, ?)",
            (name, display_name or name, now),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM principals WHERE type='group' AND external_id=?", (name,)
        ).fetchone()
        return int(row["id"])

    def list_groups(self) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT p.id, p.external_id, p.display_name, p.created_at,
                   (SELECT COUNT(*) FROM user_groups ug WHERE ug.principal_id = p.id) AS member_count
            FROM principals p
            WHERE p.type='group'
            ORDER BY p.external_id
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_group(self, principal_id: int) -> None:
        row = self.conn.execute(
            "SELECT type, external_id FROM principals WHERE id=?", (principal_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Group not found")
        if row["type"] != "group":
            raise ValueError("Principal is not a group")
        if row["external_id"] == "public":
            raise ValueError("The 'public' group cannot be deleted")
        # ON DELETE CASCADE clears user_groups + document_acl rows for this principal.
        self.conn.execute("DELETE FROM principals WHERE id=?", (principal_id,))
        self.conn.commit()

    def add_user_to_group(self, user_id: int, principal_id: int) -> None:
        grp = self.conn.execute(
            "SELECT type FROM principals WHERE id=?", (principal_id,)
        ).fetchone()
        if grp is None or grp["type"] != "group":
            raise ValueError("Target principal is not a group")
        if self.get_user_by_id(user_id) is None:
            raise ValueError("User not found")
        self.conn.execute(
            "INSERT OR IGNORE INTO user_groups(user_id, principal_id) VALUES(?, ?)",
            (user_id, principal_id),
        )
        self.conn.commit()

    def remove_user_from_group(self, user_id: int, principal_id: int) -> None:
        row = self.conn.execute(
            "SELECT external_id FROM principals WHERE id=?", (principal_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Group not found")
        if row["external_id"] == "public":
            raise ValueError("Membership in the 'public' group cannot be removed")
        self.conn.execute(
            "DELETE FROM user_groups WHERE user_id=? AND principal_id=?",
            (user_id, principal_id),
        )
        self.conn.commit()

    def list_group_members(self, principal_id: int) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT u.id AS user_id, u.username, u.role
            FROM user_groups ug
            JOIN users u ON u.id = ug.user_id
            WHERE ug.principal_id = ?
            ORDER BY u.username
            """,
            (principal_id,),
        ).fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_acl_management.py -v
```

Expected: 10 passing.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: all previously-passing tests still pass + 10 new.

- [ ] **Step 6: Commit**

```powershell
git add document_search/index/sqlite_store.py tests/test_acl_management.py
git commit -m @'
feat(acl): SqliteStore group CRUD and membership helpers

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 2: `SqliteStore` grant / revoke / list document ACLs

**Files:**
- Modify: `document_search/index/sqlite_store.py` (add methods after `list_group_members`)
- Test: `tests/test_acl_management.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_acl_management.py`:

```python
from datetime import UTC, datetime


def _insert_doc(store, path="/d/x.pdf", sha="h1"):
    now = datetime.now(tz=UTC).isoformat()
    cur = store.conn.execute(
        "INSERT INTO documents(path, filename, extension, file_size, modified_at, "
        "sha256, indexed_at, status) VALUES(?,?,?,?,?,?,?,?)",
        (path, Path(path).name, ".pdf", 1, now, sha, now, "ok"),
    )
    store.conn.commit()
    return cur.lastrowid


def test_grant_inserts_acl_row(store):
    doc = _insert_doc(store)
    gid = store.create_group("editors")
    store.grant(doc, gid, "write")
    row = store.conn.execute(
        "SELECT permission FROM document_acl WHERE document_id=? AND principal_id=?",
        (doc, gid),
    ).fetchone()
    assert row["permission"] == "write"


def test_grant_is_idempotent(store):
    doc = _insert_doc(store)
    gid = store.create_group("editors")
    store.grant(doc, gid, "read")
    store.grant(doc, gid, "read")
    n = store.conn.execute(
        "SELECT COUNT(*) FROM document_acl WHERE document_id=? AND principal_id=? AND permission='read'",
        (doc, gid),
    ).fetchone()[0]
    assert n == 1


def test_grant_rejects_bad_permission(store):
    doc = _insert_doc(store)
    gid = store.create_group("editors")
    with pytest.raises(ValueError, match="permission"):
        store.grant(doc, gid, "execute")


def test_grant_rejects_unknown_document(store):
    gid = store.create_group("editors")
    with pytest.raises(ValueError, match="Document"):
        store.grant(999999, gid, "read")


def test_grant_rejects_unknown_principal(store):
    doc = _insert_doc(store)
    with pytest.raises(ValueError, match="Principal"):
        store.grant(doc, 999999, "read")


def test_revoke_removes_acl_row(store):
    doc = _insert_doc(store)
    gid = store.create_group("editors")
    store.grant(doc, gid, "write")
    store.revoke(doc, gid, "write")
    row = store.conn.execute(
        "SELECT 1 FROM document_acl WHERE document_id=? AND principal_id=? AND permission='write'",
        (doc, gid),
    ).fetchone()
    assert row is None


def test_list_document_acl_returns_principal_details(store):
    doc = _insert_doc(store)
    alice = store.create_user("alice", "pw")
    alice_p = store.conn.execute(
        "SELECT principal_id FROM users WHERE id=?", (alice,)
    ).fetchone()["principal_id"]
    gid = store.create_group("editors", display_name="Editors")
    store.grant(doc, alice_p, "read")
    store.grant(doc, gid, "write")
    entries = store.list_document_acl(doc)
    # public read also present from upsert path? No — _insert_doc bypasses upsert_document,
    # so only the two explicit grants exist here.
    by_perm = {(e["principal_type"], e["permission"]) for e in entries}
    assert ("user", "read") in by_perm
    assert ("group", "write") in by_perm
    editor = next(e for e in entries if e["principal_id"] == gid)
    assert editor["display_name"] == "Editors"
    assert all("password_hash" not in e for e in entries)
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_acl_management.py -k "grant or revoke or list_document_acl" -v
```

Expected: `AttributeError: 'SqliteStore' object has no attribute 'grant'`.

- [ ] **Step 3: Implement the helpers**

In `document_search/index/sqlite_store.py`, immediately after `list_group_members`, insert:

```python
    # ── ACL management: document grants ────────────────────────────────

    _VALID_PERMISSIONS = ("read", "write")

    def grant(self, document_id: int, principal_id: int, permission: str) -> None:
        if permission not in self._VALID_PERMISSIONS:
            raise ValueError(f"permission must be one of {self._VALID_PERMISSIONS}")
        if self.conn.execute(
            "SELECT 1 FROM documents WHERE id=?", (document_id,)
        ).fetchone() is None:
            raise ValueError("Document not found")
        if self.conn.execute(
            "SELECT 1 FROM principals WHERE id=?", (principal_id,)
        ).fetchone() is None:
            raise ValueError("Principal not found")
        now = datetime.now(tz=UTC).isoformat()
        self.conn.execute(
            "INSERT OR IGNORE INTO document_acl(document_id, principal_id, permission, granted_at) "
            "VALUES(?, ?, ?, ?)",
            (document_id, principal_id, permission, now),
        )
        self.conn.commit()

    def revoke(self, document_id: int, principal_id: int, permission: str) -> None:
        if permission not in self._VALID_PERMISSIONS:
            raise ValueError(f"permission must be one of {self._VALID_PERMISSIONS}")
        self.conn.execute(
            "DELETE FROM document_acl WHERE document_id=? AND principal_id=? AND permission=?",
            (document_id, principal_id, permission),
        )
        self.conn.commit()

    def list_document_acl(self, document_id: int) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT a.principal_id, p.type AS principal_type, p.external_id,
                   p.display_name, a.permission, a.granted_at
            FROM document_acl a
            JOIN principals p ON p.id = a.principal_id
            WHERE a.document_id = ?
            ORDER BY p.type, p.external_id, a.permission
            """,
            (document_id,),
        ).fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_acl_management.py -v
```

Expected: 10 (Task 1) + 8 (Task 2) = 18 passing.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: green.

- [ ] **Step 6: Commit**

```powershell
git add document_search/index/sqlite_store.py tests/test_acl_management.py
git commit -m @'
feat(acl): SqliteStore grant/revoke/list document ACL helpers

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 3: `acl_service.can_write(user_id, document_id)`

**Files:**
- Modify: `document_search/services/acl_service.py` (add `can_write`)
- Test: `tests/test_acl_management.py` (extend)

The signature takes a `conn` (the live SQLite connection) so the service stays the place that owns ACL SQL — it does not import `SqliteStore` to avoid a cycle.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_acl_management.py`:

```python
from document_search.services.acl_service import can_write


def _doc_with_owner(store, owner_principal_id, path="/d/owned.pdf"):
    now = datetime.now(tz=UTC).isoformat()
    cur = store.conn.execute(
        "INSERT INTO documents(path, filename, extension, file_size, modified_at, "
        "sha256, indexed_at, status, owner_principal_id) VALUES(?,?,?,?,?,?,?,?,?)",
        (path, Path(path).name, ".pdf", 1, now, "hO", now, "ok", owner_principal_id),
    )
    store.conn.commit()
    return cur.lastrowid


def test_owner_can_write(store):
    alice = store.create_user("alice", "pw")
    alice_p = store.conn.execute(
        "SELECT principal_id FROM users WHERE id=?", (alice,)
    ).fetchone()["principal_id"]
    doc = _doc_with_owner(store, alice_p)
    assert can_write(store.conn, alice, doc) is True


def test_non_owner_without_grant_cannot_write_owned_doc(store):
    alice = store.create_user("alice", "pw")
    bob = store.create_user("bob", "pw")
    alice_p = store.conn.execute(
        "SELECT principal_id FROM users WHERE id=?", (alice,)
    ).fetchone()["principal_id"]
    doc = _doc_with_owner(store, alice_p)
    assert can_write(store.conn, bob, doc) is False


def test_direct_write_grant_allows_write(store):
    alice = store.create_user("alice", "pw")
    bob = store.create_user("bob", "pw")
    alice_p = store.conn.execute(
        "SELECT principal_id FROM users WHERE id=?", (alice,)
    ).fetchone()["principal_id"]
    bob_p = store.conn.execute(
        "SELECT principal_id FROM users WHERE id=?", (bob,)
    ).fetchone()["principal_id"]
    doc = _doc_with_owner(store, alice_p)
    store.grant(doc, bob_p, "write")
    assert can_write(store.conn, bob, doc) is True


def test_group_write_grant_allows_write(store):
    alice = store.create_user("alice", "pw")
    bob = store.create_user("bob", "pw")
    alice_p = store.conn.execute(
        "SELECT principal_id FROM users WHERE id=?", (alice,)
    ).fetchone()["principal_id"]
    doc = _doc_with_owner(store, alice_p)
    gid = store.create_group("editors")
    store.add_user_to_group(bob, gid)
    store.grant(doc, gid, "write")
    assert can_write(store.conn, bob, doc) is True


def test_read_grant_does_not_allow_write(store):
    alice = store.create_user("alice", "pw")
    bob = store.create_user("bob", "pw")
    alice_p = store.conn.execute(
        "SELECT principal_id FROM users WHERE id=?", (alice,)
    ).fetchone()["principal_id"]
    bob_p = store.conn.execute(
        "SELECT principal_id FROM users WHERE id=?", (bob,)
    ).fetchone()["principal_id"]
    doc = _doc_with_owner(store, alice_p)
    store.grant(doc, bob_p, "read")
    assert can_write(store.conn, bob, doc) is False


def test_unmanaged_document_is_writable_by_anyone(store):
    """Legacy doc: no owner, no write grants -> treated as unmanaged, writable."""
    bob = store.create_user("bob", "pw")
    doc = _insert_doc(store, path="/d/legacy.pdf", sha="hL")  # no owner_principal_id
    assert can_write(store.conn, bob, doc) is True


def test_managed_by_write_grant_locks_out_others(store):
    """Once ANY write grant exists, the doc is 'managed' and others are locked out."""
    alice = store.create_user("alice", "pw")
    bob = store.create_user("bob", "pw")
    alice_p = store.conn.execute(
        "SELECT principal_id FROM users WHERE id=?", (alice,)
    ).fetchone()["principal_id"]
    doc = _insert_doc(store, path="/d/managed.pdf", sha="hM")  # no owner
    store.grant(doc, alice_p, "write")
    assert can_write(store.conn, alice, doc) is True
    assert can_write(store.conn, bob, doc) is False
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_acl_management.py -k can_write -v
```

Expected: `ImportError: cannot import name 'can_write'`.

- [ ] **Step 3: Implement `can_write`**

Append to `document_search/services/acl_service.py`:

```python
def can_write(conn, user_id: int, document_id: int) -> bool:
    """Return True if `user_id` may mutate `document_id`.

    A user may write if ANY of:
    1. They own the document (`documents.owner_principal_id` == their 'user' principal).
    2. A 'write' ACL row grants them directly or via a group they belong to.
    3. The document is UNMANAGED: it has no owner AND no 'write' ACL rows at all
       (legacy behaviour — every authenticated user could mutate it before ACLs
       were enforced). The first owner assignment or write grant flips it to
       managed and locks out everyone else.

    `conn` is a live sqlite3 connection (e.g. `store.conn`). The function never
    raises on a missing document — it returns False.
    """
    doc = conn.execute(
        "SELECT owner_principal_id FROM documents WHERE id=?", (document_id,)
    ).fetchone()
    if doc is None:
        return False
    owner_principal_id = doc["owner_principal_id"]

    user_principal = conn.execute(
        "SELECT principal_id FROM users WHERE id=?", (user_id,)
    ).fetchone()
    user_principal_id = user_principal["principal_id"] if user_principal else None

    # Rule 1: owner.
    if owner_principal_id is not None and owner_principal_id == user_principal_id:
        return True

    # Rule 2: explicit write grant (direct principal or via group membership).
    granted = conn.execute(
        """
        SELECT 1 FROM document_acl a
        WHERE a.document_id = ?
          AND a.permission = 'write'
          AND (
            a.principal_id = ?
            OR a.principal_id IN (
              SELECT g.principal_id FROM user_groups g WHERE g.user_id = ?
            )
          )
        LIMIT 1
        """,
        (document_id, user_principal_id, user_id),
    ).fetchone()
    if granted is not None:
        return True

    # Rule 3: unmanaged document (no owner AND no write grants at all).
    if owner_principal_id is None:
        any_write = conn.execute(
            "SELECT 1 FROM document_acl WHERE document_id=? AND permission='write' LIMIT 1",
            (document_id,),
        ).fetchone()
        if any_write is None:
            return True

    return False
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_acl_management.py -v
```

Expected: 18 + 7 = 25 passing.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: green. Confirms the read subquery (`visible_document_ids_subquery`) is untouched.

- [ ] **Step 6: Commit**

```powershell
git add document_search/services/acl_service.py tests/test_acl_management.py
git commit -m @'
feat(acl): can_write helper mirroring read filter for write permission

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 4: `/api/groups/*` REST endpoints

**Files:**
- Modify: `document_search/app.py` (Pydantic models near line 205; routes after the users routes, ~line 1130)
- Test: `tests/test_app_acl.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_app_acl.py`:

```python
import pytest

pytestmark = pytest.mark.skipif(
    not pytest.importorskip("fastapi", reason="fastapi not installed"),
    reason="fastapi required",
)

from fastapi.testclient import TestClient
from document_search.app import create_app


def _login(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _create_user(client, admin_token, username, password="pw-123456", role="user"):
    r = client.post(
        "/api/users",
        headers={"X-Auth-Token": admin_token},
        json={"username": username, "password": password, "role": role},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_create_list_delete_group(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        tok = _login(client)
        r = client.post("/api/groups", headers={"X-Auth-Token": tok},
                        json={"name": "editors", "display_name": "Editors"})
        assert r.status_code == 200, r.text
        gid = r.json()["id"]
        assert isinstance(gid, int)

        r = client.get("/api/groups", headers={"X-Auth-Token": tok})
        assert r.status_code == 200
        names = {g["external_id"] for g in r.json()}
        assert "editors" in names and "public" in names

        r = client.delete(f"/api/groups/{gid}", headers={"X-Auth-Token": tok})
        assert r.status_code == 200
        r = client.get("/api/groups", headers={"X-Auth-Token": tok})
        assert "editors" not in {g["external_id"] for g in r.json()}


def test_delete_public_group_returns_400(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        tok = _login(client)
        public_id = next(
            g["id"] for g in client.get("/api/groups", headers={"X-Auth-Token": tok}).json()
            if g["external_id"] == "public"
        )
        r = client.delete(f"/api/groups/{public_id}", headers={"X-Auth-Token": tok})
        assert r.status_code == 400


def test_group_membership_add_remove_list(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        tok = _login(client)
        uid = _create_user(client, tok, "alice")
        gid = client.post("/api/groups", headers={"X-Auth-Token": tok},
                          json={"name": "editors"}).json()["id"]

        r = client.post(f"/api/groups/{gid}/members",
                        headers={"X-Auth-Token": tok}, json={"user_id": uid})
        assert r.status_code == 200, r.text

        r = client.get(f"/api/groups/{gid}/members", headers={"X-Auth-Token": tok})
        assert r.status_code == 200
        assert {m["username"] for m in r.json()} == {"alice"}

        r = client.delete(f"/api/groups/{gid}/members/{uid}", headers={"X-Auth-Token": tok})
        assert r.status_code == 200
        r = client.get(f"/api/groups/{gid}/members", headers={"X-Auth-Token": tok})
        assert r.json() == []


def test_groups_require_admin(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        admin = _login(client)
        _create_user(client, admin, "bob")
        bob = _login(client, "bob", "pw-123456")
        r = client.get("/api/groups", headers={"X-Auth-Token": bob})
        assert r.status_code == 403
        r = client.post("/api/groups", headers={"X-Auth-Token": bob}, json={"name": "x"})
        assert r.status_code == 403
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_acl.py -v
```

Expected: 404s (routes not registered yet).

- [ ] **Step 3: Add Pydantic models**

In `document_search/app.py`, after the `TagsRequest` class (~line 205), add:

```python
class GroupCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    display_name: str | None = None


class GroupMemberRequest(BaseModel):
    user_id: int


class GrantRequest(BaseModel):
    document_id: int
    principal_id: int
    permission: str  # 'read' | 'write'
```

- [ ] **Step 4: Register the group routes**

In `document_search/app.py`, immediately after `api_change_password` (the user route block ends at ~line 1130), add:

```python
    # ── Groups (admin) ─────────────────────────────────────────────────

    @app.get("/api/groups")
    def api_list_groups(x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        return store().list_groups()

    @app.post("/api/groups")
    def api_create_group(req: GroupCreateRequest, x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        name = req.name.strip().lower()
        if not name:
            raise HTTPException(status_code=400, detail="Group name must not be empty")
        gid = store().create_group(name, req.display_name)
        return {"id": gid, "name": name, "display_name": req.display_name or name}

    @app.delete("/api/groups/{principal_id}")
    def api_delete_group(principal_id: int, x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        try:
            store().delete_group(principal_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"status": "deleted"}

    @app.get("/api/groups/{principal_id}/members")
    def api_list_group_members(principal_id: int, x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        return store().list_group_members(principal_id)

    @app.post("/api/groups/{principal_id}/members")
    def api_add_group_member(
        principal_id: int,
        req: GroupMemberRequest,
        x_auth_token: str | None = Header(default=None),
    ):
        require_admin(x_auth_token)
        try:
            store().add_user_to_group(req.user_id, principal_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"status": "added"}

    @app.delete("/api/groups/{principal_id}/members/{user_id}")
    def api_remove_group_member(
        principal_id: int,
        user_id: int,
        x_auth_token: str | None = Header(default=None),
    ):
        require_admin(x_auth_token)
        try:
            store().remove_user_from_group(user_id, principal_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"status": "removed"}
```

- [ ] **Step 5: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_acl.py -v
```

Expected: 4 passing.

- [ ] **Step 6: Full suite + commit**

```powershell
$env:PYTHONPATH = "."; pytest -q
git add document_search/app.py tests/test_app_acl.py
git commit -m @'
feat(acl): /api/groups CRUD and membership endpoints (admin)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 5: `/api/acl/*` grant / revoke / list endpoints

**Files:**
- Modify: `document_search/app.py` (routes after the group routes)
- Test: `tests/test_app_acl.py` (extend)

`GET /api/acl/documents/{id}` is the one read-only route an *owner* may also call; grant/revoke stay admin-only.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_app_acl.py`:

```python
from datetime import UTC, datetime
from pathlib import Path
from document_search.index.sqlite_store import SqliteStore


def _seed_doc(db_path, path="/d/seed.pdf"):
    store = SqliteStore(Path(db_path))
    now = datetime.now(tz=UTC).isoformat()
    cur = store.conn.execute(
        "INSERT INTO documents(path, filename, extension, file_size, modified_at, "
        "sha256, indexed_at, status) VALUES(?,?,?,?,?,?,?,?)",
        (path, Path(path).name, ".pdf", 1, now, "hS", now, "ok"),
    )
    store.conn.commit()
    return cur.lastrowid


def test_grant_and_list_document_acl(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    doc = _seed_doc(db)
    with TestClient(app) as client:
        tok = _login(client)
        gid = client.post("/api/groups", headers={"X-Auth-Token": tok},
                          json={"name": "editors"}).json()["id"]
        r = client.post("/api/acl/grant", headers={"X-Auth-Token": tok},
                        json={"document_id": doc, "principal_id": gid, "permission": "write"})
        assert r.status_code == 200, r.text

        r = client.get(f"/api/acl/documents/{doc}", headers={"X-Auth-Token": tok})
        assert r.status_code == 200
        entries = r.json()
        assert any(e["principal_id"] == gid and e["permission"] == "write" for e in entries)


def test_revoke_document_acl(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    doc = _seed_doc(db)
    with TestClient(app) as client:
        tok = _login(client)
        gid = client.post("/api/groups", headers={"X-Auth-Token": tok},
                          json={"name": "editors"}).json()["id"]
        client.post("/api/acl/grant", headers={"X-Auth-Token": tok},
                    json={"document_id": doc, "principal_id": gid, "permission": "read"})
        r = client.post("/api/acl/revoke", headers={"X-Auth-Token": tok},
                        json={"document_id": doc, "principal_id": gid, "permission": "read"})
        assert r.status_code == 200
        entries = client.get(f"/api/acl/documents/{doc}", headers={"X-Auth-Token": tok}).json()
        assert not any(e["principal_id"] == gid for e in entries)


def test_grant_bad_permission_returns_400(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    doc = _seed_doc(db)
    with TestClient(app) as client:
        tok = _login(client)
        gid = client.post("/api/groups", headers={"X-Auth-Token": tok},
                          json={"name": "editors"}).json()["id"]
        r = client.post("/api/acl/grant", headers={"X-Auth-Token": tok},
                        json={"document_id": doc, "principal_id": gid, "permission": "execute"})
        assert r.status_code == 400


def test_grant_unknown_document_returns_400(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    with TestClient(app) as client:
        tok = _login(client)
        gid = client.post("/api/groups", headers={"X-Auth-Token": tok},
                          json={"name": "editors"}).json()["id"]
        r = client.post("/api/acl/grant", headers={"X-Auth-Token": tok},
                        json={"document_id": 999999, "principal_id": gid, "permission": "read"})
        assert r.status_code == 400


def test_acl_grant_requires_admin(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    doc = _seed_doc(db)
    with TestClient(app) as client:
        admin = _login(client)
        _create_user(client, admin, "bob")
        gid = client.post("/api/groups", headers={"X-Auth-Token": admin},
                          json={"name": "editors"}).json()["id"]
        bob = _login(client, "bob", "pw-123456")
        r = client.post("/api/acl/grant", headers={"X-Auth-Token": bob},
                        json={"document_id": doc, "principal_id": gid, "permission": "read"})
        assert r.status_code == 403


def test_acl_list_404_for_unknown_document(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    with TestClient(app) as client:
        tok = _login(client)
        r = client.get("/api/acl/documents/999999", headers={"X-Auth-Token": tok})
        assert r.status_code == 404
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_acl.py -k "acl or grant or revoke" -v
```

- [ ] **Step 3: Register the ACL routes**

In `document_search/app.py`, immediately after the group routes added in Task 4, add:

```python
    # ── Document ACLs ──────────────────────────────────────────────────

    @app.get("/api/acl/documents/{document_id}")
    def api_list_document_acl(document_id: int, x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        db = store()
        doc = db.get_document_by_id(document_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        # Admin OR document owner may inspect the ACL.
        is_admin = (db.get_user_by_id(user_id) or {})["role"] == "admin"
        if not is_admin:
            owner_pid = doc["owner_principal_id"] if "owner_principal_id" in doc.keys() else None
            user_pid = db.conn.execute(
                "SELECT principal_id FROM users WHERE id=?", (user_id,)
            ).fetchone()
            user_pid = user_pid["principal_id"] if user_pid else None
            if owner_pid is None or owner_pid != user_pid:
                raise HTTPException(status_code=403, detail="Not allowed")
        return db.list_document_acl(document_id)

    @app.post("/api/acl/grant")
    def api_acl_grant(req: GrantRequest, x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        try:
            store().grant(req.document_id, req.principal_id, req.permission)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"status": "granted"}

    @app.post("/api/acl/revoke")
    def api_acl_revoke(req: GrantRequest, x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        try:
            store().revoke(req.document_id, req.principal_id, req.permission)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"status": "revoked"}
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_acl.py -v
```

Expected: 4 (Task 4) + 6 = 10 passing.

- [ ] **Step 5: Full suite + commit**

```powershell
$env:PYTHONPATH = "."; pytest -q
git add document_search/app.py tests/test_app_acl.py
git commit -m @'
feat(acl): /api/acl grant/revoke/list document permission endpoints

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 6: Enforce `can_write` on document-mutating endpoints

**Files:**
- Modify: `document_search/app.py` — `api_reindex_document` (~line 622) and the move loop inside `api_ai_reorganize_apply` (~line 1513)
- Test: `tests/test_app_acl.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_app_acl.py`:

```python
def _seed_doc_with_owner(db_path, owner_username_principal, path):
    store = SqliteStore(Path(db_path))
    now = datetime.now(tz=UTC).isoformat()
    cur = store.conn.execute(
        "INSERT INTO documents(path, filename, extension, file_size, modified_at, "
        "sha256, indexed_at, status, owner_principal_id) VALUES(?,?,?,?,?,?,?,?,?)",
        (path, Path(path).name, ".txt", 1, now, "hW", now, "ok", owner_username_principal),
    )
    store.conn.commit()
    return cur.lastrowid


def test_reindex_rejected_for_non_owner_without_write(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    with TestClient(app) as client:
        admin = _login(client)
        alice_id = _create_user(client, admin, "alice")
        _create_user(client, admin, "bob")
        # owner = alice's principal
        store = SqliteStore(db)
        alice_p = store.conn.execute(
            "SELECT principal_id FROM users WHERE id=?", (alice_id,)
        ).fetchone()["principal_id"]
        # Write an on-disk file so the path exists (reindex checks existence first,
        # but can_write must reject BEFORE extraction). Use a real temp file.
        f = tmp_path / "owned.txt"
        f.write_text("hello", encoding="utf-8")
        doc = _seed_doc_with_owner(db, alice_p, str(f))

        bob = _login(client, "bob", "pw-123456")
        r = client.post(f"/api/documents/{doc}/reindex", headers={"X-Auth-Token": bob})
        assert r.status_code == 403, r.text


def test_reindex_allowed_for_owner(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    with TestClient(app) as client:
        admin = _login(client)
        alice_id = _create_user(client, admin, "alice")
        store = SqliteStore(db)
        alice_p = store.conn.execute(
            "SELECT principal_id FROM users WHERE id=?", (alice_id,)
        ).fetchone()["principal_id"]
        f = tmp_path / "owned2.txt"
        f.write_text("hello world", encoding="utf-8")
        doc = _seed_doc_with_owner(db, alice_p, str(f))

        alice = _login(client, "alice", "pw-123456")
        r = client.post(f"/api/documents/{doc}/reindex", headers={"X-Auth-Token": alice})
        assert r.status_code == 200, r.text


def test_reindex_allowed_for_admin(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    with TestClient(app) as client:
        admin = _login(client)
        alice_id = _create_user(client, admin, "alice")
        store = SqliteStore(db)
        alice_p = store.conn.execute(
            "SELECT principal_id FROM users WHERE id=?", (alice_id,)
        ).fetchone()["principal_id"]
        f = tmp_path / "owned3.txt"
        f.write_text("admin can reindex", encoding="utf-8")
        doc = _seed_doc_with_owner(db, alice_p, str(f))
        r = client.post(f"/api/documents/{doc}/reindex", headers={"X-Auth-Token": admin})
        assert r.status_code == 200, r.text


def test_reindex_allowed_for_unmanaged_legacy_doc(tmp_path):
    """No owner, no write grants -> any authenticated user may reindex (legacy)."""
    db = tmp_path / "t.db"
    app = create_app(str(db))
    with TestClient(app) as client:
        admin = _login(client)
        _create_user(client, admin, "bob")
        f = tmp_path / "legacy.txt"
        f.write_text("legacy", encoding="utf-8")
        doc = _seed_doc(db, path=str(f))  # no owner
        bob = _login(client, "bob", "pw-123456")
        r = client.post(f"/api/documents/{doc}/reindex", headers={"X-Auth-Token": bob})
        assert r.status_code == 200, r.text
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_acl.py -k reindex -v
```

Expected: `test_reindex_rejected_for_non_owner_without_write` fails with 200 (no enforcement yet).

- [ ] **Step 3: Enforce in `api_reindex_document`**

In `document_search/app.py`, locate `api_reindex_document` (~line 622). Insert the `can_write` check right after the document-existence check and before the on-disk existence check. The current body is:

```python
    @app.post("/api/documents/{document_id}/reindex")
    def api_reindex_document(document_id: int, x_auth_token: str | None = Header(default=None)):
        require_user(x_auth_token)
        db = store()
        doc = db.get_document_by_id(document_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        p = Path(doc["path"])
```

Replace it with:

```python
    @app.post("/api/documents/{document_id}/reindex")
    def api_reindex_document(document_id: int, x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        db = store()
        doc = db.get_document_by_id(document_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        is_admin = (db.get_user_by_id(user_id) or {})["role"] == "admin"
        if not is_admin and not can_write(db.conn, user_id, document_id):
            raise HTTPException(status_code=403, detail="You do not have write access to this document")
        p = Path(doc["path"])
```

Add the import near the top of `document_search/app.py` with the other service imports (search for `from document_search.services` or, if none, add after the existing `from document_search.index...` imports):

```python
from document_search.services.acl_service import can_write
```

- [ ] **Step 4: Enforce in the move loop**

In `api_ai_reorganize_apply` (the loop around line 1513), inside `for item in req.moves:`, after the `doc` lookup and the `not doc` guard, add a per-item write check. Locate:

```python
        for item in req.moves:
            doc = db.get_document_by_id(item.document_id)
            if not doc:
                results.append({"document_id": item.document_id, "status": "not_found"})
                continue
```

Replace with (this route is `require_admin` today; capture the admin id and still call `can_write` for symmetry — admins always pass, but the check documents intent and stays correct if the route is ever relaxed):

```python
        for item in req.moves:
            doc = db.get_document_by_id(item.document_id)
            if not doc:
                results.append({"document_id": item.document_id, "status": "not_found"})
                continue
            if not is_admin_apply and not can_write(db.conn, apply_user_id, item.document_id):
                results.append({"document_id": item.document_id, "status": "forbidden"})
                continue
```

At the top of `api_ai_reorganize_apply`, change the auth line from `require_admin(x_auth_token)` to capture the id and admin flag. Find the function's first lines (the `require_admin(...)` call and the `db = store()` setup) and ensure they read:

```python
        apply_user_id = require_admin(x_auth_token)
        db = store()
        is_admin_apply = True  # require_admin guarantees admin; kept explicit for can_write parity
```

(If the existing function already binds `db = store()` differently, keep its structure and only add `apply_user_id` + `is_admin_apply`. Do not change the move/shutil logic.)

- [ ] **Step 5: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_acl.py -v
```

Expected: 10 (Tasks 4-5) + 4 = 14 passing.

- [ ] **Step 6: Full suite + commit**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

If `tests/test_app_search.py` or any existing test that reindexes a doc now fails with 403: it inserted a doc with an owner but logged in as a different non-admin user. Adjust that test to either log in as admin or grant write — do NOT weaken `can_write`. Most existing tests use the admin session and are unaffected.

```powershell
git add document_search/app.py tests/test_app_acl.py
git commit -m @'
feat(acl): enforce can_write on reindex and reorganize-move endpoints

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 7: Config → Access tab (HTML)

**Files:**
- Modify: `document_search/web/templates/config.html` (tab button after the Users button ~line 84; panel after the Users panel ~line 367)

- [ ] **Step 1: Add the tab button**

In `document_search/web/templates/config.html`, immediately after the **Users** tab `<button>` block (the one with `data-tab="users"`, closing `</button>` at ~line 84), insert:

```html
          <button class="tab admin-only hidden" onclick="switchTab('access')" data-tab="access">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><circle cx="12" cy="16" r="1"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg>
            Access
          </button>
```

- [ ] **Step 2: Add the panel**

Immediately after the closing `</div>` of the Users panel (`<div id="tab-users" ...>` ends at ~line 367, the line `</div>` just before `<!-- ─── Tab: SSL ─── -->`), insert:

```html
        <!-- ─── Tab: Access (ACL) ─── -->
        <div id="tab-access" class="tab-panel hidden admin-only">

          <div class="card">
            <div class="card-head">
              <div class="card-ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/></svg></div>
              <div class="card-titles"><h2>Groups</h2><p>Create groups and manage their members</p></div>
            </div>
            <div class="card-body">
              <div id="groupTable" class="u-table-wrap"></div>
              <p id="groupsResult" class="feedback"></p>
              <div class="f-grid" style="margin-top:12px;">
                <div class="f-col">
                  <label class="f-label" for="newGroupName">Group name</label>
                  <input id="newGroupName" placeholder="editors" autocomplete="off" />
                </div>
                <div class="f-col">
                  <label class="f-label" for="newGroupLabel">Display name (optional)</label>
                  <input id="newGroupLabel" placeholder="Editors" autocomplete="off" />
                </div>
              </div>
              <div class="btn-row">
                <button class="btn btn-p" onclick="createGroup()">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
                  Create Group
                </button>
              </div>
            </div>
          </div>

          <!-- Members of the selected group (hidden until a group is chosen) -->
          <div id="groupMembersCard" class="card hidden">
            <div class="card-head">
              <div class="card-ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="7" r="4"/><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/></svg></div>
              <div class="card-titles"><h2 id="groupMembersTitle">Members</h2><p>Add or remove users from this group</p></div>
            </div>
            <div class="card-body">
              <input type="hidden" id="selectedGroupId" />
              <div id="groupMemberTable" class="u-table-wrap"></div>
              <div class="f-grid" style="margin-top:12px; grid-template-columns:1fr auto;">
                <div class="f-col">
                  <label class="f-label" for="addMemberSelect">Add user</label>
                  <select id="addMemberSelect"></select>
                </div>
                <div class="f-col" style="align-self:end;">
                  <button class="btn btn-p" onclick="addGroupMember()">Add</button>
                </div>
              </div>
              <p id="groupMembersResult" class="feedback"></p>
            </div>
          </div>

          <div class="card">
            <div class="card-head">
              <div class="card-ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg></div>
              <div class="card-titles"><h2>Document Access</h2><p>Grant or revoke read/write on a single document</p></div>
            </div>
            <div class="card-body">
              <div class="f-grid" style="grid-template-columns:1fr auto;">
                <div class="f-col">
                  <label class="f-label" for="aclDocId">Document ID</label>
                  <input id="aclDocId" type="number" placeholder="e.g. 42" />
                </div>
                <div class="f-col" style="align-self:end;">
                  <button class="btn btn-g" onclick="loadDocumentAcl()">Load ACL</button>
                </div>
              </div>
              <div id="docAclTable" class="u-table-wrap" style="margin-top:12px;"></div>
              <div class="f-grid" style="margin-top:12px; grid-template-columns:1fr 1fr auto;">
                <div class="f-col">
                  <label class="f-label" for="aclPrincipalSelect">Principal</label>
                  <select id="aclPrincipalSelect"></select>
                </div>
                <div class="f-col">
                  <label class="f-label" for="aclPermSelect">Permission</label>
                  <select id="aclPermSelect">
                    <option value="read">Read</option>
                    <option value="write">Write</option>
                  </select>
                </div>
                <div class="f-col" style="align-self:end;">
                  <button class="btn btn-p" onclick="grantDocumentAcl()">Grant</button>
                </div>
              </div>
              <p id="docAclResult" class="feedback"></p>
            </div>
          </div>

        </div>
```

- [ ] **Step 3: Verify the page still renders**

```powershell
$env:PYTHONPATH = "."; python -c "from document_search.app import create_app; from fastapi.testclient import TestClient; c=TestClient(create_app('build_check.db')); r=c.get('/config'); print(r.status_code); assert r.status_code==200; assert 'tab-access' in r.text"
```

Expected: prints `200`. (Delete `build_check.db` afterwards: `Remove-Item build_check.db* -Force`.)

- [ ] **Step 4: Commit**

```powershell
git add document_search/web/templates/config.html
git commit -m @'
feat(acl): add Config Access tab markup for groups and document ACLs

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 8: Config → Access tab (JS)

**Files:**
- Modify: `document_search/web/static/app.js` — wire `switchTab`, add the Access functions next to the Users functions (after `cancelChangePassword`, ~line 1090+)

- [ ] **Step 1: Wire the tab into `switchTab`**

In `document_search/web/static/app.js`, in `switchTab` (~line 853), add a line next to the existing tab loaders:

```javascript
  if (name === 'users') loadUsers();
  if (name === 'access') loadAccessTab();
  if (name === 'ssl') loadSslStatus();
```

(Insert the `access` line immediately after the `users` line.)

- [ ] **Step 2: Add the Access functions**

Append the following block to `document_search/web/static/app.js` (after the user-management functions, e.g. after `cancelChangePassword`). These mirror the Users-tab patterns: `api()` for requests, `showToast` + `setText` for feedback, `escHtml` for safe rendering.

```javascript
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
    await loadAccessTab();  // refresh member counts
    document.getElementById('selectedGroupId').value = gid;  // preserve selection
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
  // We need each principal's id. Groups already expose `id` (the principal_id).
  // For users we expose them as "user:<userId>" and resolve to principal server-side?
  // The grant API takes principal_id directly, so we fetch the user principal map.
  const groupOpts = groups.map(g =>
    `<option value="${g.id}">Group: ${escHtml(g.display_name || g.external_id)}</option>`).join('');
  // Users: principal_id is not in /api/users; load it lazily via the document ACL view
  // is unnecessary — we add a dedicated principals endpoint? To keep scope tight, the
  // grant UI targets GROUPS only; per-user grants use the group of one or the document
  // owner. Show groups here.
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
```

> **Note on the principal select:** the grant UI targets **groups** because `/api/users` does not expose each user's `principal_id`, and adding a user-principal lookup endpoint is out of scope for this plan. Per-user document sharing is achieved by granting to a group (a group-of-one works). The document-ACL list still renders user-type entries that were granted elsewhere (e.g. via owner backfill), so revoke works for any principal type.

- [ ] **Step 3: Manual smoke test (optional but recommended)**

```powershell
$env:PYTHONPATH = "."; uvicorn document_search.app:app --port 8080
```

Open `http://localhost:8080/config`, log in as admin, click **Access**. Create a group, open its members, add/remove a user, then load a document's ACL (use ID `1` after indexing something) and grant/revoke to the group. Confirm toasts fire and tables refresh.

- [ ] **Step 4: Full suite (JS isn't covered by pytest, but confirm backend untouched)**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: green (no regressions).

- [ ] **Step 5: Commit**

```powershell
git add document_search/web/static/app.js
git commit -m @'
feat(acl): Access tab JS for groups, membership, and document grants

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 9: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Full suite, clean**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: every test passes, zero failures, no flaky reruns. New tests added by this plan: `tests/test_acl_management.py` (25) + `tests/test_app_acl.py` (14).

- [ ] **Step 2: End-to-end smoke via TestClient**

```powershell
$env:PYTHONPATH = "."; python -c "
from fastapi.testclient import TestClient
from document_search.app import create_app
from document_search.index.sqlite_store import SqliteStore
from datetime import UTC, datetime
import tempfile, pathlib
tmp = pathlib.Path(tempfile.mkdtemp())
db = tmp / 'acl.db'
# seed a document directly
s = SqliteStore(db); now = datetime.now(tz=UTC).isoformat()
cur = s.conn.execute('INSERT INTO documents(path,filename,extension,file_size,modified_at,sha256,indexed_at,status) VALUES(?,?,?,?,?,?,?,?)',('/d/a.pdf','a.pdf','.pdf',1,now,'h',now,'ok')); s.conn.commit(); doc=cur.lastrowid; s.conn.close()
app = create_app(str(db))
with TestClient(app) as c:
    tok = c.post('/api/login', json={'username':'admin','password':'admin'}).json()['token']
    H = {'X-Auth-Token': tok}
    gid = c.post('/api/groups', headers=H, json={'name':'editors','display_name':'Editors'}).json()['id']
    print('group', gid)
    uid = c.post('/api/users', headers=H, json={'username':'alice','password':'pw-123456','role':'user'}).json()['id']
    assert c.post(f'/api/groups/{gid}/members', headers=H, json={'user_id':uid}).status_code==200
    assert {m['username'] for m in c.get(f'/api/groups/{gid}/members', headers=H).json()}=={'alice'}
    assert c.post('/api/acl/grant', headers=H, json={'document_id':doc,'principal_id':gid,'permission':'write'}).status_code==200
    acl = c.get(f'/api/acl/documents/{doc}', headers=H).json()
    assert any(e['principal_id']==gid and e['permission']=='write' for e in acl)
    assert c.post('/api/acl/revoke', headers=H, json={'document_id':doc,'principal_id':gid,'permission':'write'}).status_code==200
    assert c.delete(f'/api/groups/{gid}', headers=H).status_code==200
print('OK')
"
```

Expected: prints `group <id>`, then `OK`.

- [ ] **Step 3: Confirm read filter unchanged**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_acl_service.py tests/test_search_acl.py
```

Expected: green — this plan added no change to `visible_document_ids_subquery`.

- [ ] **Step 4: No code changes in this task; no commit.**

---

## Definition of Done

- [ ] `$env:PYTHONPATH = "."; pytest -q` is green on a clean clone, including the 25 tests in `tests/test_acl_management.py` and 14 in `tests/test_app_acl.py`.
- [ ] `SqliteStore` exposes `create_group`, `list_groups`, `delete_group`, `add_user_to_group`, `remove_user_from_group`, `list_group_members`, `grant`, `revoke`, `list_document_acl` — and they are the only place ACL tables are written outside `_backfill_acl`/`create_user`/`upsert_document`.
- [ ] The `public` group cannot be deleted, and membership in it cannot be removed (400 at the API).
- [ ] `acl_service.can_write(conn, user_id, document_id)` returns owner ✔, write-grant ✔ (direct or via group), read-only ✘, unmanaged-legacy ✔, managed-by-others ✘.
- [ ] `/api/groups/*` (list/create/delete/members) and `/api/acl/*` (grant/revoke/list) exist; all `require_admin` except `GET /api/acl/documents/{id}` which also permits the document owner.
- [ ] `/api/documents/{id}/reindex` and the reorganize-move path reject non-owners without write (403 / `"forbidden"`), while admins, owners, write-grantees, and unmanaged legacy docs pass.
- [ ] Config → **Access** tab exists, is admin-only, loads lazily on `switchTab('access')`, and supports group CRUD, membership, and per-document grant/revoke with toast feedback — following the Users-tab pattern.
- [ ] `visible_document_ids_subquery` and the read-side tests (`test_acl_service.py`, `test_search_acl.py`) are unchanged and still green.
- [ ] Manual smoke test (Task 9 Step 2) passes.

---

## Notes for the executing agent

- **Why `can_write` takes a `conn`, not a `SqliteStore`:** `acl_service` must not import `SqliteStore` (that module imports nothing from `services`, and a reverse import risks a cycle). Passing the live connection keeps the SQL in `acl_service` while letting callers use `db.conn`. The read helper `visible_document_ids_subquery` returns SQL because it is composed into list queries; `can_write` returns a bool because callers are single-document mutation endpoints — this asymmetry is intentional.
- **The "unmanaged document" rule is the migration-safety hinge.** Without it, enforcing `can_write` on legacy data (owner NULL, no write grants) would 403 every non-admin user on every reindex/move — a breaking change. The rule says: a document is *managed* the moment it gets an owner or any write grant; until then it behaves exactly as before. Keep this logic inside `can_write` only — do not duplicate it in the routes.
- **Group `external_id` is lowercased at the API** (`req.name.strip().lower()`) so group names are case-insensitive and idempotent. The store layer trusts its input; normalisation is the route's job (mirrors how usernames flow today).
- **`UNIQUE(type, external_id)` lets a group and a user share a name** (different `type`). `test_create_group_rejects_user_type_clash` documents this — it's allowed, not an error.
- **`delete_group` relies on `ON DELETE CASCADE`** (defined in the schema for both `user_groups.principal_id` and `document_acl.principal_id`). `PRAGMA foreign_keys=ON` is set in `_configure_connection`, so the cascade fires. If a future change disables FK enforcement, `delete_group` must delete dependents explicitly.
- **Per-user document grants from the UI are intentionally routed through groups.** `/api/users` does not expose `principal_id`, and adding a `/api/principals` endpoint was out of scope. A group-of-one is the documented workaround. If a later plan adds per-user sharing, expose `principal_id` on the users list and extend `populateAclPrincipalSelect` to include `type='user'` principals — the grant API already accepts any `principal_id`.
- **The reorganize-apply route stays `require_admin`.** The added `can_write` check is belt-and-suspenders: `is_admin_apply` is always `True` there today, so the check is a no-op for the current auth, but it keeps the mutation-enforcement contract uniform and correct if the route is ever relaxed to non-admins. Do not remove it.
- **JS has no automated test.** Verify the Access tab manually (Task 8 Step 3 / Task 9 Step 2 cover the backend it calls). Keep every DOM write going through `escHtml` for user-supplied strings (group names, usernames) — the Users tab is the reference for this.
- **Do not touch `visible_document_ids_subquery`.** Read-side ACL is a settled contract from the ACL-foundation plan; this plan is purely additive (write management + UI).
