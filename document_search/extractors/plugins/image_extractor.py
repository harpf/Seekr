"""Image OCR extractor (.jpg/.png/.tiff). Drop-in plugin: auto-discovered by the
extractor loader. Images carry no native text layer, so extraction is always OCR.
"""
from __future__ import annotations

from pathlib import Path

from document_search.extractors.base import TextExtractor
from document_search.models import ContentBlock, ExtractionResult
from document_search.services.ocr_service import ocr_image_bytes, resolve_ocr_language

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".tif", ".tiff")


def ocr_image_file(path: Path, languages: str | None = None) -> str:
    """OCR a single image file, returning the recognised text (or "")."""
    lang = languages or resolve_ocr_language()
    return ocr_image_bytes(path.read_bytes(), languages=lang).strip()


class ImageTextExtractor(TextExtractor):
    suffixes = IMAGE_SUFFIXES

    def extract(self, file_path: Path) -> ExtractionResult:
        try:
            text = ocr_image_file(file_path)
        except OSError as exc:
            return ExtractionResult(
                file_path=file_path, status="error", error_message=str(exc)
            )
        blocks = []
        if text:
            blocks.append(
                ContentBlock("ocr_page", 1, text, self.__class__.__name__, {})
            )
        return ExtractionResult(file_path=file_path, status="ok", blocks=blocks)


def register(register_extractor) -> None:
    """Plugin hook: register the image extractor for every image suffix."""
    extractor = ImageTextExtractor()
    for suffix in IMAGE_SUFFIXES:
        register_extractor(suffix, extractor)
