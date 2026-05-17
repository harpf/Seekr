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
