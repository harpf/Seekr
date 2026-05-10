from __future__ import annotations

import os
from pathlib import Path

from pypdf import PdfReader

from document_search.extractors.base import TextExtractor
from document_search.models import ContentBlock, ExtractionResult
from document_search.services.ocr_service import ocr_pdf_file


class PdfTextExtractor(TextExtractor):
    def extract(self, file_path: Path) -> ExtractionResult:
        try:
            reader = PdfReader(str(file_path))
            blocks: list[ContentBlock] = []
            empty_pages = 0
            for i, page in enumerate(reader.pages, start=1):
                text = (page.extract_text() or "").strip()
                if text:
                    blocks.append(ContentBlock("page", i, text, self.__class__.__name__, {"page": i}))
                else:
                    empty_pages += 1
            if not blocks and os.getenv("DOCUMENT_SEARCH_OCR_ENABLED", "false").lower() == "true":
                for i, ocr_text in enumerate(ocr_pdf_file(file_path), start=1):
                    if ocr_text:
                        blocks.append(ContentBlock("ocr_page", i, ocr_text, self.__class__.__name__, {"page": i, "source": "ocr"}))
            metadata = {
                "page_count": len(reader.pages),
                "pdf_metadata": {k: str(v) for k, v in (reader.metadata or {}).items()},
                "likely_scanned": empty_pages == len(reader.pages) and len(reader.pages) > 0,
            }
            return ExtractionResult(file_path=file_path, status="ok", document_metadata=metadata, blocks=blocks)
        except Exception as ex:
            return ExtractionResult(file_path=file_path, status="error", error_message=str(ex))
