from pathlib import Path
import pytest
from document_search.index.sqlite_store import SqliteStore
from document_search.services.acl_service import visible_document_ids_subquery


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
