# ACL Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a principal/group/ACL layer to Seekr so every search and document fetch is filtered through a single `visible_document_ids` subquery. Existing single-tenant behaviour is preserved via a `public` group all current users belong to.

**Architecture:** Three new SQLite tables (`principals`, `user_groups`, `document_acl`) plus an `owner_principal_id` column on `documents`. One new service module `acl_service.py` returns a parameterised SQL fragment. `search_service.search()` and `_browse_all()` inject that fragment into every query. CLI search keeps working through an explicit `bypass_acl=True` flag (which the web API never sets). All schema changes are additive and run inside the existing `try/except` migration pattern in `SqliteStore._init_schema`.

**Tech Stack:** Python 3.11, SQLite (FTS5), pytest. No new third-party dependencies.

**Scope boundaries (out of scope for this plan, picked up by later plans):**
- No Nextcloud connector, no WebDAV — `owner_principal_id` is set only by the migration here; new ingestion paths will populate it later.
- No write-API to manage ACLs (no `/api/acl/*` endpoints). Created via direct DB calls in tests only.
- No admin bypass UI; admins go through the same ACL like everyone else (admin role bypass is a per-call boolean flag).

---

## File Structure

**Create:**
- `document_search/services/acl_service.py` — single source of truth for ACL filter SQL.
- `tests/test_acl_service.py` — unit tests for the SQL builder and CRUD helpers.
- `tests/test_search_acl.py` — integration tests: search returns only visible docs.

**Modify:**
- `document_search/index/sqlite_store.py` — new tables, new columns, ACL CRUD helpers, migration block at end of `_init_schema`.
- `document_search/index/search_service.py` — add `user_id` requirement, `bypass_acl` flag, inject ACL filter into both code paths (`search` FTS branch and `_browse_all`).
- `document_search/main.py:63` — CLI passes `bypass_acl=True`.
- `document_search/app.py:1497` — `api_search` already has `user_id`; no signature change but verify it's passed.

**Test fixtures touched:**
- `tests/test_search_service.py` — no changes (only tests `build_match_query`, untouched).
- `tests/test_app_search.py` — verify it still passes; existing test users go through migration which auto-grants `public`-group membership so behaviour is preserved.

---

## Task 1: Schema for principals, groups, ACLs

**Files:**
- Modify: `document_search/index/sqlite_store.py:29-127` (extend `_init_schema`)
- Test: `tests/test_acl_service.py` (new)

- [ ] **Step 1: Write the failing test for table existence**

Append to a new file `tests/test_acl_service.py`:

```python
from pathlib import Path
import pytest
from document_search.index.sqlite_store import SqliteStore


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "test.db")


def test_acl_tables_exist(store):
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('principals','user_groups','document_acl')"
    ).fetchall()
    names = {r[0] for r in rows}
    assert names == {"principals", "user_groups", "document_acl"}


def test_documents_has_owner_principal_id(store):
    cols = store.conn.execute("PRAGMA table_info(documents)").fetchall()
    names = {c[1] for c in cols}
    assert "owner_principal_id" in names


def test_users_has_principal_id(store):
    cols = store.conn.execute("PRAGMA table_info(users)").fetchall()
    names = {c[1] for c in cols}
    assert "principal_id" in names
```

- [ ] **Step 2: Run test, expect FAIL**

Run: `pytest -q tests/test_acl_service.py::test_acl_tables_exist -v`
Expected: FAIL with empty `names` set (tables not yet created).

- [ ] **Step 3: Add schema in `_init_schema`**

In `document_search/index/sqlite_store.py`, inside the `self.conn.executescript("""...""")` block in `_init_schema` (around line 30–112), **append** the following just before the closing `"""` of the executescript (i.e. after the last `CREATE INDEX` line at ~line 110):

```sql
CREATE TABLE IF NOT EXISTS principals (
  id INTEGER PRIMARY KEY,
  type TEXT NOT NULL CHECK(type IN ('user','group')),
  external_id TEXT NOT NULL,
  display_name TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(type, external_id)
);
CREATE TABLE IF NOT EXISTS user_groups (
  user_id INTEGER NOT NULL,
  principal_id INTEGER NOT NULL,
  PRIMARY KEY (user_id, principal_id),
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  FOREIGN KEY (principal_id) REFERENCES principals(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS document_acl (
  document_id INTEGER NOT NULL,
  principal_id INTEGER NOT NULL,
  permission TEXT NOT NULL CHECK(permission IN ('read','write')),
  granted_at TEXT NOT NULL,
  PRIMARY KEY (document_id, principal_id, permission),
  FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE,
  FOREIGN KEY (principal_id) REFERENCES principals(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_acl_doc        ON document_acl(document_id);
CREATE INDEX IF NOT EXISTS idx_acl_principal  ON document_acl(principal_id);
CREATE INDEX IF NOT EXISTS idx_user_groups_pid ON user_groups(principal_id);
```

Then, **after** the existing `try/except` block that adds the `role` column (lines 113–118), append two further additive migrations following the exact same pattern:

```python
        # Migration: add owner_principal_id column for existing databases
        try:
            self.conn.execute("ALTER TABLE documents ADD COLUMN owner_principal_id INTEGER")
            self.conn.commit()
        except Exception:
            pass
        # Migration: add principal_id column on users (links user -> their 'user' principal)
        try:
            self.conn.execute("ALTER TABLE users ADD COLUMN principal_id INTEGER")
            self.conn.commit()
        except Exception:
            pass
```

- [ ] **Step 4: Run test, expect PASS**

Run: `pytest -q tests/test_acl_service.py -v`
Expected: all three tests pass.

- [ ] **Step 5: Commit**

```powershell
git add document_search/index/sqlite_store.py tests/test_acl_service.py
git commit -m "feat(acl): add principals, user_groups, document_acl tables"
```

---

## Task 2: Backfill migration — every user gets a principal, every doc gets a `public` ACL

**Files:**
- Modify: `document_search/index/sqlite_store.py` (extend `_init_schema`, add helper method `_backfill_acl`)
- Test: `tests/test_acl_service.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_acl_service.py`:

```python
def test_backfill_creates_public_group(store):
    row = store.conn.execute(
        "SELECT id FROM principals WHERE type='group' AND external_id='public'"
    ).fetchone()
    assert row is not None


def test_backfill_creates_user_principal_for_existing_users(store):
    store.create_user("alice", "alice-password", role="user")
    # Re-open store to trigger migration on existing user row
    store2 = SqliteStore(store.db_path)
    user_row = store2.conn.execute(
        "SELECT principal_id FROM users WHERE username='alice'"
    ).fetchone()
    assert user_row["principal_id"] is not None
    p = store2.conn.execute(
        "SELECT type, external_id FROM principals WHERE id=?",
        (user_row["principal_id"],),
    ).fetchone()
    assert p["type"] == "user"
    assert p["external_id"] == "alice"


def test_backfill_adds_existing_users_to_public_group(store):
    store.create_user("bob", "bob-password", role="user")
    store2 = SqliteStore(store.db_path)
    public_id = store2.conn.execute(
        "SELECT id FROM principals WHERE type='group' AND external_id='public'"
    ).fetchone()["id"]
    bob_id = store2.conn.execute("SELECT id FROM users WHERE username='bob'").fetchone()["id"]
    row = store2.conn.execute(
        "SELECT 1 FROM user_groups WHERE user_id=? AND principal_id=?",
        (bob_id, public_id),
    ).fetchone()
    assert row is not None


def test_backfill_grants_public_read_to_existing_documents(store, tmp_path):
    # Insert a fake document directly (we don't need to extract anything for this test)
    from datetime import UTC, datetime
    now = datetime.now(tz=UTC).isoformat()
    store.conn.execute(
        "INSERT INTO documents(path, filename, extension, file_size, modified_at, "
        "sha256, indexed_at, status) VALUES(?,?,?,?,?,?,?,?)",
        ("/tmp/old.pdf", "old.pdf", ".pdf", 100, now, "deadbeef", now, "ok"),
    )
    store.conn.commit()
    store2 = SqliteStore(store.db_path)
    public_id = store2.conn.execute(
        "SELECT id FROM principals WHERE type='group' AND external_id='public'"
    ).fetchone()["id"]
    doc_id = store2.conn.execute("SELECT id FROM documents WHERE path='/tmp/old.pdf'").fetchone()["id"]
    row = store2.conn.execute(
        "SELECT permission FROM document_acl WHERE document_id=? AND principal_id=?",
        (doc_id, public_id),
    ).fetchone()
    assert row is not None
    assert row["permission"] == "read"
```

- [ ] **Step 2: Run tests, expect FAIL**

Run: `pytest -q tests/test_acl_service.py -v`
Expected: the four new tests fail (no `public` group, `principal_id` NULL on users, no ACL rows).

- [ ] **Step 3: Implement backfill in `SqliteStore`**

In `document_search/index/sqlite_store.py`, **append** the call `self._backfill_acl()` at the very end of `_init_schema` (after the existing admin-ensure block, around line 126), then add this new method directly below `_init_schema`:

```python
    def _backfill_acl(self) -> None:
        """Idempotent backfill so existing data stays visible after ACL migration.

        Creates the 'public' group, ensures every existing user has a 'user'-type
        principal and is a member of 'public', and grants read on every existing
        document to 'public'. Safe to run on every startup.
        """
        from datetime import UTC, datetime
        now = datetime.now(tz=UTC).isoformat()

        self.conn.execute(
            "INSERT OR IGNORE INTO principals(type, external_id, display_name, created_at) "
            "VALUES('group', 'public', 'Everyone', ?)",
            (now,),
        )
        public_row = self.conn.execute(
            "SELECT id FROM principals WHERE type='group' AND external_id='public'"
        ).fetchone()
        public_id = public_row["id"]

        users_without_principal = self.conn.execute(
            "SELECT id, username FROM users WHERE principal_id IS NULL"
        ).fetchall()
        for u in users_without_principal:
            self.conn.execute(
                "INSERT OR IGNORE INTO principals(type, external_id, display_name, created_at) "
                "VALUES('user', ?, ?, ?)",
                (u["username"], u["username"], now),
            )
            p = self.conn.execute(
                "SELECT id FROM principals WHERE type='user' AND external_id=?",
                (u["username"],),
            ).fetchone()
            self.conn.execute(
                "UPDATE users SET principal_id=? WHERE id=?", (p["id"], u["id"])
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO user_groups(user_id, principal_id) VALUES(?, ?)",
                (u["id"], public_id),
            )

        # Grant 'public' read on every existing document that has no ACL yet
        self.conn.execute(
            """
            INSERT OR IGNORE INTO document_acl(document_id, principal_id, permission, granted_at)
            SELECT d.id, ?, 'read', ?
            FROM documents d
            WHERE NOT EXISTS (SELECT 1 FROM document_acl a WHERE a.document_id = d.id)
            """,
            (public_id, now),
        )
        self.conn.commit()
```

Also extend `create_user` (currently at ~line 203) to register a principal for new users. Replace the existing method body with:

```python
    def create_user(self, username: str, password: str, role: str = "user") -> int:
        salt = new_salt()
        now = datetime.now(tz=UTC).isoformat()
        cursor = self.conn.execute(
            "INSERT INTO users(username, password_hash, salt, created_at, role) VALUES(?,?,?,?,?)",
            (username, hash_password(password, salt), salt, now, role),
        )
        user_id = cursor.lastrowid
        self.conn.execute(
            "INSERT OR IGNORE INTO principals(type, external_id, display_name, created_at) "
            "VALUES('user', ?, ?, ?)",
            (username, username, now),
        )
        p_row = self.conn.execute(
            "SELECT id FROM principals WHERE type='user' AND external_id=?", (username,)
        ).fetchone()
        self.conn.execute(
            "UPDATE users SET principal_id=? WHERE id=?", (p_row["id"], user_id)
        )
        public_row = self.conn.execute(
            "SELECT id FROM principals WHERE type='group' AND external_id='public'"
        ).fetchone()
        if public_row:
            self.conn.execute(
                "INSERT OR IGNORE INTO user_groups(user_id, principal_id) VALUES(?, ?)",
                (user_id, public_row["id"]),
            )
        self.conn.commit()
        return user_id
```

- [ ] **Step 4: Run tests, expect PASS**

Run: `pytest -q tests/test_acl_service.py -v`
Expected: all seven tests pass.

- [ ] **Step 5: Verify existing test suite still passes**

Run: `pytest -q`
Expected: all previously-passing tests still pass (the new tables and backfill are transparent to existing queries).

- [ ] **Step 6: Commit**

```powershell
git add document_search/index/sqlite_store.py tests/test_acl_service.py
git commit -m "feat(acl): backfill principals and public-group ACL for legacy data"
```

---

## Task 3: `acl_service.visible_document_ids_subquery`

**Files:**
- Create: `document_search/services/acl_service.py`
- Test: `tests/test_acl_service.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_acl_service.py`:

```python
from document_search.services.acl_service import visible_document_ids_subquery


def test_subquery_returns_sql_and_params(store):
    sql, params = visible_document_ids_subquery(user_id=1)
    assert "SELECT" in sql.upper()
    assert isinstance(params, list)
    # Subquery must yield a column called document_id
    assert "document_id" in sql.lower()


def test_subquery_yields_only_user_visible_docs(store):
    from datetime import UTC, datetime
    now = datetime.now(tz=UTC).isoformat()

    alice_id = store.create_user("alice", "pw-alice")
    bob_id = store.create_user("bob", "pw-bob")

    # Insert two docs, no ACL rows beyond migration default (which gives 'public')
    store.conn.execute(
        "INSERT INTO documents(path, filename, extension, file_size, modified_at, sha256, indexed_at, status) "
        "VALUES('/d/a.pdf','a.pdf','.pdf',1,?, 'h1', ?, 'ok')", (now, now),
    )
    store.conn.execute(
        "INSERT INTO documents(path, filename, extension, file_size, modified_at, sha256, indexed_at, status) "
        "VALUES('/d/b.pdf','b.pdf','.pdf',1,?, 'h2', ?, 'ok')", (now, now),
    )
    store.conn.commit()
    # Trigger backfill so the two new docs get 'public' read ACL
    store2 = SqliteStore(store.db_path)

    # Now revoke 'public' read on doc b and grant it only to alice
    bob_row = store2.conn.execute("SELECT principal_id FROM users WHERE username='bob'").fetchone()
    alice_row = store2.conn.execute("SELECT principal_id FROM users WHERE username='alice'").fetchone()
    public_row = store2.conn.execute(
        "SELECT id FROM principals WHERE type='group' AND external_id='public'"
    ).fetchone()
    b_doc = store2.conn.execute("SELECT id FROM documents WHERE path='/d/b.pdf'").fetchone()["id"]
    store2.conn.execute("DELETE FROM document_acl WHERE document_id=? AND principal_id=?", (b_doc, public_row["id"]))
    store2.conn.execute(
        "INSERT INTO document_acl(document_id, principal_id, permission, granted_at) VALUES(?,?, 'read', ?)",
        (b_doc, alice_row["principal_id"], now),
    )
    store2.conn.commit()

    sql, params = visible_document_ids_subquery(alice_id)
    alice_visible = {r[0] for r in store2.conn.execute(f"SELECT document_id FROM ({sql})", params).fetchall()}
    sql, params = visible_document_ids_subquery(bob_id)
    bob_visible = {r[0] for r in store2.conn.execute(f"SELECT document_id FROM ({sql})", params).fetchall()}

    a_doc = store2.conn.execute("SELECT id FROM documents WHERE path='/d/a.pdf'").fetchone()["id"]
    assert a_doc in alice_visible and a_doc in bob_visible  # public
    assert b_doc in alice_visible                            # direct grant
    assert b_doc not in bob_visible                          # no grant


def test_subquery_owner_always_visible(store):
    from datetime import UTC, datetime
    now = datetime.now(tz=UTC).isoformat()
    alice_id = store.create_user("alice", "pw")
    alice_p = store.conn.execute(
        "SELECT principal_id FROM users WHERE id=?", (alice_id,)
    ).fetchone()["principal_id"]

    store.conn.execute(
        "INSERT INTO documents(path, filename, extension, file_size, modified_at, sha256, indexed_at, status, owner_principal_id) "
        "VALUES('/d/owned.pdf','owned.pdf','.pdf',1,?, 'h3', ?, 'ok', ?)",
        (now, now, alice_p),
    )
    store.conn.commit()
    # No ACL rows on this doc at all
    sql, params = visible_document_ids_subquery(alice_id)
    visible = {r[0] for r in store.conn.execute(f"SELECT document_id FROM ({sql})", params).fetchall()}
    owned_id = store.conn.execute("SELECT id FROM documents WHERE path='/d/owned.pdf'").fetchone()["id"]
    assert owned_id in visible
```

- [ ] **Step 2: Run tests, expect FAIL**

Run: `pytest -q tests/test_acl_service.py -v`
Expected: import error (`acl_service` module doesn't exist yet).

- [ ] **Step 3: Implement `acl_service.py`**

Create `document_search/services/acl_service.py`:

```python
"""Single source of truth for ACL-aware retrieval.

All search/listing code paths MUST filter through `visible_document_ids_subquery`.
Skipping this filter is allowed only via an explicit `bypass_acl=True` flag in
the caller (CLI/system contexts), and never from HTTP request handlers.
"""
from __future__ import annotations


def visible_document_ids_subquery(user_id: int) -> tuple[str, list]:
    """Return a SQL fragment + bound parameters yielding `document_id` rows
    that the given user is allowed to read.

    Visibility rules (OR'd together):
    1. The user is the owner: `documents.owner_principal_id` equals the user's
       own 'user'-type principal.
    2. An ACL row grants 'read' to a principal the user is a member of (via
       `user_groups`) or to the user's own principal directly.

    The returned SQL is intended to be embedded as `... WHERE d.id IN (<sql>)`.
    """
    sql = """
        SELECT d.id AS document_id
        FROM documents d
        LEFT JOIN users u ON u.id = ?
        WHERE
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
    """
    return sql, [user_id, user_id]
```

- [ ] **Step 4: Run tests, expect PASS**

Run: `pytest -q tests/test_acl_service.py -v`
Expected: all ten tests pass.

- [ ] **Step 5: Commit**

```powershell
git add document_search/services/acl_service.py tests/test_acl_service.py
git commit -m "feat(acl): visible_document_ids_subquery as central ACL filter"
```

---

## Task 4: Apply ACL filter in `search_service.search` (FTS branch)

**Files:**
- Modify: `document_search/index/search_service.py:90-145`
- Test: `tests/test_search_acl.py` (new)

- [ ] **Step 1: Write the failing integration test**

Create `tests/test_search_acl.py`:

```python
from datetime import UTC, datetime
from pathlib import Path
import pytest
from document_search.index.sqlite_store import SqliteStore
from document_search.index.search_service import search


@pytest.fixture
def populated_store(tmp_path):
    store = SqliteStore(tmp_path / "test.db")
    alice_id = store.create_user("alice", "pw")
    bob_id = store.create_user("bob", "pw")
    now = datetime.now(tz=UTC).isoformat()

    # Insert two docs, both indexed in FTS
    for path, sha, body in [
        ("/d/alice-secret.txt", "h1", "secret recipe for soup"),
        ("/d/shared.txt", "h2", "shared recipe for soup"),
    ]:
        cursor = store.conn.execute(
            "INSERT INTO documents(path, filename, extension, file_size, modified_at, sha256, indexed_at, status) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (path, Path(path).name, ".txt", len(body), now, sha, now, "ok"),
        )
        doc_id = cursor.lastrowid
        bcursor = store.conn.execute(
            "INSERT INTO content_blocks(document_id, block_type, block_number, text, extractor, text_length) "
            "VALUES(?,?,?,?,?,?)",
            (doc_id, "paragraph", 1, body, "txt", len(body)),
        )
        store.conn.execute(
            "INSERT INTO content_fts(document_id, block_id, path, filename, extension, block_type, block_number, text) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (doc_id, bcursor.lastrowid, path, Path(path).name, ".txt", "paragraph", "1", body),
        )
    store.conn.commit()
    # Re-open to run backfill so both docs get 'public' read
    store2 = SqliteStore(store.db_path)

    # Restrict alice-secret.txt to alice only
    public_id = store2.conn.execute(
        "SELECT id FROM principals WHERE type='group' AND external_id='public'"
    ).fetchone()["id"]
    secret_id = store2.conn.execute(
        "SELECT id FROM documents WHERE path='/d/alice-secret.txt'"
    ).fetchone()["id"]
    alice_p = store2.conn.execute(
        "SELECT principal_id FROM users WHERE username='alice'"
    ).fetchone()["principal_id"]
    store2.conn.execute(
        "DELETE FROM document_acl WHERE document_id=? AND principal_id=?",
        (secret_id, public_id),
    )
    store2.conn.execute(
        "INSERT INTO document_acl(document_id, principal_id, permission, granted_at) "
        "VALUES(?,?, 'read', ?)",
        (secret_id, alice_p, now),
    )
    store2.conn.commit()
    return store2, alice_id, bob_id


def test_search_returns_only_visible_docs_for_bob(populated_store):
    store, alice_id, bob_id = populated_store
    rows = search(store, "recipe", user_id=bob_id)
    paths = {r["path"] for r in rows}
    assert "/d/shared.txt" in paths
    assert "/d/alice-secret.txt" not in paths


def test_search_returns_both_for_alice(populated_store):
    store, alice_id, bob_id = populated_store
    rows = search(store, "recipe", user_id=alice_id)
    paths = {r["path"] for r in rows}
    assert "/d/shared.txt" in paths
    assert "/d/alice-secret.txt" in paths


def test_search_without_user_id_raises(populated_store):
    store, _, _ = populated_store
    with pytest.raises(ValueError, match="user_id is required"):
        search(store, "recipe")


def test_search_bypass_acl_returns_all(populated_store):
    store, _, _ = populated_store
    rows = search(store, "recipe", user_id=None, bypass_acl=True)
    paths = {r["path"] for r in rows}
    assert "/d/shared.txt" in paths
    assert "/d/alice-secret.txt" in paths
```

- [ ] **Step 2: Run tests, expect FAIL**

Run: `pytest -q tests/test_search_acl.py -v`
Expected: all four tests fail — currently `search()` returns both rows regardless of user.

- [ ] **Step 3: Modify `search_service.search` signature**

In `document_search/index/search_service.py`, change the `search` function (lines 90–145). Replace the entire function body with:

```python
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
    bypass_acl: bool = False,
):
    if user_id is None and not bypass_acl:
        raise ValueError("user_id is required unless bypass_acl=True is set explicitly")

    tags = [t.lower().strip() for t in (tags or [])]
    match_query = build_match_query(query, filetype, block_type)

    if match_query is None:
        return _browse_all(
            store, filetype, path_filter, block_type,
            modified_from, modified_to, tags, user_id, limit,
            bypass_acl=bypass_acl,
        )

    # snippet() column 7 = text (0-based FTS5 index order in content_fts)
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
    if path_filter:
        sql += " AND d.path LIKE ?"
        params.append(path_filter + "%")
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
    if not bypass_acl:
        from document_search.services.acl_service import visible_document_ids_subquery
        acl_sql, acl_params = visible_document_ids_subquery(user_id)
        sql += f" AND d.id IN ({acl_sql})"
        params.extend(acl_params)
    sql += " ORDER BY c.rank LIMIT ?"
    params.append(limit)
    return store.conn.execute(sql, tuple(params)).fetchall()
```

- [ ] **Step 4: Run the FTS-branch tests, expect PASS for 2 of 4**

Run: `pytest -q tests/test_search_acl.py -v`
Expected: `test_search_returns_only_visible_docs_for_bob`, `test_search_returns_both_for_alice`, `test_search_without_user_id_raises`, `test_search_bypass_acl_returns_all` — all four should pass already because all four tests use a non-empty query that hits the FTS branch.

- [ ] **Step 5: Commit**

```powershell
git add document_search/index/search_service.py tests/test_search_acl.py
git commit -m "feat(acl): filter FTS search results by visible_document_ids"
```

---

## Task 5: Apply ACL filter in `_browse_all` (empty-query branch)

**Files:**
- Modify: `document_search/index/search_service.py:27-87`
- Test: `tests/test_search_acl.py` (extend)

- [ ] **Step 1: Write the failing test for the browse branch**

Append to `tests/test_search_acl.py`:

```python
def test_browse_all_respects_acl_for_bob(populated_store):
    """Empty query goes through _browse_all — must also be ACL-filtered."""
    store, alice_id, bob_id = populated_store
    rows = search(store, "", limit=100, user_id=bob_id)
    paths = {r["path"] for r in rows}
    assert "/d/shared.txt" in paths
    assert "/d/alice-secret.txt" not in paths


def test_browse_all_bypass_acl(populated_store):
    store, _, _ = populated_store
    rows = search(store, "", limit=100, user_id=None, bypass_acl=True)
    paths = {r["path"] for r in rows}
    assert "/d/shared.txt" in paths
    assert "/d/alice-secret.txt" in paths
```

- [ ] **Step 2: Run tests, expect FAIL**

Run: `pytest -q tests/test_search_acl.py::test_browse_all_respects_acl_for_bob -v`
Expected: FAIL — `_browse_all` currently returns both rows.

- [ ] **Step 3: Add `bypass_acl` param and ACL filter to `_browse_all`**

In `document_search/index/search_service.py`, replace `_browse_all` (lines 27–87) with:

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
    bypass_acl: bool = False,
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
    if not bypass_acl:
        from document_search.services.acl_service import visible_document_ids_subquery
        acl_sql, acl_params = visible_document_ids_subquery(user_id)
        where.append(f"d.id IN ({acl_sql})")
        params.extend(acl_params)

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
```

- [ ] **Step 4: Run tests, expect PASS**

Run: `pytest -q tests/test_search_acl.py -v`
Expected: all six tests pass.

- [ ] **Step 5: Commit**

```powershell
git add document_search/index/search_service.py tests/test_search_acl.py
git commit -m "feat(acl): apply ACL filter to browse-all (empty-query) path"
```

---

## Task 6: Update CLI caller to use `bypass_acl=True`

**Files:**
- Modify: `document_search/main.py:63`

- [ ] **Step 1: Verify CLI currently broken**

Run: `python -m document_search.main search "test" 2>&1 | head -5`
Expected: `ValueError: user_id is required unless bypass_acl=True is set explicitly` (because the CLI no longer passes a user_id).

> If the CLI cannot connect to a populated DB locally, skip this step and rely on the test from Step 2.

- [ ] **Step 2: Write a test that confirms the CLI behaviour**

Add to `tests/test_search_acl.py`:

```python
def test_cli_uses_bypass_acl():
    """document_search.main.cmd_search must pass bypass_acl=True to search()."""
    import inspect
    from document_search import main as main_mod
    src = inspect.getsource(main_mod.cmd_search)
    assert "bypass_acl=True" in src, "CLI search must explicitly bypass ACL"
```

- [ ] **Step 3: Run the test, expect FAIL**

Run: `pytest -q tests/test_search_acl.py::test_cli_uses_bypass_acl -v`
Expected: FAIL — current source has no `bypass_acl=True`.

- [ ] **Step 4: Modify `cmd_search` in `main.py`**

Change line 63 in `document_search/main.py` from:

```python
    rows = search(store, args.query, args.limit, args.filetype, args.path)
```

to:

```python
    rows = search(store, args.query, args.limit, args.filetype, args.path, bypass_acl=True)
```

- [ ] **Step 5: Run all tests, expect PASS**

Run: `pytest -q`
Expected: full suite passes including the new test.

- [ ] **Step 6: Commit**

```powershell
git add document_search/main.py tests/test_search_acl.py
git commit -m "feat(acl): CLI search uses explicit bypass_acl=True"
```

---

## Task 7: Sanity-check existing app integration (no code changes expected)

**Files:**
- Read-only: `document_search/app.py:1492-1499`

- [ ] **Step 1: Confirm `/api/search` already passes `user_id`**

Read `document_search/app.py:1492-1499`. The line should already be:

```python
rows = search(db, req.query, req.limit, req.filetype, req.path, req.block_type, req.modified_from, req.modified_to, req.tags, user_id)
```

No code change is required — the existing call satisfies the new mandatory `user_id` parameter.

- [ ] **Step 2: Run the existing integration test**

Run: `pytest -q tests/test_app_search.py -v`
Expected: all tests still pass. Existing test users are auto-added to the `public` group by the migration, and existing test documents get a `public` read-ACL, so ACL filtering is transparent.

If any test fails: read the failure carefully. The most likely cause is a test that directly inserts a document into the DB without going through `upsert_document` and without granting it a `public` ACL. In that case, the test must either:
1. Re-open the `SqliteStore` after inserting docs so `_backfill_acl` re-runs (recommended), or
2. Insert a `document_acl` row manually.

Do not weaken the ACL filter to make those tests pass.

- [ ] **Step 3: Commit (if any test fixture needed adjustment)**

If you had to adjust a test fixture, commit that as:

```powershell
git add tests/<file>.py
git commit -m "test(acl): adjust fixture to re-open store for backfill"
```

Otherwise, no commit.

---

## Task 8: Run the full suite, manually exercise the API

**Files:** none (verification only)

- [ ] **Step 1: Full suite**

Run: `pytest -q`
Expected: every test passes. Print a clean green run.

- [ ] **Step 2: Manual smoke test**

```powershell
# Start the server
uvicorn document_search.app:app --port 8080
```

In another shell:

```powershell
# Log in as admin (assuming default admin/admin)
$body = '{"username":"admin","password":"admin"}'
$resp = Invoke-RestMethod -Uri http://localhost:8080/api/login -Method POST -Body $body -ContentType 'application/json'
$token = $resp.token

# Search — should return results without 500/error
Invoke-RestMethod -Uri http://localhost:8080/api/search -Method POST `
  -Headers @{ "X-Auth-Token" = $token } `
  -Body '{"query":"the","limit":5}' -ContentType 'application/json'
```

Expected: HTTP 200 with results. No `ValueError`, no 500.

- [ ] **Step 3: Verify ACL by SQL**

```powershell
sqlite3 data/document_index.db "SELECT type, external_id FROM principals;"
sqlite3 data/document_index.db "SELECT COUNT(*) FROM document_acl;"
```

Expected: at least one `user` principal (admin) and one `group` principal (`public`). `document_acl` count equals the number of indexed documents.

- [ ] **Step 4: Final commit if no further changes**

No additional commit needed; this task is verification only.

---

## Definition of Done

- [ ] `pytest -q` is green on a clean clone.
- [ ] `principals`, `user_groups`, `document_acl` tables exist; `documents.owner_principal_id` and `users.principal_id` columns exist.
- [ ] Every existing user is a member of the `public` group; every existing document has a `public` read-ACL row.
- [ ] `search_service.search` and `_browse_all` reject `user_id=None` unless `bypass_acl=True`.
- [ ] All ACL-filter logic lives in `acl_service.visible_document_ids_subquery` — no other module builds an ACL SQL clause.
- [ ] CLI search in `main.py` uses `bypass_acl=True` explicitly.
- [ ] `/api/search` returns ACL-filtered results without code change.
- [ ] Manual smoke test passes.

---

## Notes for the executing agent

- **Reentrancy**: `_init_schema` and `_backfill_acl` are designed to be safe on every startup. Don't add migration "version" tracking — the `try/except` on `ALTER TABLE` and the `INSERT OR IGNORE` calls make every run idempotent.
- **Performance**: the ACL subquery joins three tables but each has an index (PK + the two new indexes added in Task 1). For the realistic scale of this project (tens of thousands of docs, dozens of users), this is cheap enough to not warrant materialising a `visible_docs(user_id, doc_id)` table yet. If a future profile shows this is hot, that's the next optimisation — but not in this plan.
- **`bypass_acl` is a security-critical flag**. Only the CLI sets it. Never expose it to any HTTP-bound route. If a future task adds a system/cron path that needs it, it must be reviewed against this contract.
- **Why `owner_principal_id` is nullable**: legacy documents have no clear owner. Future ingestion paths (Nextcloud connector) will always set it. The ACL filter handles NULL correctly because `NULL = NULL` is false in SQL — meaning owner-based visibility silently doesn't match, and ACL rows take over.
- **Why `bypass_acl` is a kwarg, not a separate function**: a separate `search_admin()` function would be cleaner, but the CLI's `cmd_search` already shares the same row shape with the web API, and splitting would duplicate the row-shape logic. The kwarg is the smaller diff.
