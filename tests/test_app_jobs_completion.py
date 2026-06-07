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


def _create_user(client, admin_token, username, password="pw123456", role="user"):
    r = client.post(
        "/api/users",
        headers={"X-Auth-Token": admin_token},
        json={"username": username, "password": password, "role": role},
    )
    assert r.status_code in (200, 201), r.text


def test_list_jobs_owner_scoped_and_admin_sees_all(tmp_path):
    app = create_app(str(tmp_path / "t.db"))

    @app.state.worker.handler("demo_block")
    def _block(payload, progress_cb):
        import time as _t
        _t.sleep(2.0)
        return {}

    with TestClient(app) as client:
        admin_token = _login(client)
        _create_user(client, admin_token, "bob")
        bob_token = client.post("/api/login", json={"username": "bob", "password": "pw123456"}).json()["token"]

        admin_job = app.state.job_store.enqueue("demo_block", {}, owner_user_id=1)
        users = client.get("/api/users", headers={"X-Auth-Token": admin_token}).json()
        bob_uid = next(u["id"] for u in users if u["username"] == "bob")
        bob_job = app.state.job_store.enqueue("demo_block", {}, owner_user_id=bob_uid)

        r = client.get("/api/jobs", headers={"X-Auth-Token": bob_token})
        assert r.status_code == 200, r.text
        ids = {j["id"] for j in r.json()}
        assert bob_job in ids
        assert admin_job not in ids

        r = client.get("/api/jobs", headers={"X-Auth-Token": admin_token})
        ids = {j["id"] for j in r.json()}
        assert {admin_job, bob_job}.issubset(ids)


def test_cancel_job_endpoint_owner_and_admin(tmp_path):
    app = create_app(str(tmp_path / "t.db"))

    @app.state.worker.handler("loopy")
    def _loopy(payload, progress_cb):
        import time as _t
        jid = payload["job_id"]
        for _ in range(1000):
            if app.state.worker.is_cancelled(jid):
                from document_search.services.job_worker import JobCancelled
                raise JobCancelled()
            progress_cb({"tick": 1})
            _t.sleep(0.01)
        return {}

    with TestClient(app) as client:
        token = _login(client)
        jid = app.state.job_store.enqueue("loopy", {}, owner_user_id=1)
        app.state.job_store.conn.execute(
            "UPDATE jobs SET payload_json=? WHERE id=?", (f'{{"job_id": {jid}}}', jid)
        )
        app.state.job_store.conn.commit()

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if app.state.job_store.get(jid)["state"] == "running":
                break
            time.sleep(0.02)

        r = client.post(f"/api/jobs/{jid}/cancel", headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        assert r.json()["outcome"] in ("requested", "cancelled")

        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if app.state.job_store.get(jid)["state"] == "cancelled":
                break
            time.sleep(0.03)
        assert app.state.job_store.get(jid)["state"] == "cancelled"


def test_cancel_other_users_job_is_404_for_non_admin(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        admin_token = _login(client)
        _create_user(client, admin_token, "carol")
        carol_token = client.post("/api/login", json={"username": "carol", "password": "pw123456"}).json()["token"]
        admin_job = app.state.job_store.enqueue("demo", {}, owner_user_id=1)
        r = client.post(f"/api/jobs/{admin_job}/cancel", headers={"X-Auth-Token": carol_token})
        assert r.status_code == 404


def test_re_enqueue_endpoint(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        src = app.state.job_store.enqueue("index_paths", {"paths": ["/x"]}, owner_user_id=1)
        app.state.job_store.conn.execute("UPDATE jobs SET state='interrupted' WHERE id=?", (src,))
        app.state.job_store.conn.commit()
        r = client.post(f"/api/jobs/{src}/re-enqueue", headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        new_id = int(r.json()["job_id"])
        assert new_id != src
        assert app.state.job_store.get(new_id)["state"] in ("pending", "running", "succeeded")


def test_cancel_pending_job_via_api_is_immediate(tmp_path):
    """A pending job (worker stopped) cancelled via the API goes straight to
    cancelled and never runs its handler."""
    app = create_app(str(tmp_path / "t.db"))
    ran = []

    @app.state.worker.handler("never")
    def _never(payload, progress_cb):
        ran.append(payload)
        return {}

    with TestClient(app) as client:
        token = _login(client)
        app.state.worker.stop()
        jid = app.state.job_store.enqueue("never", {"x": 1}, owner_user_id=1)
        r = client.post(f"/api/jobs/{jid}/cancel", headers={"X-Auth-Token": token})
        assert r.status_code == 200
        assert r.json()["outcome"] == "cancelled"
        assert app.state.job_store.get(jid)["state"] == "cancelled"
        app.state.worker.start()
        time.sleep(0.3)
        assert ran == []


def test_re_enqueue_then_runs(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    done = []

    @app.state.worker.handler("rerun")
    def _rerun(payload, progress_cb):
        done.append(payload)
        return {"ok": True}

    with TestClient(app) as client:
        token = _login(client)
        src = app.state.job_store.enqueue("rerun", {"n": 1}, owner_user_id=1)
        app.state.job_store.conn.execute("UPDATE jobs SET state='interrupted' WHERE id=?", (src,))
        app.state.job_store.conn.commit()
        r = client.post(f"/api/jobs/{src}/re-enqueue", headers={"X-Auth-Token": token})
        new_id = int(r.json()["job_id"])
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if app.state.job_store.get(new_id)["state"] == "succeeded":
                break
            time.sleep(0.03)
        assert app.state.job_store.get(new_id)["state"] == "succeeded"
        assert done == [{"n": 1}]
