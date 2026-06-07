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


def test_reindex_writes_audit_row(tmp_path):
    target = tmp_path / "r.txt"
    target.write_text("reindex me", encoding="utf-8")
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
            (str(target), "r.txt", ".txt", 10, now, "h", now, "ok"),
        )
        db.conn.commit()
        doc_id = db.conn.execute("SELECT id FROM documents WHERE path=?", (str(target),)).fetchone()[0]
        r = client.post(f"/api/documents/{doc_id}/reindex", headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        rows = db.list_audit(action="document.reindex")
        assert len(rows) == 1
        assert rows[0]["target_id"] == str(doc_id)


def test_cleanup_writes_audit_row(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        r = client.post("/api/index/cleanup", headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        from document_search.index.sqlite_store import SqliteStore
        db = SqliteStore(tmp_path / "t.db")
        rows = db.list_audit(action="documents.cleanup")
        assert len(rows) == 1
        assert "removed" in rows[0]["detail"]


def test_reorganize_apply_audits_moves(tmp_path, monkeypatch):
    """A successful move writes a document.move audit row carrying old + new path."""
    upload_root = tmp_path / "uploads"
    (upload_root / "sub").mkdir(parents=True)
    src = upload_root / "movable.txt"
    src.write_text("data", encoding="utf-8")
    monkeypatch.setenv("DOCUMENT_SEARCH_UPLOAD_ROOT", str(upload_root))
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
            (str(src), "movable.txt", ".txt", 4, now, "h", now, "ok"),
        )
        db.conn.commit()
        doc_id = db.conn.execute("SELECT id FROM documents WHERE path=?", (str(src),)).fetchone()[0]
        r = client.post("/api/ai/reorganize/apply", headers={"X-Auth-Token": token},
                        json={"moves": [{"document_id": doc_id, "new_subpath": "sub"}]})
        assert r.status_code == 200, r.text
        assert r.json()[0]["status"] == "moved"
        rows = db.list_audit(action="document.move")
        assert len(rows) == 1
        assert rows[0]["target_id"] == str(doc_id)
        assert "new_path" in rows[0]["detail"]


def test_user_create_and_delete_audited(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        r = client.post("/api/users", headers={"X-Auth-Token": token},
                        json={"username": "carol", "password": "carol-pass-1", "role": "user"})
        assert r.status_code == 200, r.text
        new_id = r.json()["id"]
        r = client.delete(f"/api/users/{new_id}", headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text

        from document_search.index.sqlite_store import SqliteStore
        db = SqliteStore(tmp_path / "t.db")
        created = db.list_audit(action="user.create")
        deleted = db.list_audit(action="user.delete")
        assert len(created) == 1 and created[0]["target_id"] == str(new_id)
        assert created[0]["detail"]["username"] == "carol"
        assert len(deleted) == 1 and deleted[0]["target_id"] == str(new_id)


def test_update_role_and_change_password_audited(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        new_id = client.post("/api/users", headers={"X-Auth-Token": token},
                             json={"username": "dave", "password": "dave-pass-1", "role": "user"}).json()["id"]
        assert client.put(f"/api/users/{new_id}", headers={"X-Auth-Token": token},
                          json={"role": "admin"}).status_code == 200
        assert client.post(f"/api/users/{new_id}/change-password", headers={"X-Auth-Token": token},
                           json={"new_password": "newer-pass-9"}).status_code == 200

        from document_search.index.sqlite_store import SqliteStore
        db = SqliteStore(tmp_path / "t.db")
        assert len(db.list_audit(action="user.update_role")) == 1
        pw = db.list_audit(action="user.change_password")
        assert len(pw) == 1
        # The new password must NEVER appear in the audit detail.
        assert "newer-pass-9" not in (pw[0]["detail_json"] or "")


def test_config_save_audited(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        r = client.post("/api/config", headers={"X-Auth-Token": token},
                        json={
                            "database_path": str(tmp_path / "t.db"),
                            "supported_extensions": [".pdf", ".txt"],
                            "exclude_dirs": [],
                            "exclude_patterns": [],
                            "max_file_size_mb": 50,
                            "ollama_url": "http://localhost:11434",
                            "ollama_model": "llama3",
                        })
        assert r.status_code == 200, r.text
        from document_search.index.sqlite_store import SqliteStore
        db = SqliteStore(tmp_path / "t.db")
        rows = db.list_audit(action="config.save")
        assert len(rows) == 1
        assert rows[0]["target_type"] == "config"


def test_audit_endpoint_requires_admin(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        admin_token = _login(client)
        # Create a non-admin user and log in as them.
        client.post("/api/users", headers={"X-Auth-Token": admin_token},
                    json={"username": "erin", "password": "erin-pass-1", "role": "user"})
        user_token = _login(client, "erin", "erin-pass-1")
        # Non-admin: 403
        r = client.get("/api/audit", headers={"X-Auth-Token": user_token})
        assert r.status_code == 403
        # No token: 401
        assert client.get("/api/audit").status_code == 401
        # Admin: 200
        assert client.get("/api/audit", headers={"X-Auth-Token": admin_token}).status_code == 200


def test_audit_endpoint_returns_items_and_total(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        for q in ("alpha", "beta", "gamma"):
            client.post("/api/search", headers={"X-Auth-Token": token}, json={"query": q, "limit": 5})
        r = client.get("/api/audit", headers={"X-Auth-Token": token})
        assert r.status_code == 200
        body = r.json()
        assert "items" in body and "total" in body
        assert body["total"] >= 3
        # Items are newest-first and carry decoded detail + actor_username.
        assert body["items"][0]["action"] == "search"
        assert body["items"][0]["actor_username"] == "admin"
        assert isinstance(body["items"][0]["detail"], dict)


def test_audit_endpoint_filters_by_action(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        client.post("/api/search", headers={"X-Auth-Token": token}, json={"query": "x", "limit": 5})
        client.post("/api/index/cleanup", headers={"X-Auth-Token": token})
        r = client.get("/api/audit?action=documents.cleanup", headers={"X-Auth-Token": token})
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert all(it["action"] == "documents.cleanup" for it in body["items"])


def test_audit_endpoint_pagination(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        for i in range(5):
            client.post("/api/search", headers={"X-Auth-Token": token}, json={"query": str(i), "limit": 5})
        p1 = client.get("/api/audit?limit=2&offset=0", headers={"X-Auth-Token": token}).json()
        p2 = client.get("/api/audit?limit=2&offset=2", headers={"X-Auth-Token": token}).json()
        assert len(p1["items"]) == 2 and len(p2["items"]) == 2
        ids1 = {it["id"] for it in p1["items"]}
        ids2 = {it["id"] for it in p2["items"]}
        assert ids1.isdisjoint(ids2)
