from datetime import UTC, datetime
from pathlib import Path

import pytest

from document_search.index.search_service import (
    FtsQueryError,
    count_documents,
    search,
)
from document_search.index.sqlite_store import SqliteStore


def _seed(store: SqliteStore, n: int, body: str = "alpha") -> None:
    """Insert n docs, each with one content block + FTS row, all bypassing ACL setup."""
    now = datetime.now(tz=UTC).isoformat()
    for i in range(n):
        path = f"/d/doc{i:03d}.txt"
        text = f"{body} document number {i}"
        cursor = store.conn.execute(
            "INSERT INTO documents(path, filename, extension, file_size, modified_at, sha256, indexed_at, status) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (path, Path(path).name, ".txt", len(text), now, f"h{i}", now, "ok"),
        )
        doc_id = cursor.lastrowid
        bcursor = store.conn.execute(
            "INSERT INTO content_blocks(document_id, block_type, block_number, text, extractor, text_length) "
            "VALUES(?,?,?,?,?,?)",
            (doc_id, "paragraph", 1, text, "txt", len(text)),
        )
        store.conn.execute(
            "INSERT INTO content_fts(document_id, block_id, path, filename, extension, block_type, block_number, text) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (doc_id, bcursor.lastrowid, path, Path(path).name, ".txt", "paragraph", "1", text),
        )
    store.conn.commit()


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "test.db")


def test_search_offset_returns_disjoint_pages(store):
    _seed(store, 30)
    page1 = search(store, "alpha", 10, 0, bypass_acl=True)
    page2 = search(store, "alpha", 10, 10, bypass_acl=True)
    ids1 = {r["document_id"] for r in page1}
    ids2 = {r["document_id"] for r in page2}
    assert len(ids1) == 10
    assert len(ids2) == 10
    assert ids1.isdisjoint(ids2)


def test_browse_all_offset_paginates(store):
    _seed(store, 30)
    page1 = search(store, "", 10, 0, bypass_acl=True)
    page2 = search(store, "", 10, 10, bypass_acl=True)
    ids1 = {r["document_id"] for r in page1}
    ids2 = {r["document_id"] for r in page2}
    assert len(ids1) == 10
    assert len(ids2) == 10
    assert ids1.isdisjoint(ids2)


def test_offset_beyond_end_returns_empty(store):
    _seed(store, 5)
    rows = search(store, "alpha", 10, 100, bypass_acl=True)
    assert rows == []


def test_count_documents_matches_total(store):
    _seed(store, 17)
    assert count_documents(store, "alpha", bypass_acl=True) == 17
    assert count_documents(store, "", bypass_acl=True) == 17


def test_unbalanced_quote_raises_fts_query_error(store):
    _seed(store, 3)
    with pytest.raises(FtsQueryError):
        search(store, 'alpha"', 10, 0, bypass_acl=True)


def test_fts_query_error_is_value_error_subclass():
    assert issubclass(FtsQueryError, ValueError)


def test_count_documents_unbalanced_quote_raises(store):
    _seed(store, 3)
    with pytest.raises(FtsQueryError):
        count_documents(store, 'alpha"', bypass_acl=True)
