from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from document_search.app import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "ver.db"))
    with TestClient(app) as c:
        yield c


def _login(client) -> str:
    resp = client.post("/api/login", json={"username": "admin", "password": "admin"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


# ── Task 1: routing under both prefixes ──────────────────────────────────


def test_login_works_on_legacy_path(client):
    resp = client.post("/api/login", json={"username": "admin", "password": "admin"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "token" in body
    assert body["username"] == "admin"


def test_login_works_on_v1_path(client):
    resp = client.post("/api/v1/login", json={"username": "admin", "password": "admin"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "token" in body
    assert body["username"] == "admin"


def test_login_v1_and_legacy_return_same_shape(client):
    legacy = client.post("/api/login", json={"username": "admin", "password": "admin"}).json()
    v1 = client.post("/api/v1/login", json={"username": "admin", "password": "admin"}).json()
    assert legacy["username"] == v1["username"] == "admin"
    assert set(legacy.keys()) == set(v1.keys())


def test_status_get_works_on_both_prefixes(client):
    token = _login(client)
    legacy = client.get("/api/status", headers={"X-Auth-Token": token})
    v1 = client.get("/api/v1/status", headers={"X-Auth-Token": token})
    assert legacy.status_code == 200, legacy.text
    assert v1.status_code == 200, v1.text
    assert legacy.json() == v1.json()


def test_unknown_v1_path_404s_not_500(client):
    resp = client.get("/api/v1/this-route-does-not-exist")
    assert resp.status_code == 404


# ── Task 2: OpenAPI polish ───────────────────────────────────────────────


def test_openapi_has_polished_metadata(client):
    schema = client.get("/openapi.json").json()
    info = schema["info"]
    assert info["title"] == "Seekr"
    assert "Home Assistant" in info["description"]


def test_openapi_lists_both_legacy_and_v1_paths(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/login" in paths
    assert "/api/v1/login" in paths
    assert "/api/status" in paths
    assert "/api/v1/status" in paths


def test_openapi_v1_path_mirrors_legacy_operation(client):
    paths = client.get("/openapi.json").json()["paths"]
    legacy = paths["/api/login"]["post"]
    v1 = paths["/api/v1/login"]["post"]
    # Distinct operationId, but identical request body.
    assert v1["operationId"] == legacy["operationId"] + "_v1"
    assert v1["operationId"] != legacy["operationId"]
    assert v1.get("requestBody") == legacy.get("requestBody")


def test_openapi_legacy_paths_carry_deprecation_note(client):
    paths = client.get("/openapi.json").json()["paths"]
    legacy = paths["/api/login"]["post"]
    v1 = paths["/api/v1/login"]["post"]
    assert "Deprecated path prefix" in (legacy.get("description") or "")
    assert "Deprecated path prefix" not in (v1.get("description") or "")


def test_openapi_tags_are_declared(client):
    schema = client.get("/openapi.json").json()
    declared = {t["name"] for t in schema.get("tags", [])}
    assert "auth" in declared
    assert "documents" in declared
    assert "auth" in paths_tags(schema, "/api/login", "post")
    assert "documents" in paths_tags(schema, "/api/documents/mark", "post")


def paths_tags(schema, path, method):
    return schema["paths"][path][method].get("tags", [])


# ── Task 3: wiki contract note ───────────────────────────────────────────


def test_wiki_page_renders_and_mentions_v1_contract(client):
    resp = client.get("/wiki")
    assert resp.status_code == 200
    body = resp.text
    assert 'src="/docs"' in body
    assert "/api/v1" in body
