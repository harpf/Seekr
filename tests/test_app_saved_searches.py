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


# ---------------------------------------------------------------------------
# Saved-search endpoints
# ---------------------------------------------------------------------------


def test_saved_starts_empty(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _token(client)
        r = client.get("/api/search/saved", headers=_headers(token))
        assert r.status_code == 200, r.text
        assert r.json() == []


def test_create_and_list_saved_search(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _token(client)
        r = client.post(
            "/api/search/saved",
            headers=_headers(token),
            json={"name": "My Invoices", "query": "invoice", "filters": {"filetype": "pdf"}},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert isinstance(body["id"], int)
        assert body["name"] == "My Invoices"
        assert body["query"] == "invoice"
        assert body["filters"] == {"filetype": "pdf"}

        listed = client.get("/api/search/saved", headers=_headers(token)).json()
        assert len(listed) == 1
        assert listed[0]["name"] == "My Invoices"
        assert listed[0]["query"] == "invoice"
        assert listed[0]["filters"] == {"filetype": "pdf"}


def test_create_saved_search_rejects_blank_name(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _token(client)
        r = client.post(
            "/api/search/saved",
            headers=_headers(token),
            json={"name": "   ", "query": "x"},
        )
        assert r.status_code == 400, r.text
        assert client.get("/api/search/saved", headers=_headers(token)).json() == []


def test_create_saved_search_duplicate_name_conflicts(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _token(client)
        first = client.post(
            "/api/search/saved",
            headers=_headers(token),
            json={"name": "Reports", "query": "report"},
        )
        assert first.status_code == 200, first.text
        dup = client.post(
            "/api/search/saved",
            headers=_headers(token),
            json={"name": "Reports", "query": "other"},
        )
        assert dup.status_code == 409, dup.text


def test_delete_saved_search(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _token(client)
        created = client.post(
            "/api/search/saved",
            headers=_headers(token),
            json={"name": "Temp", "query": "temp"},
        ).json()
        saved_id = created["id"]

        r = client.delete(f"/api/search/saved/{saved_id}", headers=_headers(token))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "deleted"
        assert body["id"] == saved_id

        assert client.get("/api/search/saved", headers=_headers(token)).json() == []


def test_delete_saved_search_unknown_id_404(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _token(client)
        r = client.delete("/api/search/saved/99999", headers=_headers(token))
        assert r.status_code == 404, r.text


def test_saved_search_is_per_user_and_delete_isolated(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        admin_token = _token(client)
        bob_token = _make_second_user(client, admin_token)

        admin_saved = client.post(
            "/api/search/saved",
            headers=_headers(admin_token),
            json={"name": "Shared Name", "query": "admin"},
        ).json()
        bob_saved = client.post(
            "/api/search/saved",
            headers=_headers(bob_token),
            json={"name": "Shared Name", "query": "bob"},
        )
        # Same name is fine across different users
        assert bob_saved.status_code == 200, bob_saved.text

        # Each user only sees their own
        admin_list = client.get("/api/search/saved", headers=_headers(admin_token)).json()
        bob_list = client.get("/api/search/saved", headers=_headers(bob_token)).json()
        assert [s["query"] for s in admin_list] == ["admin"]
        assert [s["query"] for s in bob_list] == ["bob"]

        # Bob cannot delete admin's saved search
        r = client.delete(
            f"/api/search/saved/{admin_saved['id']}", headers=_headers(bob_token)
        )
        assert r.status_code == 404, r.text
        # Admin's saved search is untouched
        assert len(client.get("/api/search/saved", headers=_headers(admin_token)).json()) == 1


def test_saved_requires_auth(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        assert client.get("/api/search/saved").status_code == 401
        assert client.post("/api/search/saved", json={"name": "x"}).status_code == 401
        assert client.delete("/api/search/saved/1").status_code == 401
