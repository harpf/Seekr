from pathlib import Path

import document_search.extractors as ext
from document_search.config import AppConfig
from document_search.crawler import iter_documents
from document_search.extractors import (
    ExtractionResult,
    TextExtractor,
    register_extractor,
)


def test_iter_documents_filters(tmp_path: Path):
    (tmp_path / "ok.txt").write_text("x", encoding="utf-8")
    (tmp_path / "~$temp.docx").write_text("x", encoding="utf-8")
    cfg = AppConfig(supported_extensions=[".txt", ".docx"])
    files = list(iter_documents([tmp_path], cfg))
    assert any(f.name == "ok.txt" for f in files)
    assert all(not f.name.startswith("~$") for f in files)


def test_plugin_suffix_is_crawled_when_in_config(tmp_path: Path):
    """A suffix served by a (plugin) extractor is crawled once the operator adds
    it to supported_extensions — the crawler gates on the registry, not a
    hard-coded suffix map a plugin author cannot edit."""

    class _XyzExtractor(TextExtractor):
        def extract(self, file_path: Path) -> ExtractionResult:
            return ExtractionResult(file_path=file_path, status="ok", blocks=[])

    register_extractor(".xyz", _XyzExtractor())
    try:
        (tmp_path / "data.xyz").write_text("x", encoding="utf-8")
        cfg = AppConfig(supported_extensions=[".xyz"])
        files = list(iter_documents([tmp_path], cfg))
        assert any(f.name == "data.xyz" for f in files)
    finally:
        ext._REGISTRY.pop(".xyz", None)


def test_unextractable_suffix_skipped_even_if_in_config(tmp_path: Path):
    """A suffix listed in config but with no registered extractor is skipped:
    enabling a type in config alone is not enough — Seekr must be able to read it."""
    (tmp_path / "data.zzz").write_text("x", encoding="utf-8")
    cfg = AppConfig(supported_extensions=[".zzz"])
    files = list(iter_documents([tmp_path], cfg))
    assert not any(f.name == "data.zzz" for f in files)
