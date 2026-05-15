import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from document_search.app import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "index.db"))
    return TestClient(app)


def auth_headers(client):
    r = client.post("/api/login", json={"username": "admin", "password": "admin"})
    return {"X-Auth-Token": r.json()["token"]}


def test_index_start_blocks_root(client):
    resp = client.post("/api/index/start", json={"paths": ["/"]}, headers=auth_headers(client))
    assert resp.status_code == 400
    assert "not allowed" in resp.json()["detail"].lower()


def test_index_start_blocks_proc(client):
    resp = client.post("/api/index/start", json={"paths": ["/proc/sys"]}, headers=auth_headers(client))
    assert resp.status_code == 400


def test_index_start_blocks_sys(client):
    resp = client.post("/api/index/start", json={"paths": ["/sys/kernel"]}, headers=auth_headers(client))
    assert resp.status_code == 400


def test_index_start_blocks_dev(client):
    resp = client.post("/api/index/start", json={"paths": ["/dev/null"]}, headers=auth_headers(client))
    assert resp.status_code == 400


def test_index_start_blocks_double_slash_proc(client):
    resp = client.post("/api/index/start", json={"paths": ["//proc/sys"]}, headers=auth_headers(client))
    assert resp.status_code == 400


def test_index_start_allows_legitimate_path(client):
    # /mnt/data is not a blocked path — should not return 400 for path validation
    # It may return 400 for other reasons (e.g. path doesn't exist) but NOT "not allowed"
    resp = client.post("/api/index/start", json={"paths": ["/mnt/data"]}, headers=auth_headers(client))
    if resp.status_code == 400:
        assert "not allowed" not in resp.json().get("detail", "").lower()
