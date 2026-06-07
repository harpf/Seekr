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
