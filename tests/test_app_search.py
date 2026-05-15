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


def test_index_start_blocks_empty_string_path(client):
    resp = client.post("/api/index/start", json={"paths": [""]}, headers=auth_headers(client))
    assert resp.status_code == 400


def test_index_start_allows_legitimate_path(client):
    # /mnt/data is not a blocked path — should not return 400 for path validation
    # It may return 400 for other reasons (e.g. path doesn't exist) but NOT "not allowed"
    resp = client.post("/api/index/start", json={"paths": ["/mnt/data"]}, headers=auth_headers(client))
    if resp.status_code == 400:
        assert "not allowed" not in resp.json().get("detail", "").lower()


def test_index_extensions_endpoint(client):
    resp = client.get("/api/index/extensions", headers=auth_headers(client))
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_folders_endpoint(client):
    resp = client.get("/api/folders", headers=auth_headers(client))
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_source_folders_endpoint(client):
    resp = client.get("/api/source-folders", headers=auth_headers(client))
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_search_accepts_tags_list(client):
    resp = client.post(
        "/api/search",
        json={"query": "", "tags": ["tag1", "tag2"]},
        headers=auth_headers(client),
    )
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_search_response_is_grouped(client):
    resp = client.post("/api/search", json={"query": ""}, headers=auth_headers(client))
    assert resp.status_code == 200
    data = resp.json()
    if data:
        doc = data[0]
        assert "hits" in doc
        assert "hit_count" in doc
        assert "document_id" in doc
        assert isinstance(doc["hits"], list)
        if doc["hits"]:
            hit = doc["hits"][0]
            assert "block_type" in hit
            assert "block_number" in hit
            assert "snippet_html" in hit
