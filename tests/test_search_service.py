from document_search.index.search_service import build_match_query


def test_build_match_query_with_filters():
    q = build_match_query("wartung", filetype=".pdf", path_filter="/documents", block_type="page")
    assert "extension:.pdf" in q
    assert "path:/documents*" in q
    assert "block_type:page" in q
