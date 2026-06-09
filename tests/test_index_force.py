"""Global reindex (/api/index/reindex-all) and the force flag that re-extracts
already-indexed files even when their hash/mtime is unchanged."""

import json
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from document_search.app import create_app  # noqa: E402


def _login(c):
    tok = c.post("/api/login", json={"username": "admin", "password": "admin"}).json()["token"]
    return {"X-Auth-Token": tok}


def _wait(c, h, jid):
    for _ in range(300):
        s = c.get(f"/api/index/jobs/{jid}", headers=h).json()
        if s["status"] in ("finished", "failed", "interrupted"):
            return s
        time.sleep(0.02)
    raise AssertionError("index job did not finish in time")


def test_force_reindexes_unchanged_file(tmp_path, monkeypatch):
    # Point config at a non-existent file so the worker uses default config
    # (which supports .txt) deterministically, regardless of the repo's config.json.
    monkeypatch.setenv("DOCUMENT_SEARCH_CONFIG_PATH", str(tmp_path / "none.json"))
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_text("hello world", encoding="utf-8")

    with TestClient(create_app(str(tmp_path / "db.sqlite"))) as c:
        h = _login(c)

        jid = c.post("/api/index/start", headers=h, json={"paths": [str(docs)]}).json()["job_id"]
        s1 = _wait(c, h, jid)
        assert s1["status"] == "finished"
        assert s1["indexed"] >= 1

        # Second run without force: the unchanged file is skipped.
        jid = c.post("/api/index/start", headers=h, json={"paths": [str(docs)]}).json()["job_id"]
        s2 = _wait(c, h, jid)
        assert s2["skipped"] >= 1
        assert s2["updated"] == 0

        # With force: the file is re-extracted despite an unchanged hash.
        jid = c.post(
            "/api/index/start", headers=h, json={"paths": [str(docs)], "force": True}
        ).json()["job_id"]
        s3 = _wait(c, h, jid)
        assert s3["updated"] >= 1
        assert s3["skipped"] == 0


def test_reindex_all_reads_source_paths_and_forces(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_text("hello", encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps({"source_paths": [{"path": str(docs), "label": "d", "type": "local"}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCUMENT_SEARCH_CONFIG_PATH", str(cfg))

    with TestClient(create_app(str(tmp_path / "db.sqlite"))) as c:
        h = _login(c)

        jid = c.post("/api/index/reindex-all", headers=h).json()["job_id"]
        s1 = _wait(c, h, jid)
        assert s1["status"] == "finished"
        assert s1["found"] >= 1
        assert s1["indexed"] >= 1

        # Re-running reindex-all forces a re-extract of the unchanged file.
        jid = c.post("/api/index/reindex-all", headers=h).json()["job_id"]
        s2 = _wait(c, h, jid)
        assert s2["updated"] >= 1


def test_reindex_all_requires_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_CONFIG_PATH", str(tmp_path / "config.json"))
    with TestClient(create_app(str(tmp_path / "db.sqlite"))) as c:
        admin = _login(c)
        c.post(
            "/api/users",
            headers=admin,
            json={"username": "bob", "password": "bobpassword1", "role": "user"},
        )
        user = {"X-Auth-Token": c.post(
            "/api/login", json={"username": "bob", "password": "bobpassword1"}
        ).json()["token"]}
        r = c.post("/api/index/reindex-all", headers=user)
        assert r.status_code == 403


def test_parallel_index_indexes_all_files(tmp_path, monkeypatch):
    """With INDEX_WORKERS>1 the job extracts documents concurrently but still
    indexes every file exactly once (DB writes stay serialised)."""
    monkeypatch.setenv("DOCUMENT_SEARCH_INDEX_WORKERS", "4")
    monkeypatch.setenv("DOCUMENT_SEARCH_CONFIG_PATH", str(tmp_path / "none.json"))
    docs = tmp_path / "docs"
    docs.mkdir()
    for i in range(15):
        (docs / f"f{i}.txt").write_text(f"document number {i} content", encoding="utf-8")

    with TestClient(create_app(str(tmp_path / "db.sqlite"))) as c:
        h = _login(c)
        jid = c.post("/api/index/start", headers=h, json={"paths": [str(docs)]}).json()["job_id"]
        s = _wait(c, h, jid)
        assert s["status"] == "finished"
        assert s["found"] == 15
        assert s["indexed"] == 15
        assert s["errors"] == 0

        # Re-run with force under parallelism: every file is re-extracted (updated).
        jid2 = c.post(
            "/api/index/start", headers=h, json={"paths": [str(docs)], "force": True}
        ).json()["job_id"]
        s2 = _wait(c, h, jid2)
        assert s2["updated"] == 15
        assert s2["skipped"] == 0
