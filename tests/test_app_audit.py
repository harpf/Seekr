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


def test_search_writes_audit_row(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        r = client.post("/api/search", headers={"X-Auth-Token": token},
                        json={"query": "quarterly report", "limit": 5})
        assert r.status_code == 200, r.text
        from document_search.index.sqlite_store import SqliteStore
        db = SqliteStore(tmp_path / "t.db")
        rows = db.list_audit(action="search")
        assert len(rows) == 1
        assert rows[0]["target_type"] == "query"
        assert rows[0]["detail"]["query"] == "quarterly report"
        assert "result_count" in rows[0]["detail"]
        # Actor is the admin user (id 1 on a fresh DB)
        assert rows[0]["actor_user_id"] == 1


def test_file_open_writes_audit_row(tmp_path):
    # Create a real file on disk and a document row pointing at it.
    target = tmp_path / "doc.txt"
    target.write_text("hello", encoding="utf-8")
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        from datetime import UTC, datetime

        from document_search.index.sqlite_store import SqliteStore
        db = SqliteStore(tmp_path / "t.db")
        now = datetime.now(tz=UTC).isoformat()
        db.conn.execute(
            "INSERT INTO documents(path, filename, extension, file_size, modified_at, sha256, indexed_at, status) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (str(target), "doc.txt", ".txt", 5, now, "h", now, "ok"),
        )
        db.conn.commit()
        # Grant 'public' read on the freshly-inserted doc so the route's ACL
        # check passes (mirrors what the app does for existing docs at startup).
        db._backfill_acl()
        doc_id = db.conn.execute("SELECT id FROM documents WHERE path=?", (str(target),)).fetchone()[0]
        r = client.get(f"/api/files/open?document_id={doc_id}", headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        rows = db.list_audit(action="file.open")
        assert len(rows) == 1
        assert rows[0]["target_type"] == "document"
        assert rows[0]["target_id"] == str(doc_id)
