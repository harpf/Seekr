import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from document_search.app import create_app  # noqa: E402


def _login(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_app_exposes_webhook_service_and_handler(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app):
        assert getattr(app.state, "webhook_service", None) is not None
        assert "webhook_deliver" in app.state.worker._handlers
