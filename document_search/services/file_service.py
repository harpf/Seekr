from __future__ import annotations

from datetime import datetime, UTC
from pathlib import Path

from document_search.models import FileFingerprint
from document_search.services.hash_service import sha256_file


def fingerprint(path: Path) -> FileFingerprint:
    stat = path.stat()
    return FileFingerprint(
        path=path.resolve(),
        file_size=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        sha256=sha256_file(path),
    )
