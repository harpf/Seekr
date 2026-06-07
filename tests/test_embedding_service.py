from document_search.config import AppConfig, load_config


def test_appconfig_has_semantic_defaults():
    cfg = AppConfig()
    assert cfg.semantic_search_enabled is False
    assert cfg.embed_model == "nomic-embed-text"
    assert cfg.bm25_weight == 1.0
    assert cfg.vector_weight == 1.0


def test_load_config_reads_semantic_flags(tmp_path):
    import json
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "semantic_search_enabled": True,
        "embed_model": "mxbai-embed-large",
        "bm25_weight": 2.0,
        "vector_weight": 0.5,
    }), encoding="utf-8")
    cfg = load_config(p)
    assert cfg.semantic_search_enabled is True
    assert cfg.embed_model == "mxbai-embed-large"
    assert cfg.bm25_weight == 2.0
    assert cfg.vector_weight == 0.5


import json
import urllib.error

import pytest

from document_search.services import embedding_service as es
from document_search.services.embedding_service import (
    EmbeddingService,
    cosine_similarity,
    pack_vector,
    rank_by_cosine,
    unpack_vector,
)


def test_pack_unpack_roundtrip():
    v = [0.1, -0.2, 0.3, 1.0]
    blob = pack_vector(v)
    assert isinstance(blob, (bytes, bytearray))
    out = unpack_vector(blob)
    assert len(out) == 4
    for a, b in zip(v, out, strict=True):
        assert abs(a - b) < 1e-6


def test_cosine_similarity_basic():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_similarity_zero_vector_is_zero():
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_rank_by_cosine_orders_descending():
    query = [1.0, 0.0]
    candidates = [
        (10, [0.0, 1.0]),
        (20, [1.0, 0.0]),
        (30, [0.7, 0.7]),
    ]
    ranked = rank_by_cosine(query, candidates)
    assert [doc_id for doc_id, _ in ranked] == [20, 30, 10]
    scores = [s for _, s in ranked]
    assert scores == sorted(scores, reverse=True)


def test_rank_by_cosine_skips_dim_mismatch():
    query = [1.0, 0.0, 0.0]
    candidates = [(1, [1.0, 0.0]), (2, [1.0, 0.0, 0.0])]
    ranked = rank_by_cosine(query, candidates)
    assert [doc_id for doc_id, _ in ranked] == [2]


def test_embed_calls_ollama_and_parses(monkeypatch):
    captured = {}

    class _FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return json.dumps({"embedding": [0.5, 0.25, -0.1]}).encode()

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        captured["timeout"] = timeout
        return _FakeResp()

    monkeypatch.setattr(es.urllib.request, "urlopen", fake_urlopen)
    svc = EmbeddingService(base_url="http://ollama:11434", model="nomic-embed-text")
    vec = svc.embed("hello world")
    assert vec == [0.5, 0.25, -0.1]
    assert captured["url"].endswith("/api/embeddings")
    assert captured["body"]["model"] == "nomic-embed-text"
    assert captured["body"]["prompt"] == "hello world"


def test_embed_returns_none_when_ollama_unreachable(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(es.urllib.request, "urlopen", boom)
    svc = EmbeddingService()
    assert svc.embed("anything") is None


def test_try_load_sqlite_vec_returns_false_without_extension(tmp_path):
    import sqlite3
    conn = sqlite3.connect(tmp_path / "x.db")
    assert es.try_load_sqlite_vec(conn) is False
