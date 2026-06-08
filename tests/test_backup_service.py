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

def test_restore_takes_pre_restore_safety_backup(svc, store):
    _seed_one_document(store)
    snapshot = svc.create_backup()  # backup containing 1 doc

    # Mutate the live DB so we can prove restore reverts it.
    store.conn.execute("DELETE FROM documents")
    store.conn.commit()

    before = {r["filename"] for r in svc.list_backups()}
    result = svc.restore_backup(snapshot["filename"])
    after = {r["filename"] for r in svc.list_backups()}

    # A new "pre-restore" safety backup must have been created.
    new_files = after - before
    assert len(new_files) >= 1, "restore must take a pre-restore safety backup"
    assert result["restart_required"] is True
    assert result["restored_from"] == snapshot["filename"]


def test_restore_swaps_db_file_contents(tmp_path):
    # Use a fresh store we fully control so we can re-open after the swap.
    db = tmp_path / "document_index.db"
    s = SqliteStore(db)
    _seed_one_document(s)
    svc = BackupService(s, backup_dir=tmp_path / "backups", keep=14)
    snap = svc.create_backup()

    # Delete everything, then restore.
    s.conn.execute("DELETE FROM documents")
    s.conn.commit()
    s.conn.close()  # simulate the restart: close the live connection first

    svc.restore_backup(snap["filename"])

    # Re-open: the restored file must contain the original row again.
    s2 = SqliteStore(db)
    count = s2.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    assert count == 1


def test_restore_unknown_backup_raises(svc):
    with pytest.raises(FileNotFoundError):
        svc.restore_backup("document_index_19990101_000000.db")


def test_restore_rejects_path_traversal(svc):
    with pytest.raises(ValueError):
        svc.restore_backup("../../etc/passwd")

def _seed_doc_with_tag_and_acl(store):
    """Create one user (alice), one doc, a tag, and a non-public ACL grant."""
    from datetime import UTC, datetime
    now = datetime.now(tz=UTC).isoformat()
    alice_id = store.create_user("alice", "pw-alice")
    cur = store.conn.execute(
        "INSERT INTO documents(path, filename, extension, file_size, modified_at, "
        "sha256, indexed_at, status) VALUES(?,?,?,?,?,?,?,?)",
        ("/d/report.pdf", "report.pdf", ".pdf", 20, now, "h-report", now, "ok"),
    )
    doc_id = cur.lastrowid
    store.conn.execute(
        "INSERT INTO content_blocks(document_id, block_type, block_number, text, "
        "extractor, text_length) VALUES(?,?,?,?,?,?)",
        (doc_id, "paragraph", 1, "quarterly numbers", "txt", 17),
    )
    tcur = store.conn.execute(
        "INSERT INTO user_tags(user_id, name) VALUES(?, 'finance')", (alice_id,)
    )
    store.conn.execute(
        "INSERT INTO document_tags(user_id, document_id, tag_id, created_at) "
        "VALUES(?,?,?,?)",
        (alice_id, doc_id, tcur.lastrowid, now),
    )
    alice_p = store.conn.execute(
        "SELECT principal_id FROM users WHERE id=?", (alice_id,)
    ).fetchone()["principal_id"]
    store.conn.execute(
        "INSERT INTO document_acl(document_id, principal_id, permission, granted_at) "
        "VALUES(?,?, 'read', ?)",
        (doc_id, alice_p, now),
    )
    store.conn.commit()
    return alice_id, doc_id


def test_export_creates_zip_with_manifest_and_tables(svc, store, tmp_path):
    _seed_doc_with_tag_and_acl(store)
    out = tmp_path / "export.zip"
    info = svc.export_archive(out)
    assert Path(info["path"]).exists()
    import json
    import zipfile
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        assert "manifest.json" in names
        for table in ("documents", "content_blocks", "user_tags",
                      "document_tags", "principals", "user_groups", "document_acl"):
            assert f"tables/{table}.json" in names
        manifest = json.loads(z.read("manifest.json"))
        assert manifest["format"] == "seekr-export"
        assert manifest["counts"]["documents"] == 1
        docs = json.loads(z.read("tables/documents.json"))
        assert docs[0]["path"] == "/d/report.pdf"


def test_import_round_trips_documents_tags_and_acls(tmp_path, store):
    src_alice, src_doc = _seed_doc_with_tag_and_acl(store)
    svc = BackupService(store, backup_dir=tmp_path / "b", keep=14)
    archive = tmp_path / "export.zip"
    svc.export_archive(archive)

    # Fresh, empty target DB.
    target = SqliteStore(tmp_path / "target.db")
    target.create_user("alice", "pw-alice")  # same principal external_id
    target_svc = BackupService(target, backup_dir=tmp_path / "tb", keep=14)
    result = target_svc.import_archive(archive)

    # Document came across.
    doc = target.conn.execute(
        "SELECT id FROM documents WHERE path='/d/report.pdf'"
    ).fetchone()
    assert doc is not None
    # Tag + tag link came across.
    tag = target.conn.execute(
        "SELECT id FROM user_tags WHERE name='finance'"
    ).fetchone()
    assert tag is not None
    link = target.conn.execute(
        "SELECT 1 FROM document_tags WHERE document_id=? AND tag_id=?",
        (doc["id"], tag["id"]),
    ).fetchone()
    assert link is not None
    # ACL grant came across, mapped to alice's principal in the TARGET db.
    alice_p = target.conn.execute(
        "SELECT principal_id FROM users WHERE username='alice'"
    ).fetchone()["principal_id"]
    acl = target.conn.execute(
        "SELECT permission FROM document_acl WHERE document_id=? AND principal_id=?",
        (doc["id"], alice_p),
    ).fetchone()
    assert acl is not None and acl["permission"] == "read"
    assert result["imported"]["documents"] == 1


def test_import_is_idempotent_on_path(tmp_path, store):
    _seed_doc_with_tag_and_acl(store)
    svc = BackupService(store, backup_dir=tmp_path / "b", keep=14)
    archive = tmp_path / "export.zip"
    svc.export_archive(archive)

    target = SqliteStore(tmp_path / "target.db")
    target.create_user("alice", "pw-alice")
    tsvc = BackupService(target, backup_dir=tmp_path / "tb", keep=14)
    tsvc.import_archive(archive)
    tsvc.import_archive(archive)  # second import must not duplicate

    count = target.conn.execute(
        "SELECT COUNT(*) FROM documents WHERE path='/d/report.pdf'"
    ).fetchone()[0]
    assert count == 1


def test_import_owner_only_doc_is_not_publicized(tmp_path, store):
    """An owner-based doc (owner set, no document_acl row) must NOT become
    public after import: _backfill_acl publicizes any ACL-less doc, so import
    grants the owner an explicit read to keep it private."""
    from datetime import UTC, datetime

    now = datetime.now(tz=UTC).isoformat()
    alice = store.create_user("alice", "pw-alice")
    alice_p = store.conn.execute(
        "SELECT principal_id FROM users WHERE id=?", (alice,)
    ).fetchone()["principal_id"]
    cur = store.conn.execute(
        "INSERT INTO documents(path, filename, extension, file_size, modified_at, "
        "sha256, indexed_at, status, owner_principal_id) VALUES(?,?,?,?,?,?,?,?,?)",
        ("/d/secret.pdf", "secret.pdf", ".pdf", 9, now, "h-sec", now, "ok", alice_p),
    )
    store.conn.execute("DELETE FROM document_acl WHERE document_id=?", (cur.lastrowid,))
    store.conn.commit()

    svc = BackupService(store, backup_dir=tmp_path / "b", keep=14)
    archive = tmp_path / "export.zip"
    svc.export_archive(archive)

    target_path = tmp_path / "target.db"
    target = SqliteStore(target_path)
    target.create_user("alice", "pw-alice")
    BackupService(target, backup_dir=tmp_path / "tb", keep=14).import_archive(archive)

    doc_id = target.conn.execute(
        "SELECT id FROM documents WHERE path='/d/secret.pdf'"
    ).fetchone()["id"]
    # Not ACL-less -> _backfill_acl can't publicize it.
    acl_count = target.conn.execute(
        "SELECT COUNT(*) FROM document_acl WHERE document_id=?", (doc_id,)
    ).fetchone()[0]
    assert acl_count >= 1

    def _public_can_read(s):
        pub = s.conn.execute(
            "SELECT id FROM principals WHERE type='group' AND external_id='public'"
        ).fetchone()
        if not pub:
            return False
        return s.conn.execute(
            "SELECT 1 FROM document_acl WHERE document_id=? AND principal_id=? "
            "AND permission='read'",
            (doc_id, pub["id"]),
        ).fetchone() is not None

    assert not _public_can_read(target)
    # Reopen -> _backfill_acl runs; the owner-based doc must STILL not be public.
    assert not _public_can_read(SqliteStore(target_path))
