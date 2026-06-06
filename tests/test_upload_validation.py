"""Upload-endpoint validation tests: extension allow-list, oversize metadata,
and path-traversal rejection on target_subpath.

The upload root is redirected into tmp_path via the DOCUMENT_SEARCH_UPLOAD_ROOT
env var set BEFORE create_app, so nothing is written outside the temp dir.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi.testclient import TestClient  # noqa: E402

from document_search.app import create_app  # noqa: E402


def _login(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    monkeypatch.setenv("DOCUMENT_SEARCH_UPLOAD_ROOT", str(upload_root))
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as c:
        c.upload_root = upload_root  # stash for assertions
        yield c


def test_upload_rejects_disallowed_extension(client):
    token = _login(client)
    r = client.post(
        "/api/upload",
        headers={"X-Auth-Token": token},
        files={"file": ("evil.exe", b"MZ\x90\x00", "application/octet-stream")},
        data={"target_subpath": "", "tags": "", "metadata_json": "{}"},
    )
    assert r.status_code == 400
    assert "Unsupported file extension" in r.text


def test_upload_rejects_oversize_metadata(client):
    token = _login(client)
    big = "{" + '"k":"' + ("x" * 9000) + '"}'  # > 8192 bytes
    r = client.post(
        "/api/upload",
        headers={"X-Auth-Token": token},
        files={"file": ("ok.txt", b"hello world", "text/plain")},
        data={"target_subpath": "", "tags": "", "metadata_json": big},
    )
    assert r.status_code == 400
    assert "8 KB" in r.text


@pytest.mark.parametrize("evil", ["../escape", "../../etc", "/etc", "sub/../../escape"])
def test_upload_rejects_path_traversal_subpath(client, evil):
    token = _login(client)
    r = client.post(
        "/api/upload",
        headers={"X-Auth-Token": token},
        files={"file": ("ok.txt", b"hello", "text/plain")},
        data={"target_subpath": evil, "tags": "", "metadata_json": "{}"},
    )
    assert r.status_code == 400
    assert "Invalid target_subpath" in r.text
    # Nothing escaped the upload root.
    escaped = list((client.upload_root.parent).glob("escape*"))
    assert escaped == []


def test_upload_accepts_valid_txt_into_upload_root(client):
    token = _login(client)
    r = client.post(
        "/api/upload",
        headers={"X-Auth-Token": token},
        files={"file": ("note.txt", b"indexable body text", "text/plain")},
        data={"target_subpath": "inbox", "tags": "alpha,beta", "metadata_json": "{}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "uploaded"
    assert "ai_suggestion" in body
    written = list((client.upload_root / "inbox").glob("note_*.txt"))
    assert len(written) == 1


def test_upload_requires_authentication(client):
    r = client.post(
        "/api/upload",
        files={"file": ("ok.txt", b"hello", "text/plain")},
        data={"target_subpath": "", "tags": "", "metadata_json": "{}"},
    )
    assert r.status_code in (401, 403)
