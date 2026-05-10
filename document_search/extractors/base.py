from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from document_search.models import ExtractionResult


class TextExtractor(ABC):
    @abstractmethod
    def extract(self, file_path: Path) -> ExtractionResult:
        raise NotImplementedError
