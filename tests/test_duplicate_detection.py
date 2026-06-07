from pathlib import Path

import pytest

from document_search.services.file_service import normalized_content_hash


def test_identical_text_same_hash():
    a = normalized_content_hash("Hello World")
    b = normalized_content_hash("Hello World")
    assert a == b


def test_whitespace_and_case_insensitive():
    a = normalized_content_hash("Hello   World\n\tFoo")
    b = normalized_content_hash("  hello world foo  ")
    assert a == b


def test_different_text_different_hash():
    a = normalized_content_hash("the quick brown fox")
    b = normalized_content_hash("a slow green turtle")
    assert a != b


def test_empty_text_returns_none():
    assert normalized_content_hash("") is None
    assert normalized_content_hash("   \n\t  ") is None


def test_only_first_8kb_considered():
    base = "x" * 8192
    a = normalized_content_hash(base + "AAAA")
    b = normalized_content_hash(base + "BBBB")
    # Tails beyond 8 KB are truncated, so the hashes collide.
    assert a == b


def test_returns_hex_string():
    h = normalized_content_hash("some content here")
    assert isinstance(h, str)
    assert len(h) == 64  # sha256 hex digest
    int(h, 16)  # valid hex
