from document_search.index.search_service import build_match_query


def test_build_match_query_with_filters():
    q = build_match_query("wartung", filetype=".pdf", block_type="page")
    assert "extension:pdf" in q        # dot stripped
    assert "extension:.pdf" not in q
    assert "block_type:page" in q


def test_build_match_query_path_filter_not_in_fts_match():
    # path_filter must NOT appear in the FTS MATCH string —
    # paths contain '/' which the FTS5 tokenizer treats as a separator,
    # causing a sqlite3.OperationalError when used in MATCH.
    q = build_match_query("wartung", filetype=None, block_type=None)
    assert q is not None
    assert "path:" not in q
    assert "/" not in q


def test_build_match_query_empty_returns_none():
    result = build_match_query("", filetype=None, block_type=None)
    assert result is None


def test_build_match_query_wildcard_returns_none():
    result = build_match_query("*", filetype=None, block_type=None)
    assert result is None


def test_build_match_query_whitespace_only_returns_none():
    result = build_match_query("   ", filetype=None, block_type=None)
    assert result is None


def test_build_match_query_comma_separated_extensions():
    q = build_match_query("report", filetype="pdf,docx", block_type=None)
    assert "(extension:pdf OR extension:docx)" in q


def test_build_match_query_single_extension_no_dot():
    q = build_match_query("report", filetype=".pdf", block_type=None)
    assert "extension:pdf" in q
    assert "OR" not in q
