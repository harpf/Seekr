import io
import time

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


def test_restore_endpoint_returns_restart_required(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        created = client.post("/api/backup/run", headers={"X-Auth-Token": token})
        assert created.status_code == 200, created.text
        filename = created.json()["filename"]

        r = client.post(
            "/api/backup/restore",
            headers={"X-Auth-Token": token},
            json={"filename": filename},
        )
        assert r.status_code == 200, r.text
        assert r.json()["restart_required"] is True


def test_restore_rejects_bad_filename(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        r = client.post(
            "/api/backup/restore",
            headers={"X-Auth-Token": token},
            json={"filename": "../../etc/passwd"},
        )
        assert r.status_code == 400, r.text


def test_restore_requires_admin(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        admin = _login(client)
        client.post(
            "/api/users",
            headers={"X-Auth-Token": admin},
            json={"username": "carol", "password": "carolpassword1", "role": "user"},
        )
        user_token = _login(client, "carol", "carolpassword1")
        r = client.post(
            "/api/backup/restore",
            headers={"X-Auth-Token": user_token},
            json={"filename": "document_index_x.db"},
        )
        assert r.status_code == 403, r.text


def test_export_then_import_endpoints(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)

        # Non-admin is rejected from both endpoints.
        client.post(
            "/api/users",
            headers={"X-Auth-Token": token},
            json={"username": "dave", "password": "davepassword1", "role": "user"},
        )
        user_token = _login(client, "dave", "davepassword1")
        assert client.get(
            "/api/backup/export", headers={"X-Auth-Token": user_token}
        ).status_code == 403

        # Export produces a zip archive.
        exp = client.get("/api/backup/export", headers={"X-Auth-Token": token})
        assert exp.status_code == 200, exp.text
        assert exp.headers["content-type"] == "application/zip"
        archive = exp.content
        assert archive[:2] == b"PK"

        # Re-importing the same archive succeeds (additive/idempotent).
        imp = client.post(
            "/api/backup/import",
            headers={"X-Auth-Token": token},
            files={"file": ("seekr_export.zip", io.BytesIO(archive), "application/zip")},
        )
        assert imp.status_code == 200, imp.text
        assert "imported" in imp.json()

        # Non-admin cannot import.
        imp_forbidden = client.post(
            "/api/backup/import",
            headers={"X-Auth-Token": user_token},
            files={"file": ("seekr_export.zip", io.BytesIO(archive), "application/zip")},
        )
        assert imp_forbidden.status_code == 403, imp_forbidden.text

        # A non-zip upload is rejected with 400.
        bad = client.post(
            "/api/backup/import",
            headers={"X-Auth-Token": token},
            files={"file": ("not.zip", io.BytesIO(b"not a zip"), "application/zip")},
        )
        assert bad.status_code == 400, bad.text


def test_scheduled_backup_job_runs(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        job_id = app.state.job_store.enqueue("backup", {})

        deadline = time.monotonic() + 10
        state = None
        while time.monotonic() < deadline:
            job = app.state.job_store.get(job_id)
            state = job["state"]
            if state in ("succeeded", "failed"):
                break
            time.sleep(0.05)
        assert state == "succeeded", f"backup job did not succeed: {state}"

        listed = client.get("/api/backups", headers={"X-Auth-Token": token})
        assert listed.status_code == 200, listed.text
        assert len(listed.json()["backups"]) >= 1
