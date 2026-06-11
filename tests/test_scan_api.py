import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from document_search.app import create_app  # noqa: E402


def _admin_client(tmp_path, monkeypatch, extra_cfg=None):
    db_path = tmp_path / "document_index.db"
    cfg = {"database_path": str(db_path)}
    cfg.update(extra_cfg or {})
    config = tmp_path / "config.json"
    config.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("DOCUMENT_SEARCH_CONFIG_PATH", str(config))
    app = create_app(str(db_path))
    client = TestClient(app)
    r = client.post("/api/login", json={"username": "admin", "password": "admin"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    return app, client, token, config


def test_path_test_route_validates_inbox(tmp_path, monkeypatch):
    app, client, token, _ = _admin_client(tmp_path, monkeypatch)
    inbox = tmp_path / "in"
    inbox.mkdir()
    target = tmp_path / "out"
    target.mkdir()
    r = client.post("/api/scan/inboxes/test",
                    headers={"X-Auth-Token": token},
                    json={"inbox_path": str(inbox), "target_root": str(target)})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_path_test_rejects_inbox_inside_target(tmp_path, monkeypatch):
    app, client, token, _ = _admin_client(tmp_path, monkeypatch)
    target = tmp_path / "out"
    (target / "in").mkdir(parents=True)
    r = client.post("/api/scan/inboxes/test",
                    headers={"X-Auth-Token": token},
                    json={"inbox_path": str(target / "in"), "target_root": str(target)})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "inside" in r.json()["error"]


def test_save_config_persists_scan_inboxes_and_derives_id(tmp_path, monkeypatch):
    inbox = tmp_path / "in"
    inbox.mkdir()
    target = tmp_path / "out"
    target.mkdir()
    app, client, token, config = _admin_client(tmp_path, monkeypatch)
    # Build a minimal valid UiConfig payload. Read what fields UiConfigRequest requires
    # and include them; here we send the existing config plus scan_inboxes.
    payload = {
        "database_path": str(tmp_path / "document_index.db"),
        "supported_extensions": [".pdf"],
        "exclude_dirs": [],
        "exclude_patterns": [],
        "max_file_size_mb": 100,
        "scan_inboxes": [{"label": "Scan HR", "inbox_path": str(inbox), "target_root": str(target)}],
    }
    r = client.post("/api/config", headers={"X-Auth-Token": token}, json=payload)
    assert r.status_code == 200, r.text
    saved = json.loads(config.read_text(encoding="utf-8"))
    assert saved["scan_inboxes"][0]["id"] == "scan-hr"  # derived from label
    assert saved["scan_inboxes"][0]["inbox_path"] == str(inbox)


def test_path_test_requires_auth(tmp_path, monkeypatch):
    app, client, token, _ = _admin_client(tmp_path, monkeypatch)
    r = client.post("/api/scan/inboxes/test",
                    json={"inbox_path": str(tmp_path), "target_root": str(tmp_path)})
    assert r.status_code == 401


def test_save_config_rejects_invalid_scan_inbox(tmp_path, monkeypatch):
    target = tmp_path / "out"
    (target / "in").mkdir(parents=True)
    app, client, token, config = _admin_client(tmp_path, monkeypatch)
    payload = {
        "database_path": str(tmp_path / "document_index.db"),
        "supported_extensions": [".pdf"],
        "exclude_dirs": [],
        "exclude_patterns": [],
        "max_file_size_mb": 100,
        "scan_inboxes": [{"label": "Bad", "inbox_path": str(target / "in"), "target_root": str(target)}],
    }
    r = client.post("/api/config", headers={"X-Auth-Token": token}, json=payload)
    assert r.status_code == 400


def test_save_config_without_scan_inboxes_preserves_existing(tmp_path, monkeypatch):
    """Omitting scan_inboxes from the payload must not wipe the previously-saved inboxes."""
    inbox = tmp_path / "in"
    inbox.mkdir()
    target = tmp_path / "out"
    target.mkdir()
    app, client, token, config = _admin_client(tmp_path, monkeypatch)
    # First save: persist a valid scan inbox.
    first_payload = {
        "database_path": str(tmp_path / "document_index.db"),
        "supported_extensions": [".pdf"],
        "exclude_dirs": [],
        "exclude_patterns": [],
        "max_file_size_mb": 100,
        "scan_inboxes": [{"label": "Scan HR", "inbox_path": str(inbox), "target_root": str(target)}],
    }
    r = client.post("/api/config", headers={"X-Auth-Token": token}, json=first_payload)
    assert r.status_code == 200, r.text
    # Second save: General-tab style — scan_inboxes key omitted entirely.
    second_payload = {
        "database_path": str(tmp_path / "document_index.db"),
        "supported_extensions": [".pdf", ".txt"],
        "exclude_dirs": [],
        "exclude_patterns": [],
        "max_file_size_mb": 50,
    }
    r2 = client.post("/api/config", headers={"X-Auth-Token": token}, json=second_payload)
    assert r2.status_code == 200, r2.text
    saved = json.loads(config.read_text(encoding="utf-8"))
    assert saved["scan_inboxes"], "scan_inboxes must not be wiped when omitted from payload"
    assert saved["scan_inboxes"][0]["id"] == "scan-hr"


def test_save_config_without_scan_inboxes_does_not_revalidate_stale_path(tmp_path, monkeypatch):
    """When scan_inboxes is omitted, a stale (deleted) inbox path must NOT cause a 400."""
    inbox = tmp_path / "in"
    inbox.mkdir()
    target = tmp_path / "out"
    target.mkdir()
    app, client, token, config = _admin_client(tmp_path, monkeypatch)
    # Persist a config that references the inbox path.
    first_payload = {
        "database_path": str(tmp_path / "document_index.db"),
        "supported_extensions": [".pdf"],
        "exclude_dirs": [],
        "exclude_patterns": [],
        "max_file_size_mb": 100,
        "scan_inboxes": [{"label": "Scan Test", "inbox_path": str(inbox), "target_root": str(target)}],
    }
    r = client.post("/api/config", headers={"X-Auth-Token": token}, json=first_payload)
    assert r.status_code == 200, r.text
    # Delete the inbox directory so any path validation would now fail.
    inbox.rmdir()
    # General-tab save without scan_inboxes — must succeed (no revalidation).
    second_payload = {
        "database_path": str(tmp_path / "document_index.db"),
        "supported_extensions": [".pdf"],
        "exclude_dirs": [],
        "exclude_patterns": [],
        "max_file_size_mb": 100,
    }
    r2 = client.post("/api/config", headers={"X-Auth-Token": token}, json=second_payload)
    assert r2.status_code == 200, r2.text
