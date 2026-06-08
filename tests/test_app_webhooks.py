import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from document_search.app import create_app  # noqa: E402


def _login(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _create_user(client, admin_token, username, password="pw-secret-123", role="user"):
    r = client.post(
        "/api/users",
        headers={"X-Auth-Token": admin_token},
        json={"username": username, "password": password, "role": role},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_app_exposes_webhook_service_and_handler(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app):
        assert getattr(app.state, "webhook_service", None) is not None
        assert "webhook_deliver" in app.state.worker._handlers


def test_create_webhook_admin_only(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        admin = _login(client)
        _create_user(client, admin, "bob")
        user_token = _login(client, "bob", "pw-secret-123")

        # Non-admin is rejected.
        r = client.post(
            "/api/webhooks",
            headers={"X-Auth-Token": user_token},
            json={"url": "https://93.184.216.34/hook", "event_type": "index.completed"},
        )
        assert r.status_code == 403, r.text

        # Admin creates a webhook; the secret is returned once.
        r = client.post(
            "/api/webhooks",
            headers={"X-Auth-Token": admin},
            json={"url": "https://93.184.216.34/hook", "event_type": "index.completed"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["secret"]
        wid = body["id"]

        # The list omits the secret.
        r = client.get("/api/webhooks", headers={"X-Auth-Token": admin})
        assert r.status_code == 200, r.text
        items = r.json()
        assert any(item["id"] == wid for item in items)
        for item in items:
            assert "secret" not in item


def test_create_webhook_rejects_unsafe_url_via_api(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        admin = _login(client)
        r = client.post(
            "/api/webhooks",
            headers={"X-Auth-Token": admin},
            json={"url": "http://127.0.0.1/x", "event_type": "index.completed"},
        )
        assert r.status_code == 400, r.text


def test_create_webhook_rejects_unknown_event_via_api(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        admin = _login(client)
        r = client.post(
            "/api/webhooks",
            headers={"X-Auth-Token": admin},
            json={"url": "https://93.184.216.34/x", "event_type": "bogus.event"},
        )
        assert r.status_code == 400, r.text


def test_delete_webhook_admin_only(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        admin = _login(client)
        r = client.post(
            "/api/webhooks",
            headers={"X-Auth-Token": admin},
            json={"url": "https://93.184.216.34/hook", "event_type": "index.completed"},
        )
        wid = r.json()["id"]

        r = client.delete(f"/api/webhooks/{wid}", headers={"X-Auth-Token": admin})
        assert r.status_code == 200, r.text

        # Deleting again is a 404.
        r = client.delete(f"/api/webhooks/{wid}", headers={"X-Auth-Token": admin})
        assert r.status_code == 404, r.text


def test_completed_index_job_enqueues_webhook_delivery(tmp_path):
    import json
    import time

    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        admin = _login(client)

        # Subscribe to index.completed. Use a literal public IP so the SSRF guard
        # passes without DNS resolution; delivery itself will fail (no server) but
        # we only assert the job is ENQUEUED.
        r = client.post(
            "/api/webhooks",
            headers={"X-Auth-Token": admin},
            json={"url": "https://93.184.216.34/x", "event_type": "index.completed"},
        )
        assert r.status_code == 200, r.text

        empty = tmp_path / "empty"
        empty.mkdir()
        start = client.post(
            "/api/index/start",
            headers={"X-Auth-Token": admin},
            json={"paths": [str(empty)]},
        )
        assert start.status_code == 200, start.text
        index_job_id = int(start.json()["job_id"])

        # Wait for the index job to finish.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            r = client.get(
                f"/api/index/jobs/{index_job_id}", headers={"X-Auth-Token": admin}
            )
            if r.status_code == 200 and r.json()["status"] in (
                "finished",
                "failed",
                "interrupted",
            ):
                break
            time.sleep(0.05)
        assert r.json()["status"] == "finished", r.text

        # A webhook_deliver job must have been enqueued for the index.completed event.
        deadline = time.monotonic() + 5
        deliver_jobs = []
        while time.monotonic() < deadline:
            deliver_jobs = app.state.job_store.list_jobs(kind="webhook_deliver")
            if deliver_jobs:
                break
            time.sleep(0.05)
        assert deliver_jobs, "no webhook_deliver job was enqueued"

        payload = json.loads(deliver_jobs[0]["payload_json"])
        assert payload["event_type"] == "index.completed"
        event = payload["event"]
        assert "found" in event
        assert event["index_job_id"] == index_job_id
