import pytest

from document_search.index.search_service import (
    _field_boost,
    _recency_boost,
    _rrf_fuse,
)


def test_rrf_fuse_simple_two_lists():
    bm25 = [1, 2, 3]
    vector = [2, 3, 1]
    fused = _rrf_fuse({"bm25": (bm25, 1.0), "vector": (vector, 1.0)}, k=60)
    assert [d for d, _ in fused][0] in (1, 2)
    assert fused[-1][0] == 3
    assert sorted(d for d, _ in fused) == [1, 2, 3]


def test_rrf_score_math_exact():
    fused = _rrf_fuse({"only": ([7, 8], 1.0)}, k=60)
    scores = dict(fused)
    assert scores[7] == pytest.approx(1.0 / 61)
    assert scores[8] == pytest.approx(1.0 / 62)


def test_rrf_weights_bias_a_list():
    fused = _rrf_fuse({"bm25": ([1, 2], 1.0), "vector": ([2, 1], 5.0)}, k=60)
    assert fused[0][0] == 2


def test_recency_boost_newer_scores_higher():
    older = _recency_boost("2020-01-01T00:00:00+00:00", now_iso="2026-01-01T00:00:00+00:00")
    newer = _recency_boost("2025-12-01T00:00:00+00:00", now_iso="2026-01-01T00:00:00+00:00")
    assert 0.0 <= older <= newer <= 1.0


def test_recency_boost_handles_bad_date():
    assert _recency_boost(None) == 0.0
    assert _recency_boost("not-a-date") == 0.0


def test_field_boost_matches_filename():
    assert _field_boost("annual report", "annual-report-2025.pdf") > 0.0
    assert _field_boost("annual report", "invoice.pdf") == 0.0


from datetime import UTC, datetime  # noqa: E402
from pathlib import Path  # noqa: E402

from document_search.index.search_service import search  # noqa: E402
from document_search.index.sqlite_store import SqliteStore  # noqa: E402


def _seed(tmp_path):
    """Two docs about 'soup'; both public. cabbage gets a query-aligned embedding,
    tomato an orthogonal one, so the vector ranker prefers cabbage even though both
    match BM25 'soup'."""
    store = SqliteStore(tmp_path / "test.db")
    alice = store.create_user("alice", "pw")
    now = datetime.now(tz=UTC).isoformat()
    docs = [
        ("/d/cabbage.txt", "h1", "cabbage soup recipe", [1.0, 0.0, 0.0]),
        ("/d/tomato.txt", "h2", "tomato soup recipe", [0.0, 1.0, 0.0]),
    ]
    block_of = {}
    for path, sha, body, _vec in docs:
        cur = store.conn.execute(
            "INSERT INTO documents(path, filename, extension, file_size, modified_at, "
            "sha256, indexed_at, status) VALUES(?,?,?,?,?,?,?,?)",
            (path, Path(path).name, ".txt", len(body), now, sha, now, "ok"),
        )
        doc_id = cur.lastrowid
        bcur = store.conn.execute(
            "INSERT INTO content_blocks(document_id, block_type, block_number, text, "
            "extractor, text_length) VALUES(?,?,?,?,?,?)",
            (doc_id, "paragraph", 1, body, "txt", len(body)),
        )
        store.conn.execute(
            "INSERT INTO content_fts(document_id, block_id, path, filename, extension, "
            "block_type, block_number, text) VALUES(?,?,?,?,?,?,?,?)",
            (doc_id, bcur.lastrowid, path, Path(path).name, ".txt", "paragraph", "1", body),
        )
        block_of[path] = (doc_id, bcur.lastrowid)
    store.conn.commit()
    store2 = SqliteStore(store.db_path)  # re-open -> backfill public ACL
    for (path, _sha, _body, vec) in docs:
        doc_id, block_id = block_of[path]
        store2.upsert_block_embedding(block_id, doc_id, vec, model="stub")
    return store2, alice


def test_keyword_mode_unchanged(tmp_path):
    store, alice = _seed(tmp_path)
    rows = search(store, "soup", user_id=alice, mode="keyword")
    paths = {r["path"] for r in rows}
    assert paths == {"/d/cabbage.txt", "/d/tomato.txt"}


def test_semantic_mode_prefers_vector_aligned_doc(tmp_path):
    store, alice = _seed(tmp_path)

    def stub(text):
        return [1.0, 0.0, 0.0]

    rows = search(store, "soup", user_id=alice, mode="semantic", embed_fn=stub)
    ordered = [r["path"] for r in rows]
    assert ordered[0] == "/d/cabbage.txt"


def test_hybrid_mode_returns_both_and_respects_acl(tmp_path):
    store, alice = _seed(tmp_path)

    def stub(text):
        return [1.0, 0.0, 0.0]

    rows = search(store, "soup", user_id=alice, mode="hybrid", embed_fn=stub)
    paths = {r["path"] for r in rows}
    assert paths == {"/d/cabbage.txt", "/d/tomato.txt"}


def test_semantic_falls_back_to_keyword_when_embed_unavailable(tmp_path):
    store, alice = _seed(tmp_path)
    rows = search(store, "soup", user_id=alice, mode="semantic", embed_fn=lambda t: None)
    paths = {r["path"] for r in rows}
    assert paths == {"/d/cabbage.txt", "/d/tomato.txt"}


def test_hybrid_acl_filters_out_restricted_doc(tmp_path):
    store, alice = _seed(tmp_path)
    bob = store.create_user("bob", "pw")
    now = datetime.now(tz=UTC).isoformat()
    public_id = store.conn.execute(
        "SELECT id FROM principals WHERE type='group' AND external_id='public'"
    ).fetchone()["id"]
    cab_id = store.conn.execute(
        "SELECT id FROM documents WHERE path='/d/cabbage.txt'"
    ).fetchone()["id"]
    alice_p = store.conn.execute(
        "SELECT principal_id FROM users WHERE username='alice'"
    ).fetchone()["principal_id"]
    store.conn.execute(
        "DELETE FROM document_acl WHERE document_id=? AND principal_id=?", (cab_id, public_id)
    )
    store.conn.execute(
        "INSERT INTO document_acl(document_id, principal_id, permission, granted_at) "
        "VALUES(?,?, 'read', ?)", (cab_id, alice_p, now),
    )
    store.conn.commit()

    def stub(text):
        return [1.0, 0.0, 0.0]

    bob_rows = search(store, "soup", user_id=bob, mode="hybrid", embed_fn=stub)
    assert {r["path"] for r in bob_rows} == {"/d/tomato.txt"}
