from __future__ import annotations

import subprocess
from pathlib import Path

from document_search.extractors.base import TextExtractor
from document_search.models import ContentBlock, ExtractionResult


class LegacyOfficeTextExtractor(TextExtractor):
    def extract(self, file_path: Path) -> ExtractionResult:
        cmd = ["antiword", str(file_path)] if file_path.suffix.lower() == ".doc" else ["catppt", str(file_path)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
            if proc.returncode != 0:
                return ExtractionResult(file_path=file_path, status="error", error_message=proc.stderr.strip() or "legacy extraction failed")
            text = (proc.stdout or "").strip()
            blocks = [ContentBlock("legacy_document", 1, text, self.__class__.__name__, {})] if text else []
            return ExtractionResult(file_path=file_path, status="ok", blocks=blocks)
        except FileNotFoundError:
            return ExtractionResult(file_path=file_path, status="error", error_message="antiword/catppt not installed")
        except Exception as ex:
            return ExtractionResult(file_path=file_path, status="error", error_message=str(ex))
