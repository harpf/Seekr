"""Tests for the in-app update flow (/api/update/run, /api/update/status).

subprocess.run is monkeypatched so no real update.sh executes and the running
process is never replaced. The update script's existence is faked so the route
proceeds past its `script.exists()` guard.
"""
from __future__ import annotations

import subprocess
import time

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi.testclient import TestClient  # noqa: E402

import document_search.app as app_module  # noqa: E402
from document_search.app import create_app  # noqa: E402


def _login(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_UI_UPDATE_ENABLED", "true")
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as c:
        yield c


def _wait_for_status(client, token, terminal, timeout=5.0):
    deadline = time.monotonic() + timeout
    body = {}
    while time.monotonic() < deadline:
        body = client.get("/api/update/status", headers={"X-Auth-Token": token}).json()
        if body.get("status") in terminal:
            return body
        time.sleep(0.02)
    return body


def test_update_run_success_records_done(client, monkeypatch):
    token = _login(client)
    # The route checks Path("/app/scripts/update.sh").exists(); force it True.
    monkeypatch.setattr(app_module.Path, "exists", lambda self: True)

    def fake_run(cmd, **kwargs):
        assert cmd[0] == "/bin/sh"
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="update ok", stderr="")

    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    r = client.post("/api/update/run", headers={"X-Auth-Token": token})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "started"

    final = _wait_for_status(client, token, {"done", "error"})
    assert final["status"] == "done"
    assert final["exit_code"] == 0
    assert "update ok" in final["stdout"]


def test_update_run_nonzero_records_error(client, monkeypatch):
    token = _login(client)
    monkeypatch.setattr(app_module.Path, "exists", lambda self: True)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="git pull failed")

    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    client.post("/api/update/run", headers={"X-Auth-Token": token})
    final = _wait_for_status(client, token, {"done", "error"})
    assert final["status"] == "error"
    assert final["exit_code"] == 1
    assert "git pull failed" in final["stderr"]


def test_update_run_requires_admin(client, monkeypatch):
    # A non-admin user must be rejected before any subprocess is touched.
    called = {"ran": False}
    monkeypatch.setattr(app_module.Path, "exists", lambda self: True)

    def fake_run(cmd, **kwargs):
        called["ran"] = True
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    admin_token = _login(client)
    client.post(
        "/api/users",
        headers={"X-Auth-Token": admin_token},
        json={"username": "bob", "password": "bobpassword", "role": "user"},
    )
    user_token = _login(client, "bob", "bobpassword")
    r = client.post("/api/update/run", headers={"X-Auth-Token": user_token})
    assert r.status_code == 403
    assert called["ran"] is False


def test_update_run_disabled_by_feature_flag(client, monkeypatch):
    token = _login(client)
    monkeypatch.setenv("DOCUMENT_SEARCH_UI_UPDATE_ENABLED", "false")
    monkeypatch.setattr(app_module.Path, "exists", lambda self: True)
    r = client.post("/api/update/run", headers={"X-Auth-Token": token})
    assert r.status_code == 403
    assert "disabled" in r.text.lower()


def test_update_run_missing_script_is_404(client, monkeypatch):
    token = _login(client)
    monkeypatch.setattr(app_module.Path, "exists", lambda self: False)
    r = client.post("/api/update/run", headers={"X-Auth-Token": token})
    assert r.status_code == 404
