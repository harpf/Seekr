"""Scan-path extraction with FORCED OCR, isolated from the global OCR env flags.

The normal extractors gate OCR on process-wide env vars; toggling those inside a
job would race concurrent index jobs. The scan path instead calls the ocr_service
primitives directly (they do not check enable flags), so OCR is always applied for
scanned input without mutating shared state.
"""
from __future__ import annotations

from pathlib import Path

from document_search.extractors import extractor_for
from document_search.extractors.plugins.image_extractor import (
    IMAGE_SUFFIXES,
    ocr_image_file,
)
from document_search.models import ContentBlock, ExtractionResult
from document_search.services.ocr_service import ocr_pdf_pages


def _pdf_page_count(path: Path) -> int:
    from pypdf import PdfReader

    return len(PdfReader(str(path)).pages)


def _pdf_native_text(path: Path) -> dict[int, str]:
    """1-based page number -> native (non-OCR) text, best effort."""
    from pypdf import PdfReader

    out: dict[int, str] = {}
    reader = PdfReader(str(path))
    for i, page in enumerate(reader.pages, start=1):
        try:
            out[i] = (page.extract_text() or "").strip()
        except Exception:  # noqa: BLE001
            out[i] = ""
    return out


def _extract_image(path: Path, languages: str) -> ExtractionResult:
    text = ocr_image_file(path, languages=languages)
    blocks = (
        [ContentBlock("ocr_page", 1, text, "ImageTextExtractor", {})] if text else []
    )
    return ExtractionResult(file_path=path, status="ok", blocks=blocks)


def _extract_pdf_forced(path: Path, languages: str) -> ExtractionResult:
    native = _pdf_native_text(path)
    page_count = _pdf_page_count(path)
    pages_needing_ocr = [n for n in range(1, page_count + 1) if not native.get(n)]
    ocr_text = (
        ocr_pdf_pages(path, pages_needing_ocr, languages=languages)
        if pages_needing_ocr
        else {}
    )
    blocks: list[ContentBlock] = []
    for n in range(1, page_count + 1):
        if native.get(n):
            blocks.append(ContentBlock("page", n, native[n], "ScanPdfExtractor", {}))
        elif ocr_text.get(n, "").strip():
            blocks.append(
                ContentBlock("ocr_page", n, ocr_text[n].strip(), "ScanPdfExtractor", {})
            )
    return ExtractionResult(file_path=path, status="ok", blocks=blocks)


def extract_for_scan(path: Path, languages: str) -> ExtractionResult:
    """Extract text from a scanned file, forcing OCR for images and image-PDFs."""
    ext = path.suffix.lower()
    try:
        if ext in IMAGE_SUFFIXES:
            return _extract_image(path, languages)
        if ext == ".pdf":
            return _extract_pdf_forced(path, languages)
        extractor = extractor_for(ext)
        if extractor is None:
            return ExtractionResult(
                file_path=path,
                status="error",
                error_message=f"Unsupported scan format: {ext}",
            )
        return extractor.extract(path)
    except Exception as exc:  # noqa: BLE001 - surface as error row, never crash the job
        return ExtractionResult(
            file_path=path, status="error", error_message=f"{type(exc).__name__}: {exc}"
        )
