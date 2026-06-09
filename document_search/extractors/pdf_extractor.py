from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader

from document_search.extractors.base import TextExtractor
from document_search.models import ContentBlock, ExtractionResult
from document_search.services.ocr_service import (
    force_ocr_enabled,
    ocr_enabled,
    ocr_image_bytes,
    ocr_pdf_page,
    resolve_ocr_language,
)


def _page_images(page) -> list[bytes]:
    """Raw bytes of images embedded on a pypdf page; tolerant of pypdf variants."""
    try:
        images = page.images
    except Exception:
        return []
    out: list[bytes] = []
    for img in images or []:
        data = getattr(img, "data", None)
        if data:
            out.append(data)
    return out


class PdfTextExtractor(TextExtractor):
    def extract(self, file_path: Path) -> ExtractionResult:
        try:
            reader = PdfReader(str(file_path))
            blocks: list[ContentBlock] = []
            empty_pages = 0

            do_ocr = ocr_enabled()
            force = force_ocr_enabled()
            lang = resolve_ocr_language()

            for i, page in enumerate(reader.pages, start=1):
                text = (page.extract_text() or "").strip()
                if text:
                    blocks.append(ContentBlock("page", i, text, self.__class__.__name__, {"page": i}))
                else:
                    empty_pages += 1

                if not do_ocr:
                    continue

                # Full-page OCR for any page without native text (per-page
                # fallback), or for EVERY page when force_ocr is set.
                if force or not text:
                    ocr_text = ocr_pdf_page(file_path, i, languages=lang)
                    if ocr_text:
                        blocks.append(
                            ContentBlock(
                                "ocr_page", i, ocr_text, self.__class__.__name__,
                                {"page": i, "source": "ocr"},
                            )
                        )
                else:
                    # Text page (and not force-OCR): the page text is already
                    # captured, but logos/stamps/figures rendered as images are
                    # invisible to pypdf — OCR each embedded image to catch them.
                    for j, img in enumerate(_page_images(page), start=1):
                        img_text = ocr_image_bytes(img, languages=lang)
                        if img_text:
                            blocks.append(
                                ContentBlock(
                                    "ocr_image", i, img_text, self.__class__.__name__,
                                    {"page": i, "image": j, "source": "ocr"},
                                )
                            )

            metadata = {
                "page_count": len(reader.pages),
                "pdf_metadata": {k: str(v) for k, v in (reader.metadata or {}).items()},
                "likely_scanned": empty_pages == len(reader.pages) and len(reader.pages) > 0,
            }
            return ExtractionResult(file_path=file_path, status="ok", document_metadata=metadata, blocks=blocks)
        except Exception as ex:
            return ExtractionResult(file_path=file_path, status="error", error_message=str(ex))
