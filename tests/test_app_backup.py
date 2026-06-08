import pytest

pytestmark = pytest.mark.skipif(
    not pytest.importorskip("fastapi", reason="fastapi not installed"),
    reason="fastapi required",
)

from fastapi.testclient import TestClient

from document_search.app import create_app


def _login(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_backup_run_creates_file_and_lists_it(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        r = client.post("/api/backup/run", headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["filename"].startswith("document_index_")
        assert body["filename"].endswith(".db")
        assert body["size_bytes"] > 0

        listed = client.get("/api/backups", headers={"X-Auth-Token": token})
        assert listed.status_code == 200, listed.text
        names = [b["filename"] for b in listed.json()["backups"]]
        assert body["filename"] in names


def test_backup_run_requires_admin(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        admin = _login(client)
        # Create a non-admin user (real route is POST /api/users).
        r = client.post(
            "/api/users",
            headers={"X-Auth-Token": admin},
            json={"username": "bob", "password": "bobpassword1", "role": "user"},
        )
        assert r.status_code == 200, r.text
        user_token = _login(client, "bob", "bobpassword1")

        run = client.post("/api/backup/run", headers={"X-Auth-Token": user_token})
        assert run.status_code == 403, run.text
        listed = client.get("/api/backups", headers={"X-Auth-Token": user_token})
        assert listed.status_code == 403, listed.text
