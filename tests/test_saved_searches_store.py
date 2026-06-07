import sqlite3

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
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO saved_searches(user_id, name, query, filters_json, created_at) "
            "VALUES(?,?,?,?,?)", (uid, "My search", "bar", "{}", now),
        )
