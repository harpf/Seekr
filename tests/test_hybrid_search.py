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
