import io
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


def _upload(client, token, filename, content: bytes, subpath=""):
    return client.post(
        "/api/upload",
        headers={"X-Auth-Token": token},
        files={"file": (filename, io.BytesIO(content), "application/octet-stream")},
        data={"target_subpath": subpath},
    )


def test_upload_rejects_exe_renamed_to_pdf(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_UPLOAD_ROOT", str(tmp_path / "uploads"))
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        exe_bytes = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 64
        r = _upload(client, token, "report.pdf", exe_bytes)
        assert r.status_code == 400, r.text
        assert "match" in r.json()["detail"].lower() or "content" in r.json()["detail"].lower()


def test_upload_rejects_traversal_subpath(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_UPLOAD_ROOT", str(tmp_path / "uploads"))
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        r = _upload(client, token, "note.txt", b"hello text", subpath="../../etc")
        assert r.status_code == 400, r.text
        assert "subpath" in r.json()["detail"].lower() or "invalid" in r.json()["detail"].lower()


def test_upload_accepts_real_text_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_UPLOAD_ROOT", str(tmp_path / "uploads"))
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        r = _upload(client, token, "note.txt", b"a genuine plain text note\n")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "uploaded"
