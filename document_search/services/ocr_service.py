from __future__ import annotations

import io
import os
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Keep each Tesseract process single-threaded: we parallelise at the page and
# document level ourselves, so per-process OpenMP threads would only oversubscribe
# the cores. setdefault lets an operator override it explicitly.
os.environ.setdefault("OMP_THREAD_LIMIT", "1")


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, "").strip()))
    except (TypeError, ValueError):
        return default


def _cpu_default(cap: int = 4) -> int:
    return max(1, min(cap, os.cpu_count() or 1))


def ocr_dpi() -> int:
    """Rasterisation DPI for OCR (DOCUMENT_SEARCH_OCR_DPI). Higher = more accurate
    but slower; 200 is a good speed/accuracy default, 300 for dense invoices."""
    return _int_env("DOCUMENT_SEARCH_OCR_DPI", 200, minimum=50)


def ocr_page_workers() -> int:
    """Threads OCR-ing pages of ONE document in parallel
    (DOCUMENT_SEARCH_OCR_PAGE_WORKERS). Default 1 to avoid oversubscribing when
    the index job already parallelises across documents — raise it when indexing
    a few very large scanned PDFs."""
    return _int_env("DOCUMENT_SEARCH_OCR_PAGE_WORKERS", 1)


def _poppler_threads() -> int:
    return _int_env("DOCUMENT_SEARCH_OCR_RASTER_THREADS", _cpu_default())


def ocr_enabled() -> bool:
    """OCR master switch (DOCUMENT_SEARCH_OCR_ENABLED)."""
    return os.getenv("DOCUMENT_SEARCH_OCR_ENABLED", "false").lower() == "true"


def force_ocr_enabled() -> bool:
    """Force-OCR every PDF page regardless of native text (DOCUMENT_SEARCH_FORCE_OCR)."""
    return os.getenv("DOCUMENT_SEARCH_FORCE_OCR", "false").lower() == "true"


def resolve_ocr_language(default: str = "eng+deu") -> str:
    """Per-source OCR language; DOCUMENT_SEARCH_OCR_LANG overrides the default."""
    return os.getenv("DOCUMENT_SEARCH_OCR_LANG", "").strip() or default


def _load_ocr_dependencies():
    try:
        import pytesseract
        from PIL import Image

        return pytesseract, Image
    except Exception:
        return None, None


def ocr_image_bytes(blob: bytes, languages: str = "eng+deu") -> str:
    pytesseract, image_mod = _load_ocr_dependencies()
    if not pytesseract or not image_mod:
        return ""
    try:
        image = image_mod.open(io.BytesIO(blob))
        return (pytesseract.image_to_string(image, lang=languages) or "").strip()
    except Exception:
        return ""


def ocr_pdf_file(path: Path, languages: str = "eng+deu") -> list[str]:
    try:
        from pdf2image import convert_from_path
    except Exception:
        return []
    pytesseract, _ = _load_ocr_dependencies()
    if not pytesseract:
        return []
    pages = convert_from_path(str(path), dpi=ocr_dpi(), thread_count=_poppler_threads())
    return [(pytesseract.image_to_string(page, lang=languages) or "").strip() for page in pages]


def ocr_pdf_page(path: Path, page_number: int, languages: str = "eng+deu") -> str:
    """OCR a single 1-based PDF page (rasterise just that page). '' on failure."""
    return ocr_pdf_pages(path, [page_number], languages).get(page_number, "")


def ocr_pdf_pages(
    path: Path, page_numbers: list[int], languages: str = "eng+deu"
) -> dict[int, str]:
    """OCR the given 1-based PDF pages, returning ``{page_number: text}``.

    Rasterises the spanned page range **once** (instead of re-opening the PDF per
    page) at the configured DPI with poppler ``thread_count``, then OCRs the pages
    — in parallel when ``DOCUMENT_SEARCH_OCR_PAGE_WORKERS`` > 1. Empty input or a
    missing Tesseract/poppler install yields ``{}``.
    """
    pages = sorted({p for p in page_numbers if p and p > 0})
    if not pages:
        return {}
    try:
        from pdf2image import convert_from_path
    except Exception:
        return {}
    pytesseract, _ = _load_ocr_dependencies()
    if not pytesseract:
        return {}

    lo, hi = pages[0], pages[-1]
    try:
        images = convert_from_path(
            str(path), dpi=ocr_dpi(), first_page=lo, last_page=hi,
            thread_count=_poppler_threads(),
        )
    except Exception:
        return {}

    # images[k] corresponds to page (lo + k).
    wanted = [(p, images[p - lo]) for p in pages if 0 <= p - lo < len(images)]

    def _ocr(item: tuple[int, object]) -> tuple[int, str]:
        page, image = item
        try:
            return page, (pytesseract.image_to_string(image, lang=languages) or "").strip()
        except Exception:
            return page, ""

    workers = min(ocr_page_workers(), len(wanted)) if wanted else 1
    if workers <= 1:
        results = [_ocr(item) for item in wanted]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_ocr, wanted))
    return {page: text for page, text in results if text}


def ocr_office_embedded_images(path: Path, languages: str = "eng+deu") -> list[str]:
    texts: list[str] = []
    if path.suffix.lower() not in {".docx", ".pptx"}:
        return texts
    media_prefixes = ["word/media/", "ppt/media/"]
    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if not any(name.startswith(prefix) for prefix in media_prefixes):
                    continue
                if not name.lower().endswith((".png", ".jpg", ".jpeg", ".bmp", ".tiff")):
                    continue
                text = ocr_image_bytes(zf.read(name), languages=languages)
                if text:
                    texts.append(text)
    except Exception:
        return texts
    return texts
