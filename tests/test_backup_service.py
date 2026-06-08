import sqlite3
from pathlib import Path

import pytest

from document_search.index.sqlite_store import SqliteStore
from document_search.services.backup_service import BackupService


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "document_index.db")


@pytest.fixture
def svc(tmp_path, store):
    backup_dir = tmp_path / "backups"
    return BackupService(store, backup_dir=backup_dir, keep=14)


def _seed_one_document(store: SqliteStore) -> None:
    from datetime import UTC, datetime
    now = datetime.now(tz=UTC).isoformat()
    store.conn.execute(
        "INSERT INTO documents(path, filename, extension, file_size, modified_at, "
        "sha256, indexed_at, status) VALUES(?,?,?,?,?,?,?,?)",
        ("/d/a.pdf", "a.pdf", ".pdf", 10, now, "h1", now, "ok"),
    )
    store.conn.commit()


def test_create_backup_writes_timestamped_file(svc):
    info = svc.create_backup()
    p = Path(info["path"])
    assert p.exists()
    assert p.name.startswith("document_index_")
    assert p.suffix == ".db"
    assert info["size_bytes"] > 0


def test_backup_is_a_valid_openable_sqlite_db(svc, store):
    _seed_one_document(store)
    info = svc.create_backup()
    # The crux: the backup must open as SQLite AND contain the committed data.
    conn = sqlite3.connect(info["path"])
    try:
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        assert ok == "ok"
        count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        assert count == 1
        # Schema check: a representative ACL table came across too
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"documents", "principals", "document_acl"}.issubset(tables)
    finally:
        conn.close()


def test_backup_captures_data_written_through_wal(svc, store):
    # Write a row but DO NOT checkpoint — it lives in the -wal file.
    # Connection.backup() must still see it (a raw cp of the .db would not).
    from datetime import UTC, datetime
    now = datetime.now(tz=UTC).isoformat()
    store.conn.execute(
        "INSERT INTO documents(path, filename, extension, file_size, modified_at, "
        "sha256, indexed_at, status) VALUES(?,?,?,?,?,?,?,?)",
        ("/d/wal.pdf", "wal.pdf", ".pdf", 10, now, "h2", now, "ok"),
    )
    store.conn.commit()  # commit goes to WAL under journal_mode=WAL
    info = svc.create_backup()
    conn = sqlite3.connect(info["path"])
    try:
        row = conn.execute(
            "SELECT 1 FROM documents WHERE path='/d/wal.pdf'"
        ).fetchone()
        assert row is not None
    finally:
        conn.close()

def test_list_backups_returns_newest_first(svc):
    a = svc.create_backup()
    b = svc.create_backup()
    rows = svc.list_backups()
    names = [r["filename"] for r in rows]
    assert a["filename"] in names and b["filename"] in names
    # Sorted by filename descending == newest timestamp first.
    assert names == sorted(names, reverse=True)
    for r in rows:
        assert "filename" in r and "size_bytes" in r and "created_at" in r


def test_list_backups_ignores_non_backup_files(svc):
    svc.backup_dir.mkdir(parents=True, exist_ok=True)
    (svc.backup_dir / "notes.txt").write_text("hi", encoding="utf-8")
    (svc.backup_dir / "random.db").write_text("x", encoding="utf-8")
    svc.create_backup()
    names = [r["filename"] for r in svc.list_backups()]
    assert "notes.txt" not in names
    assert "random.db" not in names  # wrong prefix


def test_prune_keeps_only_newest_n(tmp_path, store):
    svc = BackupService(store, backup_dir=tmp_path / "backups", keep=2)
    import time
    created = []
    for _ in range(4):
        created.append(svc.create_backup()["filename"])
        time.sleep(1.05)  # ensure distinct second-resolution timestamps
    remaining = {r["filename"] for r in svc.list_backups()}
    assert len(remaining) == 2
    # The two newest survive.
    assert set(sorted(created)[-2:]) == remaining
