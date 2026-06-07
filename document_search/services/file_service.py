from __future__ import annotations

import re
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from document_search.models import FileFingerprint
from document_search.services.hash_service import sha256_file

_WS_RE = re.compile(r"\s+")
_CONTENT_HASH_PREFIX_BYTES = 8192


def fingerprint(path: Path) -> FileFingerprint:
    stat = path.stat()
    return FileFingerprint(
        path=path.resolve(),
        file_size=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        sha256=sha256_file(path),
    )


def normalized_content_hash(text: str) -> str | None:
    """Embedding-free near-duplicate fingerprint.

    Lowercases the text, collapses every run of whitespace to a single space,
    strips, truncates to the first 8 KB, and returns its sha256 hex digest.
    Two documents whose textual content matches after this normalization share
    a hash even when their raw bytes (and therefore file `sha256`) differ —
    e.g. the same report re-exported by a different PDF producer.

    Returns None when the normalized text is empty (no meaningful content to
    fingerprint), so such documents are excluded from content-duplicate groups.
    """
    if not text:
        return None
    normalized = _WS_RE.sub(" ", text).strip().lower()
    if not normalized:
        return None
    truncated = normalized[:_CONTENT_HASH_PREFIX_BYTES]
    return sha256(truncated.encode("utf-8")).hexdigest()
