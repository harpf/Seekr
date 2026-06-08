import pytest

from document_search.index.sqlite_store import DEFAULT_PREFERENCES, SqliteStore


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "test.db")


def test_user_preferences_table_exists(store):
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='user_preferences'"
    ).fetchall()
    assert len(rows) == 1


def test_get_preferences_returns_defaults_for_new_user(store):
    uid = store.create_user("alice", "alice-password")
    prefs = store.get_preferences(uid)
    assert prefs == DEFAULT_PREFERENCES
    # Returns a fresh mutable copy, not the shared module-level dict
    assert prefs is not DEFAULT_PREFERENCES
    prefs["default_filters"]["tags"].append("mutated")
    assert DEFAULT_PREFERENCES["default_filters"]["tags"] == []


def test_set_preferences_persists_and_merges_over_defaults(store):
    uid = store.create_user("alice", "alice-password")
    merged = store.set_preferences(uid, {"theme": "dark", "results_per_page": 50})
    assert merged["theme"] == "dark"
    assert merged["results_per_page"] == 50
    # Unspecified keys keep their default values
    assert merged["default_filters"] == DEFAULT_PREFERENCES["default_filters"]
    # Persisted across a fresh read
    again = store.get_preferences(uid)
    assert again["theme"] == "dark"
    assert again["results_per_page"] == 50


def test_set_preferences_drops_unknown_keys(store):
    uid = store.create_user("alice", "alice-password")
    merged = store.set_preferences(uid, {"theme": "dark", "bogus": "nope"})
    assert "bogus" not in merged
    assert merged["theme"] == "dark"
    stored = store.get_preferences(uid)
    assert "bogus" not in stored


def test_set_preferences_is_idempotent_upsert(store):
    uid = store.create_user("alice", "alice-password")
    store.set_preferences(uid, {"theme": "dark"})
    store.set_preferences(uid, {"results_per_page": 10})
    count = store.conn.execute(
        "SELECT COUNT(*) FROM user_preferences WHERE user_id=?", (uid,)
    ).fetchone()[0]
    assert count == 1
    prefs = store.get_preferences(uid)
    assert prefs["theme"] == "dark"
    assert prefs["results_per_page"] == 10


def test_preferences_isolated_between_users(store):
    alice = store.create_user("alice", "alice-password")
    bob = store.create_user("bob", "bob-password")
    store.set_preferences(alice, {"theme": "dark"})
    store.set_preferences(bob, {"theme": "light"})
    assert store.get_preferences(alice)["theme"] == "dark"
    assert store.get_preferences(bob)["theme"] == "light"
