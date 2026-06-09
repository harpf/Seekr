"""OCR accuracy for PDFs: config wiring, per-page fallback, embedded-image OCR
and the force-OCR switch. Hermetic — the Tesseract/poppler boundary is mocked,
so no system OCR install is required."""

from pathlib import Path

import document_search.extractors.pdf_extractor as pdf_mod
import document_search.services.ocr_service as ocr
from document_search.config import AppConfig, OcrConfig, load_config, ocr_env_overrides
from document_search.extractors.pdf_extractor import PdfTextExtractor

# --- config wiring ----------------------------------------------------------


def test_ocr_config_parses_force_ocr(tmp_path):
    import json

    p = tmp_path / "config.json"
    p.write_text(
        json.dumps({"ocr": {"enabled": True, "languages": ["deu"], "force_ocr": True}}),
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.ocr.enabled is True
    assert cfg.ocr.force_ocr is True
    assert cfg.ocr.languages == ["deu"]


def test_ocr_env_overrides_maps_config_to_env():
    cfg = AppConfig(ocr=OcrConfig(enabled=True, languages=["deu", "eng"], force_ocr=True))
    ov = ocr_env_overrides(cfg)
    assert ov["DOCUMENT_SEARCH_OCR_ENABLED"] == "true"
    assert ov["DOCUMENT_SEARCH_OCR_LANG"] == "deu+eng"
    assert ov["DOCUMENT_SEARCH_FORCE_OCR"] == "true"


def test_ocr_env_overrides_disabled_by_default():
    ov = ocr_env_overrides(AppConfig())
    assert ov["DOCUMENT_SEARCH_OCR_ENABLED"] == "false"
    assert ov["DOCUMENT_SEARCH_FORCE_OCR"] == "false"


# --- ocr_service helpers ----------------------------------------------------


def test_resolve_ocr_language_env_overrides_default(monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_OCR_LANG", "fra+ita")
    assert ocr.resolve_ocr_language("deu+eng") == "fra+ita"


def test_resolve_ocr_language_default_when_no_env(monkeypatch):
    monkeypatch.delenv("DOCUMENT_SEARCH_OCR_LANG", raising=False)
    assert ocr.resolve_ocr_language("deu+eng") == "deu+eng"


def test_ocr_enabled_and_force_flags(monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_OCR_ENABLED", "true")
    monkeypatch.setenv("DOCUMENT_SEARCH_FORCE_OCR", "true")
    assert ocr.ocr_enabled() is True
    assert ocr.force_ocr_enabled() is True
    monkeypatch.setenv("DOCUMENT_SEARCH_FORCE_OCR", "false")
    assert ocr.force_ocr_enabled() is False


def test_ocr_pdf_page_returns_empty_without_deps(monkeypatch):
    monkeypatch.setattr(ocr, "_load_ocr_dependencies", lambda: (None, None))
    assert ocr.ocr_pdf_page(Path("x.pdf"), 1) == ""


# --- PDF extractor behaviour ------------------------------------------------


class _FakeImg:
    def __init__(self, data: bytes):
        self.data = data


class _FakePage:
    def __init__(self, text: str, images=()):
        self._text = text
        self.images = [_FakeImg(d) for d in images]

    def extract_text(self):
        return self._text


class _FakeReader:
    def __init__(self, pages):
        self.pages = pages
        self.metadata = {}


def _patch_reader(monkeypatch, pages):
    monkeypatch.setattr(pdf_mod, "PdfReader", lambda _p: _FakeReader(pages))


def test_scanned_page_is_ocred_when_enabled(monkeypatch):
    """A page with no embedded text is OCR'd (per-page fallback)."""
    _patch_reader(monkeypatch, [_FakePage("")])
    monkeypatch.setenv("DOCUMENT_SEARCH_OCR_ENABLED", "true")
    monkeypatch.delenv("DOCUMENT_SEARCH_FORCE_OCR", raising=False)
    monkeypatch.setattr(
        pdf_mod, "ocr_pdf_pages",
        lambda path, pages, languages="eng+deu": {1: "Rechnung Nr. 4711 ACME GmbH"} if 1 in pages else {},
    )

    result = PdfTextExtractor().extract(Path("scan.pdf"))
    assert result.status == "ok"
    ocr_blocks = [b for b in result.blocks if b.block_type == "ocr_page"]
    assert len(ocr_blocks) == 1
    assert "Rechnung Nr. 4711" in ocr_blocks[0].text
    assert ocr_blocks[0].metadata["source"] == "ocr"


def test_no_ocr_when_disabled(monkeypatch):
    _patch_reader(monkeypatch, [_FakePage("")])
    monkeypatch.delenv("DOCUMENT_SEARCH_OCR_ENABLED", raising=False)
    monkeypatch.delenv("DOCUMENT_SEARCH_FORCE_OCR", raising=False)
    monkeypatch.setattr(
        pdf_mod, "ocr_pdf_pages",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("OCR must not run when disabled")),
    )
    result = PdfTextExtractor().extract(Path("scan.pdf"))
    assert result.status == "ok"
    assert result.blocks == []
    assert result.document_metadata["likely_scanned"] is True


def test_embedded_image_on_text_page_is_ocred(monkeypatch):
    """A text page keeps its 'page' block AND OCRs embedded images (the logo case)."""
    _patch_reader(monkeypatch, [_FakePage("Invoice body text", images=[b"LOGO_BYTES"])])
    monkeypatch.setenv("DOCUMENT_SEARCH_OCR_ENABLED", "true")
    monkeypatch.delenv("DOCUMENT_SEARCH_FORCE_OCR", raising=False)
    monkeypatch.setattr(pdf_mod, "ocr_image_bytes", lambda data, languages="eng+deu": "ACME GmbH Logo")
    # No textless pages → no full-page OCR call expected.
    monkeypatch.setattr(
        pdf_mod, "ocr_pdf_pages",
        lambda path, pages, languages="eng+deu": (
            {} if not pages else (_ for _ in ()).throw(AssertionError("full-page OCR must not run on a text page"))
        ),
    )

    result = PdfTextExtractor().extract(Path("invoice.pdf"))
    types = {b.block_type for b in result.blocks}
    assert "page" in types
    img_blocks = [b for b in result.blocks if b.block_type == "ocr_image"]
    assert len(img_blocks) == 1
    assert "ACME GmbH Logo" in img_blocks[0].text


def test_force_ocr_ocrs_every_page_and_skips_embedded(monkeypatch):
    """force_ocr=true rasterises and OCRs every page (max recall); embedded-image
    OCR is skipped because the full-page raster already contains the images."""
    _patch_reader(monkeypatch, [_FakePage("native text", images=[b"LOGO"])])
    monkeypatch.setenv("DOCUMENT_SEARCH_OCR_ENABLED", "true")
    monkeypatch.setenv("DOCUMENT_SEARCH_FORCE_OCR", "true")
    monkeypatch.setattr(
        pdf_mod, "ocr_pdf_pages",
        lambda path, pages, languages="eng+deu": {p: "FULL PAGE OCR incl logo" for p in pages},
    )
    monkeypatch.setattr(
        pdf_mod, "ocr_image_bytes",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("embedded-image OCR must not run under force_ocr")),
    )

    result = PdfTextExtractor().extract(Path("invoice.pdf"))
    types = [b.block_type for b in result.blocks]
    assert "page" in types
    assert "ocr_page" in types
    assert "ocr_image" not in types
