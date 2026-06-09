from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader

from document_search.extractors.base import TextExtractor
from document_search.models import ContentBlock, ExtractionResult
from document_search.services.ocr_service import (
    force_ocr_enabled,
    ocr_enabled,
    ocr_image_bytes,
    ocr_pdf_pages,
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

            page_info: list[tuple[int, str, object]] = []
            for i, page in enumerate(reader.pages, start=1):
                text = (page.extract_text() or "").strip()
                page_info.append((i, text, page))
                if text:
                    blocks.append(ContentBlock("page", i, text, self.__class__.__name__, {"page": i}))
                else:
                    empty_pages += 1

            if do_ocr:
                # Full-page OCR for any page without native text (per-page
                # fallback), or for EVERY page when force_ocr is set. All such
                # pages are rasterised+OCR'd in one batched, optionally parallel
                # call instead of re-opening the PDF per page.
                pages_to_ocr = [i for (i, text, _p) in page_info if force or not text]
                ocr_texts = ocr_pdf_pages(file_path, pages_to_ocr, languages=lang)
                for i in sorted(ocr_texts):
                    blocks.append(
                        ContentBlock(
                            "ocr_page", i, ocr_texts[i], self.__class__.__name__,
                            {"page": i, "source": "ocr"},
                        )
                    )
                if not force:
                    # On text pages (already captured natively), logos/stamps/
                    # figures rendered as images are invisible to pypdf — OCR each
                    # embedded image to catch them. Skipped under force_ocr, where
                    # the full-page raster already contains them.
                    for i, text, page in page_info:
                        if not text:
                            continue
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
