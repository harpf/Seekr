"""count_documents supports an optional `cap`: it stops counting distinct
documents at `cap+1`, so a broad search's total stays cheap (shown as "cap+")
instead of enumerating every match. cap=None keeps the exact behaviour."""

from pathlib import Path

from document_search.index.search_service import count_documents
from document_search.index.sqlite_store import SqliteStore

_NOW = "2026-06-10T00:00:00"


def _store_with_docs(tmp_path: Path, ndocs: int, term: str = "invoice"):
    store = SqliteStore(tmp_path / "c.db")
    uid = store.create_user("admin", "adminpassword")
    c = store.conn
    pub = c.execute(
        "SELECT id FROM principals WHERE type='group' AND external_id='public'"
    ).fetchone()["id"]
    for i in range(1, ndocs + 1):
        c.execute(
            "INSERT INTO documents(id,path,filename,extension,mime_type,file_size,"
            "modified_at,created_at,sha256,indexed_at,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (i, f"/d/f{i}.pdf", f"f{i}.pdf", ".pdf", "application/pdf", 1, _NOW, _NOW, f"h{i}", _NOW, "indexed"),
        )
        c.execute(
            "INSERT INTO document_acl(document_id,principal_id,permission,granted_at) VALUES(?,?,?,?)",
            (i, pub, "read", _NOW),
        )
        c.execute(
            "INSERT INTO content_blocks(document_id,block_type,block_number,text,extractor,text_length,metadata_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (i, "page", 0, f"{term} body {i}", "Pdf", 12, None),
        )
        bid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute(
            "INSERT INTO content_fts(document_id,block_id,path,filename,extension,block_type,block_number,text) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (i, bid, f"/d/f{i}.pdf", f"f{i}.pdf", ".pdf", "page", 0, f"{term} body {i}"),
        )
    c.commit()
    return store, uid


def test_fts_count_exact_without_cap(tmp_path):
    store, uid = _store_with_docs(tmp_path, 5)
    assert count_documents(store, "invoice", user_id=uid) == 5


def test_fts_count_capped(tmp_path):
    store, uid = _store_with_docs(tmp_path, 5)
    # 5 docs match; cap at 2 → stop at cap+1 so the caller can render "2+".
    assert count_documents(store, "invoice", user_id=uid, cap=2) == 3
    # cap above the real total → exact.
    assert count_documents(store, "invoice", user_id=uid, cap=10) == 5


def test_browse_count_capped(tmp_path):
    store, uid = _store_with_docs(tmp_path, 5)
    assert count_documents(store, "", user_id=uid) == 5
    assert count_documents(store, "", user_id=uid, cap=2) == 3
    assert count_documents(store, "", user_id=uid, cap=10) == 5


def test_search_total_count_capped_header(tmp_path, monkeypatch):
    """End-to-end: index >cap matching files through the real job path (so the
    data is visible to the request store), then assert the search caps the total
    and flags it approximate via X-Total-Approx."""
    import time

    import pytest

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import document_search.app as app_module

    monkeypatch.setattr(app_module, "SEARCH_TOTAL_CAP", 2)
    # Non-existent config → worker uses default config (supports .txt).
    monkeypatch.setenv("DOCUMENT_SEARCH_CONFIG_PATH", str(tmp_path / "none.json"))
    docs = tmp_path / "docs"
    docs.mkdir()
    unique = ["alpha", "bravo", "charlie", "delta"]
    for i in range(4):  # 4 files all contain "invoice"; cap is 2
        (docs / f"f{i}.txt").write_text(f"invoice document {unique[i]}", encoding="utf-8")

    with TestClient(app_module.create_app(str(tmp_path / "app.db"))) as client:
        h = {"X-Auth-Token": client.post(
            "/api/login", json={"username": "admin", "password": "admin"}
        ).json()["token"]}

        jid = client.post("/api/index/start", headers=h, json={"paths": [str(docs)]}).json()["job_id"]
        for _ in range(300):
            s = client.get(f"/api/index/jobs/{jid}", headers=h).json()
            if s["status"] in ("finished", "failed", "interrupted"):
                break
            time.sleep(0.02)
        assert s["status"] == "finished" and s["indexed"] >= 4, s

        r = client.post("/api/search", headers=h, json={"query": "invoice"})
        assert r.status_code == 200, r.text
        assert r.headers.get("X-Total-Approx") == "true"
        assert r.headers.get("X-Total-Count") == "2"

        # A selective query under the cap stays exact (one matching document).
        r2 = client.post("/api/search", headers=h, json={"query": "alpha"})
        assert r2.headers.get("X-Total-Approx") == "false"
        assert r2.headers.get("X-Total-Count") == "1"
