import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from document_search.app import create_app


def _login(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_embed_index_handler_registered(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    assert "embed_index" in app.state.worker._handlers


def test_embed_index_noop_when_flag_disabled(tmp_path, monkeypatch):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        r = client.post("/api/index/embeddings/start", headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            jr = client.get(f"/api/index/jobs/{job_id}", headers={"X-Auth-Token": token})
            if jr.json()["status"] in ("finished", "failed", "interrupted"):
                break
            time.sleep(0.05)
        from document_search.index.sqlite_store import SqliteStore
        s = SqliteStore(tmp_path / "t.db")
        n = s.conn.execute("SELECT COUNT(*) FROM block_embeddings").fetchone()[0]
        assert n == 0


def test_search_mode_param_downgrades_when_flag_off(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        r = client.post(
            "/api/search",
            headers={"X-Auth-Token": token},
            json={"query": "anything", "mode": "hybrid"},
        )
        assert r.status_code == 200, r.text
        assert isinstance(r.json(), list)
