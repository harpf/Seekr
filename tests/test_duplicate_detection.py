from pathlib import Path

import pytest

from document_search.services.file_service import normalized_content_hash


def test_identical_text_same_hash():
    a = normalized_content_hash("Hello World")
    b = normalized_content_hash("Hello World")
    assert a == b


def test_whitespace_and_case_insensitive():
    a = normalized_content_hash("Hello   World\n\tFoo")
    b = normalized_content_hash("  hello world foo  ")
    assert a == b


def test_different_text_different_hash():
    a = normalized_content_hash("the quick brown fox")
    b = normalized_content_hash("a slow green turtle")
    assert a != b


def test_empty_text_returns_none():
    assert normalized_content_hash("") is None
    assert normalized_content_hash("   \n\t  ") is None


def test_only_first_8kb_considered():
    base = "x" * 8192
    a = normalized_content_hash(base + "AAAA")
    b = normalized_content_hash(base + "BBBB")
    # Tails beyond 8 KB are truncated, so the hashes collide.
    assert a == b


def test_returns_hex_string():
    h = normalized_content_hash("some content here")
    assert isinstance(h, str)
    assert len(h) == 64  # sha256 hex digest
    int(h, 16)  # valid hex


from document_search.index.sqlite_store import SqliteStore


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "test.db")


def _insert_doc(store, path, sha, *, content_hash=None, status="ok"):
    from datetime import UTC, datetime
    now = datetime.now(tz=UTC).isoformat()
    cur = store.conn.execute(
        "INSERT INTO documents(path, filename, extension, file_size, modified_at, "
        "sha256, content_hash, indexed_at, status) VALUES(?,?,?,?,?,?,?,?,?)",
        (path, Path(path).name, Path(path).suffix, 100, now, sha, content_hash, now, status),
    )
    store.conn.commit()
    return cur.lastrowid


def _insert_block(store, doc_id, text, block_number=1):
    cur = store.conn.execute(
        "INSERT INTO content_blocks(document_id, block_type, block_number, text, "
        "extractor, text_length) VALUES(?,?,?,?,?,?)",
        (doc_id, "paragraph", block_number, text, "txt", len(text)),
    )
    store.conn.commit()
    return cur.lastrowid


def test_documents_has_content_hash_column(store):
    cols = {c[1] for c in store.conn.execute("PRAGMA table_info(documents)").fetchall()}
    assert "content_hash" in cols


def test_backfill_fills_content_hash_from_blocks(store):
    doc_id = _insert_doc(store, "/d/a.txt", "sha-a", content_hash=None)
    _insert_block(store, doc_id, "The Quick   Brown Fox")
    # Re-open to trigger the idempotent backfill at startup.
    store2 = SqliteStore(store.db_path)
    row = store2.conn.execute(
        "SELECT content_hash FROM documents WHERE id=?", (doc_id,)
    ).fetchone()
    from document_search.services.file_service import normalized_content_hash
    assert row["content_hash"] == normalized_content_hash("The Quick   Brown Fox")


def test_backfill_leaves_null_for_documents_without_text(store):
    doc_id = _insert_doc(store, "/d/empty.pdf", "sha-empty", content_hash=None)
    # No content_blocks for this document.
    store2 = SqliteStore(store.db_path)
    row = store2.conn.execute(
        "SELECT content_hash FROM documents WHERE id=?", (doc_id,)
    ).fetchone()
    assert row["content_hash"] is None


def test_backfill_is_idempotent_and_does_not_overwrite(store):
    doc_id = _insert_doc(store, "/d/a.txt", "sha-a", content_hash="preexisting")
    _insert_block(store, doc_id, "ignored because hash already set")
    store2 = SqliteStore(store.db_path)
    row = store2.conn.execute(
        "SELECT content_hash FROM documents WHERE id=?", (doc_id,)
    ).fetchone()
    assert row["content_hash"] == "preexisting"


def test_upsert_document_sets_content_hash(tmp_path):
    from datetime import UTC, datetime

    from document_search.models import (
        ContentBlock,
        ExtractionResult,
        FileFingerprint,
    )
    from document_search.services.file_service import normalized_content_hash

    store = SqliteStore(tmp_path / "test.db")
    p = tmp_path / "report.txt"
    p.write_text("hello body text", encoding="utf-8")
    fp = FileFingerprint(
        path=p,
        file_size=15,
        modified_at=datetime.now(tz=UTC),
        sha256="abc123",
    )
    block = ContentBlock(
        block_type="paragraph",
        block_number=1,
        text="Hello   Body  TEXT",
        extractor="txt",
        metadata={},
    )
    result = ExtractionResult(
        file_path=p,
        status="ok",
        document_metadata={},
        blocks=[block],
        error_message=None,
    )
    doc_id = store.upsert_document(fp, result)
    row = store.conn.execute(
        "SELECT content_hash FROM documents WHERE id=?", (doc_id,)
    ).fetchone()
    assert row["content_hash"] == normalized_content_hash("Hello   Body  TEXT")


def test_find_exact_duplicate_groups(store):
    a = _insert_doc(store, "/d/a.pdf", "SHARED")
    b = _insert_doc(store, "/d/b.pdf", "SHARED")
    _insert_doc(store, "/d/c.pdf", "UNIQUE")

    groups = store.find_exact_duplicate_groups()
    assert len(groups) == 1
    g = groups[0]
    assert g["hash"] == "SHARED"
    assert g["count"] == 2
    ids = {d["id"] for d in g["documents"]}
    assert ids == {a, b}
    # Each member exposes path/filename for the UI
    assert all("path" in d and "filename" in d for d in g["documents"])


def test_find_exact_duplicate_groups_empty_when_no_dupes(store):
    _insert_doc(store, "/d/a.pdf", "H1")
    _insert_doc(store, "/d/b.pdf", "H2")
    assert store.find_exact_duplicate_groups() == []


def test_find_content_duplicate_groups(store):
    a = _insert_doc(store, "/d/a.pdf", "BYTES-A", content_hash="CONTENT")
    b = _insert_doc(store, "/d/b.pdf", "BYTES-B", content_hash="CONTENT")
    _insert_doc(store, "/d/c.pdf", "BYTES-C", content_hash="OTHER")

    groups = store.find_content_duplicate_groups()
    assert len(groups) == 1
    g = groups[0]
    assert g["hash"] == "CONTENT"
    assert {d["id"] for d in g["documents"]} == {a, b}


def test_find_content_duplicate_groups_ignores_null_hash(store):
    _insert_doc(store, "/d/a.pdf", "S1", content_hash=None)
    _insert_doc(store, "/d/b.pdf", "S2", content_hash=None)
    # Two NULL content hashes must NOT be grouped together.
    assert store.find_content_duplicate_groups() == []


def test_find_content_duplicate_groups_excludes_exact_sha256_dupes(store):
    # If two docs are already byte-identical (same sha256) we don't want them
    # surfaced a second time as a content-duplicate.
    _insert_doc(store, "/d/a.pdf", "SAME", content_hash="C")
    _insert_doc(store, "/d/b.pdf", "SAME", content_hash="C")
    # Same content_hash but ALSO same sha256 -> belongs to the exact group only.
    assert store.find_content_duplicate_groups() == []


def test_delete_documents_removes_index_rows(store):
    a = _insert_doc(store, "/d/a.pdf", "H")
    _insert_block(store, a, "some text")
    store.conn.execute(
        "INSERT INTO content_fts(document_id, block_id, path, filename, extension, "
        "block_type, block_number, text) VALUES(?,?,?,?,?,?,?,?)",
        (a, 1, "/d/a.pdf", "a.pdf", ".pdf", "paragraph", "1", "some text"),
    )
    store.conn.commit()

    n = store.delete_documents([a])
    assert n == 1
    assert store.conn.execute("SELECT COUNT(*) FROM documents WHERE id=?", (a,)).fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM content_blocks WHERE document_id=?", (a,)).fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM content_fts WHERE document_id=?", (a,)).fetchone()[0] == 0


def test_delete_documents_empty_list_is_noop(store):
    assert store.delete_documents([]) == 0
