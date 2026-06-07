import pytest

from document_search.index.sqlite_store import SqliteStore


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "test.db")


def test_audit_log_table_exists(store):
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_log'"
    ).fetchall()
    assert len(rows) == 1


def test_audit_log_columns(store):
    cols = {c[1] for c in store.conn.execute("PRAGMA table_info(audit_log)").fetchall()}
    expected = {
        "id", "actor_user_id", "action", "target_type", "target_id",
        "detail_json", "ip", "created_at",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_audit_log_indexes_exist(store):
    idx = {r[1] for r in store.conn.execute("PRAGMA index_list(audit_log)").fetchall()}
    assert "idx_audit_actor" in idx
    assert "idx_audit_action" in idx
    assert "idx_audit_created" in idx
