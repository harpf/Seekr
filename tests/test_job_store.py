from pathlib import Path
import pytest
from document_search.index.sqlite_store import SqliteStore


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "test.db")


def test_jobs_table_exists(store):
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='jobs'"
    ).fetchall()
    assert len(rows) == 1


def test_jobs_table_columns(store):
    cols = {c[1] for c in store.conn.execute("PRAGMA table_info(jobs)").fetchall()}
    expected = {
        "id", "kind", "state", "payload_json", "progress_json", "result_json",
        "error_message", "retry_count", "max_retries", "owner_user_id",
        "created_at", "started_at", "finished_at", "next_attempt_at",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_jobs_state_check_constraint(store):
    from datetime import UTC, datetime
    now = datetime.now(tz=UTC).isoformat()
    # Valid states are accepted
    for state in ("pending", "running", "succeeded", "failed", "interrupted"):
        store.conn.execute(
            "INSERT INTO jobs(kind, state, payload_json, retry_count, max_retries, created_at) "
            "VALUES('test', ?, '{}', 0, 0, ?)", (state, now),
        )
    # Invalid state is rejected
    with pytest.raises(Exception):
        store.conn.execute(
            "INSERT INTO jobs(kind, state, payload_json, retry_count, max_retries, created_at) "
            "VALUES('test', 'bogus', '{}', 0, 0, ?)", (now,),
        )
