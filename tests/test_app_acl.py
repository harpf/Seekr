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


from datetime import UTC, datetime  # noqa: E402
from pathlib import Path  # noqa: E402

from document_search.index.sqlite_store import SqliteStore  # noqa: E402


def _seed_doc(db_path, path="/d/seed.pdf"):
    store = SqliteStore(Path(db_path))
    now = datetime.now(tz=UTC).isoformat()
    cur = store.conn.execute(
        "INSERT INTO documents(path, filename, extension, file_size, modified_at, "
        "sha256, indexed_at, status) VALUES(?,?,?,?,?,?,?,?)",
        (path, Path(path).name, ".pdf", 1, now, "hS", now, "ok"),
    )
    store.conn.commit()
    return cur.lastrowid


def test_grant_and_list_document_acl(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    doc = _seed_doc(db)
    with TestClient(app) as client:
        tok = _login(client)
        gid = client.post("/api/groups", headers={"X-Auth-Token": tok},
                          json={"name": "editors"}).json()["id"]
        r = client.post("/api/acl/grant", headers={"X-Auth-Token": tok},
                        json={"document_id": doc, "principal_id": gid, "permission": "write"})
        assert r.status_code == 200, r.text

        r = client.get(f"/api/acl/documents/{doc}", headers={"X-Auth-Token": tok})
        assert r.status_code == 200
        entries = r.json()
        assert any(e["principal_id"] == gid and e["permission"] == "write" for e in entries)


def test_revoke_document_acl(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    doc = _seed_doc(db)
    with TestClient(app) as client:
        tok = _login(client)
        gid = client.post("/api/groups", headers={"X-Auth-Token": tok},
                          json={"name": "editors"}).json()["id"]
        client.post("/api/acl/grant", headers={"X-Auth-Token": tok},
                    json={"document_id": doc, "principal_id": gid, "permission": "read"})
        r = client.post("/api/acl/revoke", headers={"X-Auth-Token": tok},
                        json={"document_id": doc, "principal_id": gid, "permission": "read"})
        assert r.status_code == 200
        entries = client.get(f"/api/acl/documents/{doc}", headers={"X-Auth-Token": tok}).json()
        assert not any(e["principal_id"] == gid for e in entries)


def test_grant_bad_permission_returns_400(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    doc = _seed_doc(db)
    with TestClient(app) as client:
        tok = _login(client)
        gid = client.post("/api/groups", headers={"X-Auth-Token": tok},
                          json={"name": "editors"}).json()["id"]
        r = client.post("/api/acl/grant", headers={"X-Auth-Token": tok},
                        json={"document_id": doc, "principal_id": gid, "permission": "execute"})
        assert r.status_code == 400


def test_grant_unknown_document_returns_400(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    with TestClient(app) as client:
        tok = _login(client)
        gid = client.post("/api/groups", headers={"X-Auth-Token": tok},
                          json={"name": "editors"}).json()["id"]
        r = client.post("/api/acl/grant", headers={"X-Auth-Token": tok},
                        json={"document_id": 999999, "principal_id": gid, "permission": "read"})
        assert r.status_code == 400


def test_acl_grant_requires_admin(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    doc = _seed_doc(db)
    with TestClient(app) as client:
        admin = _login(client)
        _create_user(client, admin, "bob")
        gid = client.post("/api/groups", headers={"X-Auth-Token": admin},
                          json={"name": "editors"}).json()["id"]
        bob = _login(client, "bob", "pw-123456")
        r = client.post("/api/acl/grant", headers={"X-Auth-Token": bob},
                        json={"document_id": doc, "principal_id": gid, "permission": "read"})
        assert r.status_code == 403


def test_acl_list_404_for_unknown_document(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    with TestClient(app) as client:
        tok = _login(client)
        r = client.get("/api/acl/documents/999999", headers={"X-Auth-Token": tok})
        assert r.status_code == 404
