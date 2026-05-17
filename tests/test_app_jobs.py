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
