from datetime import UTC, datetime
from pathlib import Path

import pytest

from document_search.index.sqlite_store import SqliteStore


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "test.db")


def _insert_doc_with_block(store, path="/d/a.txt", text="hello"):
    now = datetime.now(tz=UTC).isoformat()
    cur = store.conn.execute(
        "INSERT INTO documents(path, filename, extension, file_size, modified_at, "
        "sha256, indexed_at, status) VALUES(?,?,?,?,?,?,?,?)",
        (path, Path(path).name, ".txt", len(text), now, "h", now, "ok"),
    )
    doc_id = cur.lastrowid
    bcur = store.conn.execute(
        "INSERT INTO content_blocks(document_id, block_type, block_number, text, "
        "extractor, text_length) VALUES(?,?,?,?,?,?)",
        (doc_id, "paragraph", 1, text, "txt", len(text)),
    )
    store.conn.commit()
    return doc_id, bcur.lastrowid


def test_block_embeddings_table_exists(store):
    cols = {c[1] for c in store.conn.execute("PRAGMA table_info(block_embeddings)").fetchall()}
    assert {"block_id", "document_id", "dim", "vector", "model", "created_at"}.issubset(cols)


def test_upsert_and_iter_block_embedding(store):
    doc_id, block_id = _insert_doc_with_block(store)
    store.upsert_block_embedding(block_id, doc_id, [0.1, 0.2, 0.3], model="nomic-embed-text")
    rows = list(store.iter_block_embeddings([doc_id]))
    assert len(rows) == 1
    got_block_id, got_doc_id, vec = rows[0]
    assert got_block_id == block_id
    assert got_doc_id == doc_id
    assert len(vec) == 3
    assert abs(vec[0] - 0.1) < 1e-6


def test_upsert_block_embedding_is_idempotent(store):
    doc_id, block_id = _insert_doc_with_block(store)
    store.upsert_block_embedding(block_id, doc_id, [1.0, 2.0], model="m")
    store.upsert_block_embedding(block_id, doc_id, [3.0, 4.0], model="m")
    rows = list(store.iter_block_embeddings([doc_id]))
    assert len(rows) == 1
    assert rows[0][2][0] == pytest.approx(3.0)


def test_get_blocks_without_embedding(store):
    doc_id, block_id = _insert_doc_with_block(store, path="/d/a.txt", text="alpha")
    doc2, block2 = _insert_doc_with_block(store, path="/d/b.txt", text="beta")
    store.upsert_block_embedding(block_id, doc_id, [0.0, 1.0], model="m")
    missing = store.get_blocks_without_embedding(limit=10)
    missing_ids = {m["block_id"] for m in missing}
    assert block2 in missing_ids
    assert block_id not in missing_ids


def test_iter_block_embeddings_scoped_to_doc_ids(store):
    doc1, b1 = _insert_doc_with_block(store, path="/d/a.txt")
    doc2, b2 = _insert_doc_with_block(store, path="/d/b.txt")
    store.upsert_block_embedding(b1, doc1, [1.0, 0.0], model="m")
    store.upsert_block_embedding(b2, doc2, [0.0, 1.0], model="m")
    only_doc1 = list(store.iter_block_embeddings([doc1]))
    assert {r[1] for r in only_doc1} == {doc1}
