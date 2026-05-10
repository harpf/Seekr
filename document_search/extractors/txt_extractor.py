from __future__ import annotations

from pathlib import Path

from document_search.extractors.base import TextExtractor
from document_search.models import ContentBlock, ExtractionResult


class TxtTextExtractor(TextExtractor):
    def extract(self, file_path: Path) -> ExtractionResult:
        text = file_path.read_text(encoding="utf-8", errors="ignore").strip()
        blocks = []
        if text:
            blocks.append(ContentBlock("text_file", 1, text, self.__class__.__name__, {}))
        return ExtractionResult(file_path=file_path, status="ok", blocks=blocks)
