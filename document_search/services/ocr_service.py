from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path


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
    pages = convert_from_path(str(path))
    return [(pytesseract.image_to_string(page, lang=languages) or "").strip() for page in pages]


def ocr_pdf_page(path: Path, page_number: int, languages: str = "eng+deu") -> str:
    """OCR a single 1-based PDF page (rasterise just that page). '' on failure.

    Rasterising one page at a time keeps the per-page fallback cheap: only pages
    that actually need OCR are converted, instead of the whole document.
    """
    try:
        from pdf2image import convert_from_path
    except Exception:
        return ""
    pytesseract, _ = _load_ocr_dependencies()
    if not pytesseract:
        return ""
    try:
        pages = convert_from_path(str(path), first_page=page_number, last_page=page_number)
    except Exception:
        return ""
    if not pages:
        return ""
    try:
        return (pytesseract.image_to_string(pages[0], lang=languages) or "").strip()
    except Exception:
        return ""


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
