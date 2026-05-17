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


import json
from document_search.services.job_store import JobStore


def test_enqueue_returns_int_id_and_pending_row(store):
    js = JobStore(store)
    job_id = js.enqueue(kind="demo", payload={"x": 1}, owner_user_id=None, max_retries=2)
    assert isinstance(job_id, int)
    row = store.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row["state"] == "pending"
    assert json.loads(row["payload_json"]) == {"x": 1}
    assert row["max_retries"] == 2


def test_claim_next_returns_pending_job_and_marks_running(store):
    js = JobStore(store)
    job_id = js.enqueue(kind="demo", payload={})
    claimed = js.claim_next()
    assert claimed is not None
    assert claimed["id"] == job_id
    assert claimed["state"] == "running"
    # Second claim returns nothing (only one pending)
    assert js.claim_next() is None


def test_claim_next_respects_next_attempt_at(store):
    from datetime import UTC, datetime, timedelta
    js = JobStore(store)
    future = (datetime.now(tz=UTC) + timedelta(minutes=10)).isoformat()
    job_id = js.enqueue(kind="demo", payload={})
    # Push it into the future
    store.conn.execute(
        "UPDATE jobs SET next_attempt_at=? WHERE id=?", (future, job_id),
    )
    store.conn.commit()
    assert js.claim_next() is None


def test_update_progress_writes_progress_json(store):
    js = JobStore(store)
    job_id = js.enqueue(kind="demo", payload={})
    js.claim_next()
    js.update_progress(job_id, {"found": 5, "indexed": 2})
    row = store.conn.execute("SELECT progress_json FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert json.loads(row["progress_json"]) == {"found": 5, "indexed": 2}


def test_mark_succeeded_sets_state_and_result(store):
    js = JobStore(store)
    job_id = js.enqueue(kind="demo", payload={})
    js.claim_next()
    js.mark_succeeded(job_id, {"ok": True})
    row = store.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row["state"] == "succeeded"
    assert json.loads(row["result_json"]) == {"ok": True}
    assert row["finished_at"] is not None


def test_mark_failed_without_retries_sets_failed(store):
    js = JobStore(store)
    job_id = js.enqueue(kind="demo", payload={}, max_retries=0)
    js.claim_next()
    js.mark_failed(job_id, "boom")
    row = store.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row["state"] == "failed"
    assert row["error_message"] == "boom"


def test_mark_failed_with_retries_reschedules(store):
    js = JobStore(store)
    job_id = js.enqueue(kind="demo", payload={}, max_retries=2)
    js.claim_next()
    js.mark_failed(job_id, "transient")
    row = store.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row["state"] == "pending"
    assert row["retry_count"] == 1
    assert row["next_attempt_at"] is not None


def test_mark_interrupted_running_jobs(store):
    js = JobStore(store)
    a = js.enqueue(kind="demo", payload={})
    b = js.enqueue(kind="demo", payload={})
    js.claim_next()  # marks 'a' running
    n = js.mark_interrupted_running_jobs()
    assert n == 1
    row = store.conn.execute("SELECT state FROM jobs WHERE id=?", (a,)).fetchone()
    assert row["state"] == "interrupted"
    # b is still pending
    row = store.conn.execute("SELECT state FROM jobs WHERE id=?", (b,)).fetchone()
    assert row["state"] == "pending"
