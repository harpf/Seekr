from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ContentBlock:
    block_type: str
    block_number: int
    text: str
    extractor: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExtractionResult:
    file_path: Path
    status: str
    document_metadata: dict[str, Any] = field(default_factory=dict)
    blocks: list[ContentBlock] = field(default_factory=list)
    error_message: str | None = None


@dataclass(slots=True)
class FileFingerprint:
    path: Path
    file_size: int
    modified_at: datetime
    sha256: str
