import time

import pytest

pytestmark = pytest.mark.skipif(
    not pytest.importorskip("fastapi", reason="fastapi not installed"),
    reason="fastapi required",
)

from fastapi.testclient import TestClient  # noqa: E402

from document_search.app import create_app  # noqa: E402


def _login(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _wait(client, token, url, until, timeout=5.0):
    deadline = time.monotonic() + timeout
    body = {}
    while time.monotonic() < deadline:
        r = client.get(url, headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        body = r.json()
        if until(body):
            return body
        time.sleep(0.03)
    return body


def test_ai_pull_persists_and_preserves_legacy_shape(tmp_path):
    app = create_app(str(tmp_path / "t.db"))

    @app.state.worker.handler("ai_pull")
    def _stub(payload, progress_cb):
        progress_cb({"status": "downloading", "completed": 50, "total": 100})
        progress_cb({"status": "downloading", "completed": 100, "total": 100})
        return {"ok": True, "model": payload.get("model") or "llama3.2", "status": "success"}

    with TestClient(app) as client:
        token = _login(client)
        r = client.post("/api/ai/models/pull", headers={"X-Auth-Token": token}, json={})
        assert r.status_code == 200, r.text
        body = r.json()
        assert "job_id" in body
        assert body["job_id"].isdigit()
        assert "model" in body

        final = _wait(
            client, token, f"/api/ai/jobs/{body['job_id']}",
            until=lambda b: b["status"] in ("done", "error"),
        )
        assert final["status"] == "done"
        assert final["model"]
        assert final["result"]["ok"] is True


def test_ai_pull_failure_maps_to_error_status(tmp_path):
    app = create_app(str(tmp_path / "t.db"))

    @app.state.worker.handler("ai_pull")
    def _stub(payload, progress_cb):
        return {"ok": False, "model": "bogus", "error": "manifest not found"}

    with TestClient(app) as client:
        token = _login(client)
        r = client.post("/api/ai/models/pull", headers={"X-Auth-Token": token},
                        json={"model": "bogus"})
        job_id = r.json()["job_id"]
        final = _wait(
            client, token, f"/api/ai/jobs/{job_id}",
            until=lambda b: b["status"] in ("done", "error"),
        )
        assert final["status"] == "error"
        assert final["result"]["ok"] is False
        assert "manifest" in final["result"]["error"]


def test_update_run_persists_job_row(tmp_path, monkeypatch):
    """Monkeypatch the script existence + subprocess to a fast no-op, then assert
    a persistent system_update row exists and is finalised."""
    import document_search.app as appmod

    app = create_app(str(tmp_path / "t.db"))

    class _FakeProc:
        returncode = 0
        stdout = "updated ok"
        stderr = ""

    monkeypatch.setenv("DOCUMENT_SEARCH_UI_UPDATE_ENABLED", "true")
    monkeypatch.setattr(appmod.Path, "exists", lambda self: True, raising=False)
    monkeypatch.setattr(appmod.subprocess, "run", lambda *a, **k: _FakeProc())

    with TestClient(app) as client:
        token = _login(client)
        r = client.post("/api/update/run", headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        body = r.json()
        assert "job_id" in body

        final = _wait(
            client, token, "/api/update/status",
            until=lambda b: b.get("status") in ("done", "error"),
        )
        assert final["status"] == "done"

        js = app.state.job_store
        rows = js.list_jobs(kind="system_update")
        assert len(rows) == 1
        assert rows[0]["state"] == "succeeded"
