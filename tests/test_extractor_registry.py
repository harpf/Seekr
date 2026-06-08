from __future__ import annotations

from pathlib import Path

import pytest

from document_search.extractors import (
    BUILTIN_EXTENSIONS,
    EXTRACTOR_API_VERSION,
    ContentBlock,
    ExtractionResult,
    TextExtractor,
    extractor_for,
    register_extractor,
    supported_extensions,
)


def test_public_contract_is_reexported() -> None:
    assert EXTRACTOR_API_VERSION == 1
    assert isinstance(BUILTIN_EXTENSIONS, frozenset)
    # The contract symbols are the real classes from base/models.
    assert TextExtractor is not None
    assert ExtractionResult is not None
    assert ContentBlock is not None


@pytest.mark.parametrize(
    "suffix",
    [".pdf", ".docx", ".pptx", ".txt", ".md", ".doc", ".ppt"],
)
def test_builtin_extractor_resolves(suffix: str) -> None:
    extractor = extractor_for(suffix)
    assert isinstance(extractor, TextExtractor)


def test_builtin_extractor_resolves_case_insensitive() -> None:
    assert isinstance(extractor_for(".PDF"), TextExtractor)
    assert isinstance(extractor_for(".Docx"), TextExtractor)


def test_extractor_for_unknown_returns_none() -> None:
    assert extractor_for(".nope") is None


def test_supported_extensions_lists_builtins() -> None:
    exts = supported_extensions()
    for suffix in [".pdf", ".docx", ".pptx", ".txt", ".md", ".doc", ".ppt"]:
        assert suffix in exts


def test_register_extractor_adds_a_new_suffix() -> None:
    class FakeExtractor(TextExtractor):
        def extract(self, file_path: Path) -> ExtractionResult:
            return ExtractionResult(
                file_path=file_path,
                status="ok",
                blocks=[
                    ContentBlock(
                        block_type="text",
                        block_number=0,
                        text="hello",
                        extractor="fake",
                    )
                ],
            )

    from document_search import extractors as _ext

    register_extractor(".fake", FakeExtractor())
    try:
        resolved = extractor_for(".fake")
        assert isinstance(resolved, FakeExtractor)
        assert ".fake" in supported_extensions()
    finally:
        _ext._REGISTRY.pop(".fake", None)
