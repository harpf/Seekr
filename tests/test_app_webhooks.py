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
