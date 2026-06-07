import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from document_search.app import create_app


def _login(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_audit_helper_swallows_errors(tmp_path, monkeypatch):
    """The _audit wrapper must never propagate a logging failure. We make
    record_audit raise and confirm a search still returns 200."""
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        # Force the per-thread store's record_audit to blow up.
        from document_search.index.sqlite_store import SqliteStore
        orig = SqliteStore.record_audit

        def boom(self, *a, **k):
            raise RuntimeError("audit backend down")

        monkeypatch.setattr(SqliteStore, "record_audit", boom)
        r = client.post("/api/search", headers={"X-Auth-Token": token},
                        json={"query": "anything", "limit": 5})
        assert r.status_code == 200, r.text
        monkeypatch.setattr(SqliteStore, "record_audit", orig)
