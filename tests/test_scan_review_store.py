from datetime import UTC, datetime
from pathlib import Path

from document_search.index.sqlite_store import SqliteStore
from document_search.models import ContentBlock, ExtractionResult, FileFingerprint
from document_search.services.acl_service import visible_document_ids_subquery


def _make_doc(db, tmp_path, name="d.pdf"):
    p = tmp_path / name
    p.write_text("x")
    fp = FileFingerprint(path=p, file_size=1, modified_at=datetime.now(tz=UTC), sha256="h" + name)
    ext = ExtractionResult(file_path=p, status="ok",
                           blocks=[ContentBlock("page", 1, "hello", "T", {})])
    return db.upsert_document(fp, ext)


def _visible_to(db, user_id, doc_id):
    sql, params = visible_document_ids_subquery(user_id)
    rows = db.conn.execute(f"SELECT 1 FROM ({sql}) WHERE document_id = ?",
                           list(params) + [doc_id]).fetchall()
    return bool(rows)


def test_set_scan_acl_revokes_public_and_grants_reviewers(tmp_path):
    db = SqliteStore(Path(tmp_path / "t.db"))
    reviewer = db.create_user("reviewer", "pw12345678", role="user")
    other = db.create_user("other", "pw12345678", role="user")
    doc_id = _make_doc(db, tmp_path)
    assert _visible_to(db, other, doc_id)  # public initially

    db.set_scan_acl(doc_id, group_external_ids=[], user_external_ids=["reviewer"])

    assert _visible_to(db, reviewer, doc_id)
    assert not _visible_to(db, other, doc_id)


def test_restore_public_read_makes_doc_visible_again(tmp_path):
    db = SqliteStore(Path(tmp_path / "t.db"))
    other = db.create_user("other", "pw12345678", role="user")
    doc_id = _make_doc(db, tmp_path)
    db.set_scan_acl(doc_id, group_external_ids=[], user_external_ids=[])
    assert not _visible_to(db, other, doc_id)
    db.restore_public_read(doc_id)
    assert _visible_to(db, other, doc_id)


def test_scan_review_table_and_index_exist(tmp_path):
    db = SqliteStore(Path(tmp_path / "t.db"))
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(scan_review)").fetchall()}
    assert {"id", "inbox_id", "document_id", "staging_path", "status",
            "suggested_folder", "suggested_tags", "ai_reasoning",
            "ai_decision_id", "error_message", "created_at", "updated_at",
            "reviewed_by", "reviewed_at", "original_filename"} <= cols
    idx = {r[1] for r in db.conn.execute("PRAGMA index_list(scan_review)").fetchall()}
    assert "idx_scan_review_inbox_status" in idx


def test_migration_creates_table_on_legacy_db(tmp_path):
    # Simulate a legacy DB without scan_review, then re-open to trigger migration.
    db_path = Path(tmp_path / "legacy.db")
    db = SqliteStore(db_path)
    db.conn.execute("DROP TABLE scan_review")
    db.conn.commit()
    db.conn.close()
    db2 = SqliteStore(db_path)  # re-open runs migrations
    assert db2.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='scan_review'"
    ).fetchone() is not None
    idx = {r[1] for r in db2.conn.execute("PRAGMA index_list(scan_review)").fetchall()}
    assert "idx_scan_review_inbox_status" in idx


def test_user_group_external_ids_returns_group_slugs(tmp_path):
    """user_group_external_ids returns the external_id of every group the user belongs to."""
    db = SqliteStore(Path(tmp_path / "t.db"))
    user_id = db.create_user("alice", "pw12345678", role="user")
    # create_group is the real store API (idempotent, returns principal_id)
    grp_pid = db.create_group("reviewers", "Reviewers")
    db.add_user_to_group(user_id, grp_pid)
    result = db.user_group_external_ids(user_id)
    assert "reviewers" in result
    # The 'public' group is added by create_user/_backfill_acl
    assert "public" in result


# ---------------------------------------------------------------------------
# ScanReviewStore tests (Task C3)
# ---------------------------------------------------------------------------
from document_search.services.scan_review_store import ScanReviewStore  # noqa: E402


def test_create_list_and_status_transitions(tmp_path):
    db = SqliteStore(Path(tmp_path / "t.db"))
    doc_id = _make_doc(db, tmp_path)
    srs = ScanReviewStore(db)

    rid = srs.create_pending(
        inbox_id="b", document_id=doc_id, staging_path="/stage/b/x.pdf",
        original_filename="x.pdf", suggested_folder="Rechnungen",
        suggested_tags=["rechnung"], ai_reasoning="weil", ai_decision_id=None,
    )
    row = srs.get(rid)
    assert row["status"] == "pending"
    assert row["suggested_tags"] == ["rechnung"]

    pending = srs.list_reviews(inbox_ids=["b"], status="pending")
    assert len(pending) == 1

    srs.mark_filed(rid, reviewed_by="admin")
    assert srs.get(rid)["status"] == "filed"
    assert srs.count_pending(["b"]) == 0


def test_create_error_and_retry(tmp_path):
    db = SqliteStore(Path(tmp_path / "t.db"))
    srs = ScanReviewStore(db)
    rid = srs.create_error(inbox_id="b", staging_path="/stage/b/bad.pdf",
                           original_filename="bad.pdf", error_message="OCR failed")
    assert srs.get(rid)["status"] == "error"
    srs.set_pending(rid)
    assert srs.get(rid)["status"] == "pending"


def test_staging_paths_with_rows(tmp_path):
    db = SqliteStore(Path(tmp_path / "t.db"))
    srs = ScanReviewStore(db)
    srs.create_error(inbox_id="b", staging_path="/stage/b/a.pdf",
                     original_filename="a.pdf", error_message="e")
    assert "/stage/b/a.pdf" in srs.staging_paths_with_rows()


def test_list_reviews_empty_inbox_ids_returns_empty(tmp_path):
    db = SqliteStore(Path(tmp_path / "t.db"))
    srs = ScanReviewStore(db)
    srs.create_error(inbox_id="b", staging_path="/s/a.pdf", original_filename="a.pdf", error_message="e")
    assert srs.list_reviews(inbox_ids=[], status=None) == []


def test_mark_rejected_sets_status_and_reviewer(tmp_path):
    db = SqliteStore(Path(tmp_path / "t.db"))
    doc_id = _make_doc(db, tmp_path)
    srs = ScanReviewStore(db)
    rid = srs.create_pending(inbox_id="b", document_id=doc_id, staging_path="/s/x.pdf",
                             original_filename="x.pdf", suggested_folder=None,
                             suggested_tags=None, ai_reasoning=None, ai_decision_id=None)
    srs.mark_rejected(rid, reviewed_by="admin")
    row = srs.get(rid)
    assert row["status"] == "rejected"
    assert row["reviewed_by"] == "admin"
