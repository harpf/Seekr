
import pytest

from document_search.index.sqlite_store import SqliteStore


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "test.db")


def test_webhooks_table_exists(store):
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='webhooks'"
    ).fetchall()
    assert len(rows) == 1


def test_webhook_deliveries_table_exists(store):
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='webhook_deliveries'"
    ).fetchall()
    assert len(rows) == 1


def test_webhooks_columns(store):
    cols = {c[1] for c in store.conn.execute("PRAGMA table_info(webhooks)").fetchall()}
    expected = {
        "id", "url", "event_type", "secret", "enabled", "created_by", "created_at",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_webhook_deliveries_columns(store):
    cols = {c[1] for c in store.conn.execute(
        "PRAGMA table_info(webhook_deliveries)"
    ).fetchall()}
    expected = {
        "id", "webhook_id", "job_id", "event_type", "status_code",
        "success", "error_message", "attempted_at",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"
