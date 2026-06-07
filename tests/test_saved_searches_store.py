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
