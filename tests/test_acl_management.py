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
