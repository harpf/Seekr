from document_search.index.search_service import build_match_query


def test_build_match_query_with_filters():
    q = build_match_query("wartung", filetype=".pdf", path_filter="/documents", block_type="page")
    assert "extension:pdf" in q        # dot stripped
    assert "extension:.pdf" not in q
    assert "path:/documents*" in q
    assert "block_type:page" in q


def test_build_match_query_empty_returns_none():
    result = build_match_query("", filetype=None, path_filter=None, block_type=None)
    assert result is None


def test_build_match_query_wildcard_returns_none():
    result = build_match_query("*", filetype=None, path_filter=None, block_type=None)
    assert result is None


def test_build_match_query_whitespace_only_returns_none():
    result = build_match_query("   ", filetype=None, path_filter=None, block_type=None)
    assert result is None


def test_build_match_query_comma_separated_extensions():
    q = build_match_query("report", filetype="pdf,docx", path_filter=None, block_type=None)
    assert "(extension:pdf OR extension:docx)" in q


def test_build_match_query_single_extension_no_dot():
    q = build_match_query("report", filetype=".pdf", path_filter=None, block_type=None)
    assert "extension:pdf" in q
    assert "OR" not in q
