import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from document_search.app import create_app


def test_status_endpoint_requires_login(tmp_path):
    app = create_app(str(tmp_path / "index.db"))
    client = TestClient(app)
    login = client.post("/api/login", json={"username": "admin", "password": "admin"})
    assert login.status_code == 200
    token = login.json()["token"]
    response = client.get("/api/status", headers={"X-Auth-Token": token})
    assert response.status_code == 200



def test_status_contains_size(tmp_path):
    app = create_app(str(tmp_path / "index.db"))
    client = TestClient(app)
    login = client.post("/api/login", json={"username": "admin", "password": "admin"})
    token = login.json()["token"]
    response = client.get("/api/status", headers={"X-Auth-Token": token})
    payload = response.json()
    assert "total_file_size_bytes" in payload
    assert payload["total_file_size_bytes"] >= 0


def test_status_exposes_supported_extensions(tmp_path):
    """/api/status reports the live set of extractable file types so clients can
    discover what Seekr can index — including types added by plugins."""
    app = create_app(str(tmp_path / "index.db"))
    client = TestClient(app)
    token = client.post(
        "/api/login", json={"username": "admin", "password": "admin"}
    ).json()["token"]
    payload = client.get("/api/status", headers={"X-Auth-Token": token}).json()

    exts = payload["supported_extensions"]
    # Built-in extractors are always present.
    assert ".pdf" in exts and ".docx" in exts
    # The shipped example .csv drop-in plugin is discovered at startup, proving
    # plugin-provided types surface here too.
    assert ".csv" in exts
    # Sorted, dotted-lower suffixes.
    assert exts == sorted(exts)
    assert all(e.startswith(".") and e == e.lower() for e in exts)
