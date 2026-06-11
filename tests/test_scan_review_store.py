from pathlib import Path

from document_search.index.sqlite_store import SqliteStore


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
