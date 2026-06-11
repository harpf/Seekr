import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from document_search.app import create_app  # noqa: E402
from document_search.index.sqlite_store import SqliteStore  # noqa: E402
from document_search.models import ContentBlock, ExtractionResult, FileFingerprint  # noqa: E402
from document_search.services.scan_review_store import ScanReviewStore  # noqa: E402


def _client(tmp_path, monkeypatch, scan_inboxes):
    db_path = tmp_path / "document_index.db"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"database_path": str(db_path), "scan_inboxes": scan_inboxes}),
                      encoding="utf-8")
    monkeypatch.setenv("DOCUMENT_SEARCH_CONFIG_PATH", str(config))
    app = create_app(str(db_path))
    client = TestClient(app)
    r = client.post("/api/login", json={"username": "admin", "password": "admin"})
    assert r.status_code == 200, r.text
    return app, client, r.json()["token"], db_path


def _seed_pending(db_path, staging_dir, inbox_id="b", folder="Rechnungen"):
    db = SqliteStore(Path(db_path))
    staging_dir.mkdir(parents=True, exist_ok=True)
    p = staging_dir / "staged.pdf"
    p.write_text("x")
    fp = FileFingerprint(path=p, file_size=1, modified_at=datetime.now(tz=UTC), sha256="h1")
    ext = ExtractionResult(file_path=p, status="ok",
                           blocks=[ContentBlock("ocr_page", 1, "Rechnung", "X", {})])
    doc_id = db.upsert_document(fp, ext)
    srs = ScanReviewStore(db)
    rid = srs.create_pending(inbox_id=inbox_id, document_id=doc_id, staging_path=str(p),
                             original_filename="staged.pdf", suggested_folder=folder,
                             suggested_tags=["rechnung"], ai_reasoning="r", ai_decision_id=None)
    return rid, doc_id, p


def _inbox(tmp_path, inbox_id="b"):
    inbox = tmp_path / "in"
    inbox.mkdir(exist_ok=True)
    target = tmp_path / "Buchhaltung"
    (target / "Rechnungen").mkdir(parents=True, exist_ok=True)
    return {"id": inbox_id, "label": "B", "inbox_path": str(inbox), "target_root": str(target)}


def test_admin_lists_pending_reviews(tmp_path, monkeypatch):
    app, client, token, db_path = _client(tmp_path, monkeypatch, [_inbox(tmp_path)])
    staging = tmp_path / "data" / "scan-staging" / "b" / "pending-review"
    rid, doc_id, _ = _seed_pending(db_path, staging)
    r = client.get("/api/scan/review?inbox=b&status=pending", headers={"X-Auth-Token": token})
    assert r.status_code == 200
    assert any(item["id"] == rid for item in r.json())


def test_folders_route_lists_existing_subfolders(tmp_path, monkeypatch):
    box = _inbox(tmp_path)
    (Path(box["target_root"]) / "Verträge").mkdir(parents=True, exist_ok=True)
    app, client, token, db_path = _client(tmp_path, monkeypatch, [box])
    staging = tmp_path / "data" / "scan-staging" / "b" / "pending-review"
    rid, _, _ = _seed_pending(db_path, staging)
    r = client.get(f"/api/scan/review/{rid}/folders", headers={"X-Auth-Token": token})
    assert r.status_code == 200
    assert set(r.json()) == {"Rechnungen", "Verträge"}


def test_confirm_moves_file_and_marks_filed(tmp_path, monkeypatch):
    box = _inbox(tmp_path)
    app, client, token, db_path = _client(tmp_path, monkeypatch, [box])
    staging = tmp_path / "data" / "scan-staging" / "b" / "pending-review"
    rid, doc_id, staged = _seed_pending(db_path, staging)
    r = client.post(f"/api/scan/review/{rid}/confirm", headers={"X-Auth-Token": token},
                    json={"folder": "Rechnungen", "tags": ["rechnung"]})
    assert r.status_code == 200, r.text
    assert (Path(box["target_root"]) / "Rechnungen" / "staged.pdf").exists()
    assert not staged.exists()
    db = SqliteStore(Path(db_path))
    assert ScanReviewStore(db).get(rid)["status"] == "filed"


def test_confirm_rejects_path_traversal(tmp_path, monkeypatch):
    box = _inbox(tmp_path)
    app, client, token, db_path = _client(tmp_path, monkeypatch, [box])
    staging = tmp_path / "data" / "scan-staging" / "b" / "pending-review"
    rid, _, _ = _seed_pending(db_path, staging)
    r = client.post(f"/api/scan/review/{rid}/confirm", headers={"X-Auth-Token": token},
                    json={"folder": "../../etc", "tags": []})
    assert r.status_code == 400


def test_confirm_rejects_nonexistent_folder_without_new_flag(tmp_path, monkeypatch):
    box = _inbox(tmp_path)
    app, client, token, db_path = _client(tmp_path, monkeypatch, [box])
    staging = tmp_path / "data" / "scan-staging" / "b" / "pending-review"
    rid, _, _ = _seed_pending(db_path, staging)
    r = client.post(f"/api/scan/review/{rid}/confirm", headers={"X-Auth-Token": token},
                    json={"folder": "BrandNew", "tags": []})
    assert r.status_code == 400


def test_confirm_creates_new_folder_with_flag(tmp_path, monkeypatch):
    box = _inbox(tmp_path)
    app, client, token, db_path = _client(tmp_path, monkeypatch, [box])
    staging = tmp_path / "data" / "scan-staging" / "b" / "pending-review"
    rid, _, _ = _seed_pending(db_path, staging)
    r = client.post(f"/api/scan/review/{rid}/confirm", headers={"X-Auth-Token": token},
                    json={"folder": "BrandNew", "tags": [], "new_folder": True})
    assert r.status_code == 200, r.text
    assert (Path(box["target_root"]) / "BrandNew" / "staged.pdf").exists()


def test_reject_removes_from_index(tmp_path, monkeypatch):
    box = _inbox(tmp_path)
    app, client, token, db_path = _client(tmp_path, monkeypatch, [box])
    staging = tmp_path / "data" / "scan-staging" / "b" / "pending-review"
    rid, doc_id, staged = _seed_pending(db_path, staging)
    r = client.post(f"/api/scan/review/{rid}/reject", headers={"X-Auth-Token": token})
    assert r.status_code == 200
    db = SqliteStore(Path(db_path))
    assert db.conn.execute("SELECT 1 FROM documents WHERE id=?", (doc_id,)).fetchone() is None
    assert ScanReviewStore(db).get(rid)["status"] == "rejected"


def test_non_reviewer_cannot_access_inbox(tmp_path, monkeypatch):
    # inbox with a specific reviewer user; a different non-admin user is forbidden.
    box = _inbox(tmp_path)
    box["reviewers"] = {"groups": [], "users": ["alice"]}
    app, client, token, db_path = _client(tmp_path, monkeypatch, [box])
    db = SqliteStore(Path(db_path))
    db.create_user("bob", "pw12345678", role="user")
    rb = client.post("/api/login", json={"username": "bob", "password": "pw12345678"})
    bob_token = rb.json()["token"]
    staging = tmp_path / "data" / "scan-staging" / "b" / "pending-review"
    rid, _, _ = _seed_pending(db_path, staging)
    # bob is not a reviewer of inbox b -> the list excludes b, and direct access 403.
    lst = client.get("/api/scan/review?inbox=b&status=pending", headers={"X-Auth-Token": bob_token})
    assert lst.status_code == 200
    assert lst.json() == []
    conf = client.post(f"/api/scan/review/{rid}/confirm", headers={"X-Auth-Token": bob_token},
                       json={"folder": "Rechnungen", "tags": []})
    assert conf.status_code == 403
    folders = client.get(f"/api/scan/review/{rid}/folders", headers={"X-Auth-Token": bob_token})
    assert folders.status_code == 403
    rej = client.post(f"/api/scan/review/{rid}/reject", headers={"X-Auth-Token": bob_token})
    assert rej.status_code == 403
    retry = client.post(f"/api/scan/review/{rid}/retry", headers={"X-Auth-Token": bob_token})
    assert retry.status_code == 403
