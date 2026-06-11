import document_search.observability as obs


def test_scan_metrics_registered():
    assert obs.SCAN_INGESTED_TOTAL is not None
    assert obs.SCAN_REVIEW_PENDING is not None
    obs.SCAN_INGESTED_TOTAL.labels(inbox="b", outcome="pending").inc()


import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from document_search.app import create_app  # noqa: E402
from document_search.index.sqlite_store import SqliteStore  # noqa: E402
from document_search.models import ContentBlock, ExtractionResult  # noqa: E402
from document_search.services.acl_service import visible_document_ids_subquery  # noqa: E402
from document_search.services.ai_organizer import OrganizationSuggestion  # noqa: E402
from document_search.services.scan_review_store import ScanReviewStore  # noqa: E402


def _run_handler(app, kind, payload):
    return app.state.worker._handlers[kind](payload, lambda p: None)


def _stage_file(data_dir, inbox_id, name="scan001.pdf"):
    staging = Path(data_dir) / "scan-staging" / inbox_id / "pending-review"
    staging.mkdir(parents=True)
    f = staging / name
    f.write_bytes(b"%PDF fake")
    return f


def _setup(tmp_path, monkeypatch, *, reviewers):
    data = tmp_path / "data"
    data.mkdir()
    db_path = data / "document_index.db"
    target = tmp_path / "Buchhaltung"
    (target / "Rechnungen").mkdir(parents=True)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "database_path": str(db_path),
        "scan_inboxes": [{
            "id": "b", "label": "B", "inbox_path": str(tmp_path / "in"),
            "target_root": str(target), "reviewers": reviewers,
        }],
    }), encoding="utf-8")
    (tmp_path / "in").mkdir()
    monkeypatch.setenv("DOCUMENT_SEARCH_CONFIG_PATH", str(config))
    app = create_app(str(db_path))
    # Deterministic extraction (no real OCR).
    monkeypatch.setattr(
        "document_search.services.scan_extractor.extract_for_scan",
        lambda p, languages: ExtractionResult(
            file_path=p, status="ok",
            blocks=[ContentBlock("ocr_page", 1, "Rechnung Nr 42", "X", {})]),
    )
    return app, db_path, data


def _visible(db, user_id, doc_id):
    sql, params = visible_document_ids_subquery(user_id)
    return bool(db.conn.execute(f"SELECT 1 FROM ({sql}) WHERE document_id=?",
                                list(params) + [doc_id]).fetchall())


def test_scan_ingest_keeps_existing_folder_suggestion_and_sets_reviewer_acl(tmp_path, monkeypatch):
    app, db_path, data = _setup(tmp_path, monkeypatch, reviewers={"groups": [], "users": ["reviewer"]})
    db = SqliteStore(db_path)
    reviewer = db.create_user("reviewer", "pw12345678", role="user")
    other = db.create_user("other", "pw12345678", role="user")
    staged = _stage_file(data, "b")
    # AI proposes a folder that EXISTS -> kept.
    app.state.organizer.suggest = lambda **kw: OrganizationSuggestion(
        suggested_subpath="Rechnungen", suggested_tags=["rechnung"], reason="weil", model="test")

    _run_handler(app, "scan_ingest",
                 {"inbox_id": "b", "staging_path": str(staged), "original_filename": "scan001.pdf"})

    reviews = ScanReviewStore(db).list_reviews(inbox_ids=["b"], status="pending")
    assert len(reviews) == 1
    r = reviews[0]
    assert r["suggested_folder"] == "Rechnungen"
    assert r["document_id"] is not None
    # Reviewer-only ACL: reviewer can see it, other cannot.
    assert _visible(db, reviewer, r["document_id"])
    assert not _visible(db, other, r["document_id"])


def test_scan_ingest_drops_hallucinated_folder(tmp_path, monkeypatch):
    app, db_path, data = _setup(tmp_path, monkeypatch, reviewers={"groups": [], "users": []})
    staged = _stage_file(data, "b")
    app.state.organizer.suggest = lambda **kw: OrganizationSuggestion(
        suggested_subpath="DoesNotExist", suggested_tags=[], reason="x", model="test")
    _run_handler(app, "scan_ingest",
                 {"inbox_id": "b", "staging_path": str(staged), "original_filename": "scan001.pdf"})
    db = SqliteStore(db_path)
    r = ScanReviewStore(db).list_reviews(inbox_ids=["b"], status="pending")[0]
    assert r["suggested_folder"] is None


def test_scan_ingest_unknown_inbox_records_error(tmp_path, monkeypatch):
    app, db_path, data = _setup(tmp_path, monkeypatch, reviewers={"groups": [], "users": []})
    staged = _stage_file(data, "nope")
    _run_handler(app, "scan_ingest",
                 {"inbox_id": "nope", "staging_path": str(staged), "original_filename": "scan001.pdf"})
    db = SqliteStore(db_path)
    errs = ScanReviewStore(db).list_reviews(inbox_ids=["nope"], status="error")
    assert len(errs) == 1


def test_scan_ingest_extraction_error_records_error(tmp_path, monkeypatch):
    app, db_path, data = _setup(tmp_path, monkeypatch, reviewers={"groups": [], "users": []})
    staged = _stage_file(data, "b")
    monkeypatch.setattr(
        "document_search.services.scan_extractor.extract_for_scan",
        lambda p, languages: ExtractionResult(file_path=p, status="error", error_message="OCR boom"))
    _run_handler(app, "scan_ingest",
                 {"inbox_id": "b", "staging_path": str(staged), "original_filename": "scan001.pdf"})
    db = SqliteStore(db_path)
    errs = ScanReviewStore(db).list_reviews(inbox_ids=["b"], status="error")
    assert len(errs) == 1 and "OCR boom" in (errs[0]["error_message"] or "")
