from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from document_search.app import create_app
from document_search.index.sqlite_store import SqliteStore


def _seed(db_path: str, n: int) -> None:
    store = SqliteStore(Path(db_path))
    now = datetime.now(tz=UTC).isoformat()
    for i in range(n):
        path = f"/d/doc{i:03d}.txt"
        text = f"alpha document number {i}"
        cursor = store.conn.execute(
            "INSERT INTO documents(path, filename, extension, file_size, modified_at, sha256, indexed_at, status) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (path, Path(path).name, ".txt", len(text), now, f"h{i}", now, "ok"),
        )
        doc_id = cursor.lastrowid
        bcursor = store.conn.execute(
            "INSERT INTO content_blocks(document_id, block_type, block_number, text, extractor, text_length) "
            "VALUES(?,?,?,?,?,?)",
            (doc_id, "paragraph", 1, text, "txt", len(text)),
        )
        store.conn.execute(
            "INSERT INTO content_fts(document_id, block_id, path, filename, extension, block_type, block_number, text) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (doc_id, bcursor.lastrowid, path, Path(path).name, ".txt", "paragraph", "1", text),
        )
    store.conn.commit()
    # Re-open to backfill public ACL so the admin user can see all docs.
    SqliteStore(Path(db_path))


@pytest.fixture
def client_and_db(tmp_path):
    db_path = str(tmp_path / "index.db")
    app = create_app(db_path)
    return TestClient(app), db_path


def auth_headers(client):
    r = client.post("/api/login", json={"username": "admin", "password": "admin"})
    return {"X-Auth-Token": r.json()["token"]}


def test_search_returns_list_and_pagination_headers(client_and_db):
    client, db_path = client_and_db
    _seed(db_path, 30)
    resp = client.post(
        "/api/search",
        json={"query": "alpha", "limit": 10, "offset": 0},
        headers=auth_headers(client),
    )
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
    assert len(resp.json()) == 10
    assert resp.headers["X-Total-Count"] == "30"
    assert resp.headers["X-Has-More"] == "true"
    assert resp.headers["X-Next-Offset"] == "10"


def test_search_second_page_disjoint(client_and_db):
    client, db_path = client_and_db
    _seed(db_path, 30)
    headers = auth_headers(client)
    p1 = client.post("/api/search", json={"query": "alpha", "limit": 10, "offset": 0}, headers=headers)
    p2 = client.post("/api/search", json={"query": "alpha", "limit": 10, "offset": 10}, headers=headers)
    ids1 = {d["document_id"] for d in p1.json()}
    ids2 = {d["document_id"] for d in p2.json()}
    assert ids1.isdisjoint(ids2)


def test_search_last_page_has_more_false(client_and_db):
    client, db_path = client_and_db
    _seed(db_path, 25)
    resp = client.post(
        "/api/search",
        json={"query": "alpha", "limit": 10, "offset": 20},
        headers=auth_headers(client),
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 5
    assert resp.headers["X-Has-More"] == "false"
    assert resp.headers["X-Next-Offset"] == "20"


def test_search_unbalanced_quote_returns_400(client_and_db):
    client, db_path = client_and_db
    _seed(db_path, 5)
    resp = client.post(
        "/api/search",
        json={"query": 'alpha"', "limit": 10, "offset": 0},
        headers=auth_headers(client),
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "query" in detail.lower()
