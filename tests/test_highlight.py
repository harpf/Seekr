import pytest

pytest.importorskip("fastapi")

from document_search.app import highlight_terms


def test_highlight_terms_marks_query_words():
    rendered = highlight_terms("maintenance of anlage required", "maintenance AND anlage")
    assert "<mark>maintenance</mark>" in rendered.lower()
    assert "<mark>anlage</mark>" in rendered.lower()
