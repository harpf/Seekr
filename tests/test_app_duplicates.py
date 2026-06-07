from datetime import UTC, datetime
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not pytest.importorskip("fastapi", reason="fastapi not installed"),
    reason="fastapi required",
)

from fastapi.testclient import TestClient  # noqa: E402

from document_search.app import create_app  # noqa: E402
from document_search.index.sqlite_store import SqliteStore  # noqa: E402


def _login(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _insert_doc(store, path, sha, *, content_hash=None, status="ok"):
    now = datetime.now(tz=UTC).isoformat()
    cur = store.conn.execute(
        "INSERT INTO documents(path, filename, extension, file_size, modified_at, "
        "sha256, content_hash, indexed_at, status) VALUES(?,?,?,?,?,?,?,?,?)",
        (path, Path(path).name, Path(path).suffix, 100, now, sha, content_hash, now, status),
    )
    store.conn.commit()
    return cur.lastrowid


def test_duplicates_requires_admin(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        r = client.get("/api/documents/duplicates")
        assert r.status_code == 401, r.text


def test_duplicates_returns_exact_and_content_groups(tmp_path):
    db_path = tmp_path / "t.db"
    store = SqliteStore(db_path)
    # Admin must exist before the re-open so _backfill_acl grants it public group
    # membership; otherwise the ACL filter hides every doc from the admin.
    store.ensure_default_admin()
    # Exact duplicate group: two docs share the same sha256.
    _insert_doc(store, "/d/a.txt", "sha-exact")
    _insert_doc(store, "/d/b.txt", "sha-exact")
    # Content duplicate group: same content_hash, different sha256.
    _insert_doc(store, "/d/c.txt", "sha-c1", content_hash="ch-shared")
    _insert_doc(store, "/d/d.txt", "sha-c2", content_hash="ch-shared")
    store.conn.close()
    # Re-open to trigger _backfill_acl (grants public read on all docs).
    SqliteStore(db_path).conn.close()

    app = create_app(str(db_path))
    with TestClient(app) as client:
        tok = _login(client)
        r = client.get("/api/documents/duplicates", headers={"X-Auth-Token": tok})
        assert r.status_code == 200, r.text
        body = r.json()

        assert len(body["exact"]) == 1
        exact = body["exact"][0]
        assert exact["hash"] == "sha-exact"
        assert exact["count"] == 2
        assert {d["id"] for d in exact["documents"]} == {1, 2}

        assert len(body["content"]) == 1
        content = body["content"][0]
        assert content["hash"] == "ch-shared"
        assert content["count"] == 2
        assert {d["id"] for d in content["documents"]} == {3, 4}


def test_duplicates_empty_when_none(tmp_path):
    db_path = tmp_path / "t.db"
    store = SqliteStore(db_path)
    store.ensure_default_admin()
    _insert_doc(store, "/d/a.txt", "sha-a")
    _insert_doc(store, "/d/b.txt", "sha-b")
    store.conn.close()
    SqliteStore(db_path).conn.close()

    app = create_app(str(db_path))
    with TestClient(app) as client:
        tok = _login(client)
        r = client.get("/api/documents/duplicates", headers={"X-Auth-Token": tok})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["exact"] == []
        assert body["content"] == []
