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


def test_enabled_semantic_search_end_to_end(tmp_path, monkeypatch):
    import json
    from pathlib import Path

    from document_search.services.embedding_service import EmbeddingService

    # Deterministic stub embedder: 'tomato' -> [0,1,0], anything else (incl. the
    # query and 'cabbage') -> [1,0,0]. So the query aligns with cabbage's vector.
    def fake_embed(self, text):
        t = (text or "").lower()
        if "tomato" in t:
            return [0.0, 1.0, 0.0]
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr(EmbeddingService, "embed", fake_embed)

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"semantic_search_enabled": True, "embed_model": "stub"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCUMENT_SEARCH_CONFIG_PATH", str(config_path))

    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        from datetime import UTC, datetime

        from document_search.index.sqlite_store import SqliteStore
        s = SqliteStore(tmp_path / "t.db")
        now = datetime.now(tz=UTC).isoformat()
        for path, body in [("/d/cabbage.txt", "cabbage soup"), ("/d/tomato.txt", "tomato soup")]:
            cur = s.conn.execute(
                "INSERT INTO documents(path, filename, extension, file_size, modified_at, sha256, indexed_at, status) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (path, Path(path).name, ".txt", len(body), now, path, now, "ok"),
            )
            doc_id = cur.lastrowid
            bcur = s.conn.execute(
                "INSERT INTO content_blocks(document_id, block_type, block_number, text, extractor, text_length) "
                "VALUES(?,?,?,?,?,?)", (doc_id, "paragraph", 1, body, "txt", len(body)),
            )
            s.conn.execute(
                "INSERT INTO content_fts(document_id, block_id, path, filename, extension, "
                "block_type, block_number, text) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (doc_id, bcur.lastrowid, path, Path(path).name, ".txt", "paragraph", "1", body),
            )
        s.conn.commit()
        SqliteStore(tmp_path / "t.db")  # re-open -> backfill public ACL

        # Backfill embeddings via the queue
        jr = client.post("/api/index/embeddings/start", headers={"X-Auth-Token": token}).json()
        import time
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            st = client.get(f"/api/index/jobs/{jr['job_id']}", headers={"X-Auth-Token": token}).json()
            if st["status"] in ("finished", "failed"):
                break
            time.sleep(0.05)
        n = SqliteStore(tmp_path / "t.db").conn.execute("SELECT COUNT(*) FROM block_embeddings").fetchone()[0]
        assert n == 2

        # Semantic search: query -> cabbage-aligned vector -> cabbage first
        r = client.post("/api/search", headers={"X-Auth-Token": token},
                        json={"query": "cabbage", "mode": "semantic"})
        assert r.status_code == 200, r.text
        results = r.json()
        assert results and results[0]["path"] == "/d/cabbage.txt"
