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


def _create_user(client, admin_token, username, password="pw-123456", role="user"):
    r = client.post(
        "/api/users",
        headers={"X-Auth-Token": admin_token},
        json={"username": username, "password": password, "role": role},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_create_list_delete_group(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        tok = _login(client)
        r = client.post("/api/groups", headers={"X-Auth-Token": tok},
                        json={"name": "editors", "display_name": "Editors"})
        assert r.status_code == 200, r.text
        gid = r.json()["id"]
        assert isinstance(gid, int)

        r = client.get("/api/groups", headers={"X-Auth-Token": tok})
        assert r.status_code == 200
        names = {g["external_id"] for g in r.json()}
        assert "editors" in names and "public" in names

        r = client.delete(f"/api/groups/{gid}", headers={"X-Auth-Token": tok})
        assert r.status_code == 200
        r = client.get("/api/groups", headers={"X-Auth-Token": tok})
        assert "editors" not in {g["external_id"] for g in r.json()}


def test_delete_public_group_returns_400(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        tok = _login(client)
        public_id = next(
            g["id"] for g in client.get("/api/groups", headers={"X-Auth-Token": tok}).json()
            if g["external_id"] == "public"
        )
        r = client.delete(f"/api/groups/{public_id}", headers={"X-Auth-Token": tok})
        assert r.status_code == 400


def test_group_membership_add_remove_list(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        tok = _login(client)
        uid = _create_user(client, tok, "alice")
        gid = client.post("/api/groups", headers={"X-Auth-Token": tok},
                          json={"name": "editors"}).json()["id"]

        r = client.post(f"/api/groups/{gid}/members",
                        headers={"X-Auth-Token": tok}, json={"user_id": uid})
        assert r.status_code == 200, r.text

        r = client.get(f"/api/groups/{gid}/members", headers={"X-Auth-Token": tok})
        assert r.status_code == 200
        assert {m["username"] for m in r.json()} == {"alice"}

        r = client.delete(f"/api/groups/{gid}/members/{uid}", headers={"X-Auth-Token": tok})
        assert r.status_code == 200
        r = client.get(f"/api/groups/{gid}/members", headers={"X-Auth-Token": tok})
        assert r.json() == []


def test_groups_require_admin(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        admin = _login(client)
        _create_user(client, admin, "bob")
        bob = _login(client, "bob", "pw-123456")
        r = client.get("/api/groups", headers={"X-Auth-Token": bob})
        assert r.status_code == 403
        r = client.post("/api/groups", headers={"X-Auth-Token": bob}, json={"name": "x"})
        assert r.status_code == 403
