from datetime import UTC, datetime
from pathlib import Path

from document_search.index.sqlite_store import SqliteStore
from document_search.models import ContentBlock, ExtractionResult, FileFingerprint
from document_search.services.acl_service import visible_document_ids_subquery


def _make_scan_doc(db, tmp_path):
    p = tmp_path / "scan.pdf"
    p.write_text("x")
    fp = FileFingerprint(path=p, file_size=1, modified_at=datetime.now(tz=UTC), sha256="hh")
    ext = ExtractionResult(file_path=p, status="ok",
                           blocks=[ContentBlock("ocr_page", 1, "secret", "X", {})])
    return db.upsert_document(fp, ext)


def _visible(db, user_id, doc_id):
    sql, params = visible_document_ids_subquery(user_id)
    return bool(db.conn.execute(f"SELECT 1 FROM ({sql}) WHERE document_id=?",
                                list(params) + [doc_id]).fetchall())


def test_scan_doc_stays_reviewer_only_across_store_reopen(tmp_path):
    """The backfill runs on every store construction and re-publicises docs with
    no ACL rows. A scanned doc must keep explicit reviewer grants and stay hidden
    from non-reviewers even after the DB is reopened (backfill re-run)."""
    db_path = Path(tmp_path / "document_index.db")
    db = SqliteStore(db_path)
    reviewer = db.create_user("reviewer", "pw12345678", role="user")
    other = db.create_user("other", "pw12345678", role="user")
    doc_id = _make_scan_doc(db, tmp_path)
    db.set_scan_acl(doc_id, group_external_ids=[], user_external_ids=["reviewer"])
    assert _visible(db, reviewer, doc_id)
    assert not _visible(db, other, doc_id)
    db.conn.close()

    db2 = SqliteStore(db_path)  # re-open -> _backfill_acl runs again
    assert _visible(db2, reviewer, doc_id)
    assert not _visible(db2, other, doc_id), "backfill must not re-publicise a scan doc"


def test_scan_doc_with_no_reviewers_stays_private_across_reopen(tmp_path):
    """Empty reviewers -> sentinel grant keeps the doc non-empty AND non-public, so
    the backfill does not re-publicise it on reopen."""
    db_path = Path(tmp_path / "document_index.db")
    db = SqliteStore(db_path)
    other = db.create_user("other", "pw12345678", role="user")
    doc_id = _make_scan_doc(db, tmp_path)
    db.set_scan_acl(doc_id, group_external_ids=[], user_external_ids=[])
    assert not _visible(db, other, doc_id)
    db.conn.close()
    db2 = SqliteStore(db_path)
    assert not _visible(db2, other, doc_id), "empty-reviewer scan doc must stay private after reopen"
