from datetime import UTC, datetime
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
    assert a == b


def test_list_groups_excludes_users(store):
    store.create_group("editors")
    store.create_user("alice", "pw-alice")
    names = {g["external_id"] for g in store.list_groups()}
    assert "editors" in names
    assert "public" in names
    assert "alice" not in names


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
    store.add_user_to_group(alice, gid)
    n = store.conn.execute(
        "SELECT COUNT(*) FROM user_groups WHERE user_id=? AND principal_id=?", (alice, gid)
    ).fetchone()[0]
    assert n == 1


def test_remove_from_public_group_is_refused(store):
    alice = store.create_user("alice", "pw")
    public_id = store.conn.execute(
        "SELECT id FROM principals WHERE type='group' AND external_id='public'"
    ).fetchone()["id"]
    with pytest.raises(ValueError, match="public"):
        store.remove_user_from_group(alice, public_id)


def test_list_group_members(store):
    alice = store.create_user("alice", "pw")
    store.create_user("bob", "pw")
    gid = store.create_group("editors")
    store.add_user_to_group(alice, gid)
    members = store.list_group_members(gid)
    usernames = {m["username"] for m in members}
    assert usernames == {"alice"}
    assert all("password_hash" not in m for m in members)


def test_create_group_rejects_user_type_clash(store):
    store.create_user("shared", "pw")
    gid = store.create_group("shared")
    row = store.conn.execute("SELECT type FROM principals WHERE id=?", (gid,)).fetchone()
    assert row["type"] == "group"


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
    by_perm = {(e["principal_type"], e["permission"]) for e in entries}
    assert ("user", "read") in by_perm
    assert ("group", "write") in by_perm
    editor = next(e for e in entries if e["principal_id"] == gid)
    assert editor["display_name"] == "Editors"
    assert all("password_hash" not in e for e in entries)
