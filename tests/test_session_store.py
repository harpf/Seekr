from pathlib import Path
import pytest
from document_search.index.sqlite_store import SqliteStore


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "test.db")


def test_sessions_table_exists(store):
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
    ).fetchall()
    assert len(rows) == 1


def test_sessions_table_columns(store):
    cols = {c[1] for c in store.conn.execute("PRAGMA table_info(sessions)").fetchall()}
    expected = {"token", "user_id", "role", "issued_at", "expires_at"}
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_login_attempts_table_exists(store):
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='login_attempts'"
    ).fetchall()
    assert len(rows) == 1


def test_login_attempts_table_columns(store):
    cols = {c[1] for c in store.conn.execute("PRAGMA table_info(login_attempts)").fetchall()}
    expected = {"id", "ip", "attempted_at"}
    assert expected.issubset(cols), f"missing: {expected - cols}"


import time
from document_search.services.session_store import (
    SqliteSessionStore,
    build_session_store,
)


def test_create_and_get_roundtrip(store):
    ss = SqliteSessionStore(store)
    ss.create("tok-abc", user_id=7, role="admin", ttl_seconds=3600)
    sess = ss.get("tok-abc")
    assert sess is not None
    assert sess["user_id"] == 7
    assert sess["role"] == "admin"


def test_session_persists_across_fresh_store_instance(tmp_path):
    """A session written by one store instance is readable by a brand-new
    instance pointed at the same DB — proving it survives restart / is shared
    across replicas."""
    from document_search.index.sqlite_store import SqliteStore
    db = tmp_path / "shared.db"
    s1 = SqliteStore(db)
    ss1 = SqliteSessionStore(s1)
    ss1.create("persisted", user_id=42, role="user", ttl_seconds=3600)

    # Simulate a different process/replica: a fresh SqliteStore on the same file.
    s2 = SqliteStore(db)
    ss2 = SqliteSessionStore(s2)
    sess = ss2.get("persisted")
    assert sess is not None
    assert sess["user_id"] == 42
    assert sess["role"] == "user"


def test_expired_session_returns_none_and_is_deleted(store):
    ss = SqliteSessionStore(store)
    ss.create("expiring", user_id=1, role="user", ttl_seconds=-1)  # already expired
    assert ss.get("expiring") is None
    # Lazy delete on read: the row should be gone.
    row = store.conn.execute(
        "SELECT 1 FROM sessions WHERE token='expiring'"
    ).fetchone()
    assert row is None


def test_unknown_token_returns_none(store):
    ss = SqliteSessionStore(store)
    assert ss.get("nope") is None


def test_delete_removes_session(store):
    ss = SqliteSessionStore(store)
    ss.create("kill-me", user_id=1, role="user", ttl_seconds=3600)
    ss.delete("kill-me")
    assert ss.get("kill-me") is None


def test_purge_expired_removes_only_expired(store):
    ss = SqliteSessionStore(store)
    ss.create("alive", user_id=1, role="user", ttl_seconds=3600)
    ss.create("dead", user_id=2, role="user", ttl_seconds=-10)
    removed = ss.purge_expired()
    assert removed == 1
    assert ss.get("alive") is not None
    assert ss.get("dead") is None


def test_build_session_store_defaults_to_sqlite(store):
    ss = build_session_store(sqlite_store=store, backend="sqlite")
    assert isinstance(ss, SqliteSessionStore)


def test_build_session_store_rejects_unknown_backend(store):
    with pytest.raises(ValueError, match="unknown session backend"):
        build_session_store(sqlite_store=store, backend="bogus")
