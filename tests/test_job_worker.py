import time

import pytest

from document_search.index.sqlite_store import SqliteStore
from document_search.services.job_store import JobStore
from document_search.services.job_worker import Worker


@pytest.fixture
def setup(tmp_path):
    store = SqliteStore(tmp_path / "test.db")
    js = JobStore(store)
    worker = Worker(js, max_concurrent=2, poll_interval_s=0.01)
    yield store, js, worker
    worker.stop()


def test_register_and_tick_runs_handler(setup):
    store, js, worker = setup
    calls = []

    @worker.handler("demo")
    def demo(payload, progress_cb):
        calls.append(payload)
        return {"ok": True}

    job_id = js.enqueue("demo", {"hello": "world"})
    worker.tick()
    worker.wait_until_idle(timeout=2.0)
    job = js.get(job_id)
    assert job["state"] == "succeeded"
    assert calls == [{"hello": "world"}]


def test_handler_exception_marks_failed_when_no_retries(setup):
    store, js, worker = setup

    @worker.handler("boom")
    def boom(payload, progress_cb):
        raise RuntimeError("nope")

    job_id = js.enqueue("boom", {}, max_retries=0)
    worker.tick()
    worker.wait_until_idle(timeout=2.0)
    job = js.get(job_id)
    assert job["state"] == "failed"
    assert "nope" in (job["error_message"] or "")


def test_handler_exception_retries_when_allowed(setup):
    store, js, worker = setup
    attempts = []

    @worker.handler("flaky")
    def flaky(payload, progress_cb):
        attempts.append(1)
        if len(attempts) < 2:
            raise RuntimeError("first try")
        return {"finally": True}

    job_id = js.enqueue("flaky", {}, max_retries=3)
    worker.tick()
    worker.wait_until_idle(timeout=2.0)
    # After first failure: state=pending with next_attempt_at in future.
    # Force the retry to be eligible:
    store.conn.execute(
        "UPDATE jobs SET next_attempt_at=NULL WHERE id=?", (job_id,),
    )
    store.conn.commit()
    worker.tick()
    worker.wait_until_idle(timeout=2.0)
    job = js.get(job_id)
    assert job["state"] == "succeeded"
    assert job["retry_count"] == 1


def test_progress_cb_writes_to_progress_json(setup):
    store, js, worker = setup
    import json

    @worker.handler("progressing")
    def progressing(payload, progress_cb):
        progress_cb({"step": 1})
        progress_cb({"step": 2, "done": True})
        return {"final": True}

    job_id = js.enqueue("progressing", {})
    worker.tick()
    worker.wait_until_idle(timeout=2.0)
    job = js.get(job_id)
    assert job["state"] == "succeeded"
    assert json.loads(job["progress_json"]) == {"step": 2, "done": True}


def test_unknown_kind_marks_failed(setup):
    """A job whose kind has no registered handler fails immediately (not a retry)."""
    store, js, worker = setup
    job_id = js.enqueue("no-such-kind", {}, max_retries=5)
    worker.tick()
    worker.wait_until_idle(timeout=2.0)
    job = js.get(job_id)
    assert job["state"] == "failed"
    assert "no handler" in (job["error_message"] or "").lower()


def test_max_concurrent_caps_in_flight(setup):
    """A second job doesn't start until the first finishes when max_concurrent=2
    and there are 3 long-running jobs queued."""
    store, js, worker = setup
    import threading
    barrier_lock = threading.Lock()
    barrier_lock.acquire()
    started = []

    @worker.handler("slow")
    def slow(payload, progress_cb):
        started.append(payload["i"])
        with barrier_lock:
            return {}

    ids = [js.enqueue("slow", {"i": i}) for i in range(3)]
    worker.tick()
    # Two should have started, third waits.
    # Brief sleep to allow threads to enter the handler.
    time.sleep(0.1)
    assert len(started) == 2
    barrier_lock.release()
    worker.wait_until_idle(timeout=3.0)
    assert len(started) == 3


def test_app_startup_creates_worker_and_marks_interrupted(tmp_path, monkeypatch):
    """The FastAPI app must instantiate a Worker and run mark_interrupted_running_jobs
    at startup."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from document_search.app import create_app
    from document_search.index.sqlite_store import SqliteStore

    db_path = tmp_path / "test.db"
    # Seed the DB with a 'running' job from a pretend prior process
    seed = SqliteStore(db_path)
    from document_search.services.job_store import JobStore
    js = JobStore(seed)
    job_id = js.enqueue("demo", {})
    seed.conn.execute("UPDATE jobs SET state='running' WHERE id=?", (job_id,))
    seed.conn.commit()
    seed.conn.close()

    app = create_app(str(db_path))
    with TestClient(app):  # triggers startup
        post_startup = SqliteStore(db_path)
        row = post_startup.conn.execute(
            "SELECT state FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        assert row["state"] == "interrupted"
        # The app must expose its worker for handlers to register
        assert hasattr(app.state, "worker")
        assert hasattr(app.state, "job_store")
