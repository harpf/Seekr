from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from document_search.app import create_app


def _login(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_create_user_duplicate_does_not_leak_exception_text(tmp_path):
    """Creating a duplicate user must return a generic message, NOT the raw
    sqlite3 IntegrityError / UNIQUE-constraint string."""
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        r1 = client.post(
            "/api/users",
            headers={"X-Auth-Token": token},
            json={"username": "dupe", "password": "password123", "role": "user"},
        )
        assert r1.status_code == 200, r1.text
        r2 = client.post(
            "/api/users",
            headers={"X-Auth-Token": token},
            json={"username": "dupe", "password": "password123", "role": "user"},
        )
        assert r2.status_code == 400
        detail = r2.json()["detail"].lower()
        for leak in ("unique", "constraint", "sqlite", "integrityerror", "traceback"):
            assert leak not in detail, f"leaked internal detail: {detail!r}"


def test_dockerfile_runs_as_non_root():
    """The Dockerfile must create a dedicated user and drop to it before CMD."""
    text = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "useradd" in text or "adduser" in text, "no user creation in Dockerfile"
    assert "USER seekr" in text, "Dockerfile does not switch to the seekr user"
    user_idx = text.index("USER seekr")
    cmd_idx = text.rindex("CMD")
    assert user_idx < cmd_idx, "USER must precede CMD"
