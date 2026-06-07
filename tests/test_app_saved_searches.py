import pytest

pytestmark = pytest.mark.skipif(
    not pytest.importorskip("fastapi", reason="fastapi not installed"),
    reason="fastapi required",
)

from fastapi.testclient import TestClient

from document_search.app import create_app


def _token(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _headers(token):
    return {"X-Auth-Token": token}


def _make_second_user(client, admin_token, username="bob", password="bobpassword1"):
    r = client.post(
        "/api/users",
        headers=_headers(admin_token),
        json={"username": username, "password": password, "role": "user"},
    )
    assert r.status_code in (200, 201), r.text
    return _token(client, username, password)


# ---------------------------------------------------------------------------
# History endpoints + auto-record
# ---------------------------------------------------------------------------


def test_history_starts_empty(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _token(client)
        r = client.get("/api/search/history", headers=_headers(token))
        assert r.status_code == 200, r.text
        assert r.json() == []


def test_search_auto_records_history(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _token(client)
        r = client.post(
            "/api/search",
            headers=_headers(token),
            json={"query": "invoice", "filetype": "pdf"},
        )
        assert r.status_code == 200, r.text
        assert isinstance(r.json(), list)

        hist = client.get("/api/search/history", headers=_headers(token)).json()
        assert len(hist) == 1
        assert hist[0]["query"] == "invoice"
        assert hist[0]["filters"]["filetype"] == "pdf"


def test_search_empty_query_not_recorded(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _token(client)
        r = client.post("/api/search", headers=_headers(token), json={"query": ""})
        assert r.status_code == 200, r.text

        hist = client.get("/api/search/history", headers=_headers(token)).json()
        assert hist == []


def test_clear_history(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _token(client)
        client.post("/api/search", headers=_headers(token), json={"query": "alpha"})
        client.post("/api/search", headers=_headers(token), json={"query": "beta"})
        assert len(client.get("/api/search/history", headers=_headers(token)).json()) == 2

        r = client.delete("/api/search/history", headers=_headers(token))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok"
        assert body["removed"] == 2

        assert client.get("/api/search/history", headers=_headers(token)).json() == []


def test_history_is_per_user(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        admin_token = _token(client)
        bob_token = _make_second_user(client, admin_token)

        a = client.post("/api/search", headers=_headers(admin_token), json={"query": "adminquery"})
        b = client.post("/api/search", headers=_headers(bob_token), json={"query": "bobquery"})
        assert a.status_code == 200, a.text
        assert b.status_code == 200, b.text

        admin_hist = client.get("/api/search/history", headers=_headers(admin_token)).json()
        bob_hist = client.get("/api/search/history", headers=_headers(bob_token)).json()

        assert [h["query"] for h in admin_hist] == ["adminquery"]
        assert [h["query"] for h in bob_hist] == ["bobquery"]


def test_history_requires_auth(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        assert client.get("/api/search/history").status_code == 401
        assert client.delete("/api/search/history").status_code == 401
