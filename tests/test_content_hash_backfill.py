"""_backfill_content_hash runs on every store open. It must (a) hash documents
from their blocks in block_number order, (b) leave block-less documents NULL, and
(c) do it in one query (not N+1) — this test locks the behaviour across that
optimisation."""

from document_search.index.sqlite_store import SqliteStore
from document_search.services.file_service import normalized_content_hash

_NOW = "2026-01-01T00:00:00"


def _doc(c, i):
    c.execute(
        "INSERT INTO documents(id,path,filename,extension,mime_type,file_size,"
        "modified_at,created_at,sha256,indexed_at,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (i, f"/d/f{i}", f"f{i}", ".pdf", "m", 1, _NOW, _NOW, f"h{i}", _NOW, "indexed"),
    )


def _block(c, doc_id, n, text):
    c.execute(
        "INSERT INTO content_blocks(document_id,block_type,block_number,text,extractor,text_length,metadata_json) "
        "VALUES(?,?,?,?,?,?,?)",
        (doc_id, "page", n, text, "Pdf", len(text), None),
    )


def test_backfill_hashes_blocks_in_order_and_skips_blockless(tmp_path):
    store = SqliteStore(tmp_path / "c.db")
    c = store.conn
    _doc(c, 1)
    _doc(c, 2)
    _doc(c, 3)  # no blocks → stays NULL
    _block(c, 1, 1, "world")  # inserted out of order on purpose
    _block(c, 1, 0, "hello")
    _block(c, 2, 0, "solo")
    c.commit()

    store._backfill_content_hash()

    h1 = c.execute("SELECT content_hash FROM documents WHERE id=1").fetchone()[0]
    h2 = c.execute("SELECT content_hash FROM documents WHERE id=2").fetchone()[0]
    h3 = c.execute("SELECT content_hash FROM documents WHERE id=3").fetchone()[0]
    assert h1 == normalized_content_hash("hello world")  # block_number 0 then 1
    assert h2 == normalized_content_hash("solo")
    assert h3 is None  # block-less document is excluded


def test_backfill_is_idempotent_and_leaves_hashed_docs_alone(tmp_path):
    store = SqliteStore(tmp_path / "c.db")
    c = store.conn
    _doc(c, 1)
    _block(c, 1, 0, "alpha")
    c.commit()
    store._backfill_content_hash()
    first = c.execute("SELECT content_hash FROM documents WHERE id=1").fetchone()[0]
    # A second run must not change an already-hashed document.
    store._backfill_content_hash()
    assert c.execute("SELECT content_hash FROM documents WHERE id=1").fetchone()[0] == first
