"""OCR tuning: configurable DPI, poppler thread_count, batch rasterisation and
parallel per-page OCR. Hermetic — pdf2image and pytesseract are faked, so no
Tesseract/poppler install is required."""

import sys
import types
from pathlib import Path

import document_search.services.ocr_service as ocr


def test_ocr_dpi_default_and_env(monkeypatch):
    monkeypatch.delenv("DOCUMENT_SEARCH_OCR_DPI", raising=False)
    assert ocr.ocr_dpi() == 200
    monkeypatch.setenv("DOCUMENT_SEARCH_OCR_DPI", "300")
    assert ocr.ocr_dpi() == 300
    # Garbage falls back to the default.
    monkeypatch.setenv("DOCUMENT_SEARCH_OCR_DPI", "notanint")
    assert ocr.ocr_dpi() == 200


class _Img:
    def __init__(self, n):
        self.n = n


def _fake_pdf2image(monkeypatch, recorder):
    fake = types.ModuleType("pdf2image")

    def convert_from_path(path, dpi=None, first_page=None, last_page=None, thread_count=None):
        recorder.append({"dpi": dpi, "first": first_page, "last": last_page, "threads": thread_count})
        return [_Img(p) for p in range(first_page, last_page + 1)]

    fake.convert_from_path = convert_from_path
    monkeypatch.setitem(sys.modules, "pdf2image", fake)


def _fake_tesseract(monkeypatch):
    class _Pyt:
        @staticmethod
        def image_to_string(img, lang):
            return f"text-p{img.n}-{lang}"

    monkeypatch.setattr(ocr, "_load_ocr_dependencies", lambda: (_Pyt, object()))


def test_ocr_pdf_pages_batch_rasterises_span_and_ocrs_each(monkeypatch):
    rec = []
    _fake_pdf2image(monkeypatch, rec)
    _fake_tesseract(monkeypatch)
    monkeypatch.setenv("DOCUMENT_SEARCH_OCR_DPI", "300")
    monkeypatch.delenv("DOCUMENT_SEARCH_OCR_PAGE_WORKERS", raising=False)

    out = ocr.ocr_pdf_pages(Path("x.pdf"), [2, 3], languages="deu")
    assert out == {2: "text-p2-deu", 3: "text-p3-deu"}
    # One batch rasterisation over the page span at the configured DPI.
    assert len(rec) == 1
    assert rec[0]["dpi"] == 300
    assert rec[0]["first"] == 2 and rec[0]["last"] == 3


def test_ocr_pdf_pages_parallel_matches_serial(monkeypatch):
    rec = []
    _fake_pdf2image(monkeypatch, rec)
    _fake_tesseract(monkeypatch)
    monkeypatch.setenv("DOCUMENT_SEARCH_OCR_PAGE_WORKERS", "4")
    out = ocr.ocr_pdf_pages(Path("x.pdf"), [1, 2, 3], languages="eng")
    assert out == {1: "text-p1-eng", 2: "text-p2-eng", 3: "text-p3-eng"}


def test_ocr_pdf_pages_empty_and_missing_deps(monkeypatch):
    assert ocr.ocr_pdf_pages(Path("x.pdf"), [], languages="eng") == {}
    monkeypatch.setattr(ocr, "_load_ocr_dependencies", lambda: (None, None))
    _fake_pdf2image(monkeypatch, [])
    assert ocr.ocr_pdf_pages(Path("x.pdf"), [1], languages="eng") == {}
