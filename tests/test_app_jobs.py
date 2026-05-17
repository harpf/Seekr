import pytest

pytestmark = pytest.mark.skipif(
    not pytest.importorskip("fastapi", reason="fastapi not installed"),
    reason="fastapi required",
)

from fastapi.testclient import TestClient
from document_search.app import create_app


def _login(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_index_start_persists_job_and_returns_string_id(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        # Use a tmp path that exists but is empty — no documents to index
        empty = tmp_path / "empty"
        empty.mkdir()
        r = client.post(
            "/api/index/start",
            headers={"X-Auth-Token": token},
            json={"paths": [str(empty)]},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "job_id" in body
        assert isinstance(body["job_id"], str)
        # Numeric (since job ids come from SQLite autoincrement)
        int(body["job_id"])


def test_index_job_get_returns_progress_shape(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        empty = tmp_path / "empty"
        empty.mkdir()
        start = client.post(
            "/api/index/start",
            headers={"X-Auth-Token": token},
            json={"paths": [str(empty)]},
        ).json()
        job_id = start["job_id"]

        # Wait for the worker to finish (empty dir → finishes instantly)
        import time
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            r = client.get(f"/api/index/jobs/{job_id}", headers={"X-Auth-Token": token})
            assert r.status_code == 200, r.text
            body = r.json()
            if body["status"] in ("finished", "failed", "interrupted"):
                break
            time.sleep(0.05)
        # Shape check: must contain status + the six progress counters
        for key in ("status", "found", "indexed", "skipped", "updated", "errors", "done"):
            assert key in body, f"missing {key} in {body}"
        assert body["status"] == "finished"
        assert body["found"] == 0


def test_index_job_404_for_unknown_id(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        r = client.get("/api/index/jobs/999999", headers={"X-Auth-Token": token})
        assert r.status_code == 404


def test_ai_suggest_structure_persists_and_shape(tmp_path, monkeypatch):
    """The route enqueues a job; we replace the handler with a stub so we don't
    need a live Ollama. We register a fake handler on the worker BEFORE startup."""
    from document_search.app import create_app
    app = create_app(str(tmp_path / "t.db"))

    # Override the real handler with a stub that returns a deterministic structure.
    # This must happen before the worker picks up jobs, but after registration.
    @app.state.worker.handler("ai_suggest_structure")
    def _stub(payload, progress_cb):
        return {"structure": ["projects/", "archive/"]}

    with TestClient(app) as client:
        token = _login(client)
        r = client.post("/api/ai/suggest-structure", headers={"X-Auth-Token": token}, json={})
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        # Poll for completion
        import time
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            r = client.get(f"/api/ai/jobs/{job_id}", headers={"X-Auth-Token": token})
            assert r.status_code == 200, r.text
            body = r.json()
            if body["status"] == "finished":
                break
            time.sleep(0.05)
        assert body["status"] == "finished"
        assert body["result"] == {"structure": ["projects/", "archive/"]}


def test_ai_reorganize_persists_and_shape(tmp_path):
    from document_search.app import create_app
    app = create_app(str(tmp_path / "t.db"))

    @app.state.worker.handler("ai_reorganize")
    def _stub(payload, progress_cb):
        progress_cb({"total": 2, "done": 0, "results": []})
        progress_cb({"total": 2, "done": 1, "results": [{"id": 1}]})
        progress_cb({"total": 2, "done": 2, "results": [{"id": 1}, {"id": 2}]})
        return {"total": 2, "done": 2, "results": [{"id": 1}, {"id": 2}]}

    with TestClient(app) as client:
        token = _login(client)
        r = client.post("/api/ai/reorganize/start", headers={"X-Auth-Token": token}, json={"limit": 10})
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        import time
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            r = client.get(f"/api/ai/jobs/{job_id}", headers={"X-Auth-Token": token})
            body = r.json()
            if body["status"] == "finished":
                break
            time.sleep(0.05)
        assert body["status"] == "finished"
        assert body["total"] == 2
        assert body["done"] == 2
        assert len(body["results"]) == 2


def test_restart_marks_running_jobs_interrupted(tmp_path):
    """Simulate a crash: enqueue + claim, then close the app, then re-open.
    The previously-running job must be marked 'interrupted'."""
    from document_search.app import create_app
    from document_search.services.job_store import JobStore
    from document_search.index.sqlite_store import SqliteStore

    db = tmp_path / "t.db"

    # First app instance: enqueue + claim (force into running)
    app1 = create_app(str(db))
    with TestClient(app1) as c1:
        token = _login(c1)
        # Use a known-blocked path to skip actual indexing, but the validator
        # rejects blocked paths *before* enqueuing, so use a real empty dir.
        empty = tmp_path / "empty"
        empty.mkdir()
        r = c1.post("/api/index/start", headers={"X-Auth-Token": token},
                    json={"paths": [str(empty)]})
        job_id = r.json()["job_id"]
        # Force the job back to running so we can simulate the crash
        s = SqliteStore(db)
        s.conn.execute("UPDATE jobs SET state='running' WHERE id=?", (int(job_id),))
        s.conn.commit()

    # Second app instance: must mark it interrupted
    app2 = create_app(str(db))
    with TestClient(app2) as c2:
        token = _login(c2)
        r = c2.get(f"/api/index/jobs/{job_id}", headers={"X-Auth-Token": token})
        assert r.status_code == 200
        assert r.json()["status"] == "interrupted"


def test_retry_recovers_from_transient_failure(tmp_path):
    """A handler that fails the first time then succeeds: end state is succeeded,
    retry_count == 1."""
    from document_search.app import create_app
    from document_search.services.job_store import JobStore

    db = tmp_path / "t.db"
    app = create_app(str(db))
    attempts = []

    @app.state.worker.handler("flaky_demo")
    def _flaky(payload, progress_cb):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("transient")
        return {"ok": True}

    with TestClient(app):
        job_id = app.state.job_store.enqueue("flaky_demo", {}, max_retries=2)
        # The worker is running; wait for retry cycle.
        # Backoff is 2**1 = 2s; force next_attempt to now so we don't wait.
        import time
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            job = app.state.job_store.get(job_id)
            if job["state"] == "pending" and job["retry_count"] >= 1:
                # Speed up retry
                app.state.job_store.conn.execute(
                    "UPDATE jobs SET next_attempt_at=NULL WHERE id=?", (job_id,)
                )
                app.state.job_store.conn.commit()
            if job["state"] in ("succeeded", "failed"):
                break
            time.sleep(0.05)
        assert job["state"] == "succeeded"
        assert job["retry_count"] == 1


def test_index_job_returns_404_when_user_deleted_mid_session(tmp_path):
    """If a user is deleted from the DB while their session token is still
    held, accessing a job they don't own (or even one they do own) must NOT
    raise a 500. Currently it does, because the GET endpoint does
    `get_user_by_id(user_id)["role"]` without a None check.
    """
    from document_search.app import create_app
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        admin_token = _login(client)
        # Create a second user, get their session, then delete them.
        r = client.post(
            "/api/users",
            headers={"X-Auth-Token": admin_token},
            json={"username": "alice", "password": "alicepw1", "role": "user"},
        )
        assert r.status_code == 200, r.text
        alice_token = _login(client, "alice", "alicepw1")

        # Enqueue a job as admin (so alice doesn't own it)
        empty = tmp_path / "empty"
        empty.mkdir()
        r = client.post(
            "/api/index/start",
            headers={"X-Auth-Token": admin_token},
            json={"paths": [str(empty)]},
        )
        job_id = r.json()["job_id"]

        # Delete alice
        alice_id = None
        users = client.get("/api/users", headers={"X-Auth-Token": admin_token}).json()
        for u in users.get("users", users) if isinstance(users, dict) else users:
            if u["username"] == "alice":
                alice_id = u["id"]
                break
        assert alice_id is not None, f"alice not found in users list: {users}"
        client.delete(f"/api/users/{alice_id}", headers={"X-Auth-Token": admin_token})

        # Now alice's token is still in the in-memory sessions dict but the
        # underlying user row is gone. Accessing the job must NOT 500.
        r = client.get(
            f"/api/index/jobs/{job_id}",
            headers={"X-Auth-Token": alice_token},
        )
        assert r.status_code == 404, f"expected 404, got {r.status_code}: {r.text}"
