"""Central logging configuration for the document_search package.

Call `configure_logging()` once at process entry (FastAPI `create_app`, CLI
`main`). It is idempotent: safe to call multiple times and a no-op for handler
creation if the root logger is already configured (e.g. under pytest or uvicorn).
"""
from __future__ import annotations

import logging
import os

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_DEFAULT_LEVEL = "INFO"


def _resolve_level() -> tuple[int, str | None]:
    """Return (numeric_level, bad_value_or_None).

    Reads DOCUMENT_SEARCH_LOG_LEVEL. Unknown values fall back to INFO and the
    offending string is returned so the caller can warn about it.
    """
    raw = os.getenv("DOCUMENT_SEARCH_LOG_LEVEL", _DEFAULT_LEVEL).strip().upper()
    level = logging.getLevelName(raw)
    if isinstance(level, int):
        return level, None
    return logging.INFO, raw


def configure_logging() -> None:
    """Configure the root logger once, honouring DOCUMENT_SEARCH_LOG_LEVEL.

    - Default level INFO.
    - Invalid level values fall back to INFO and emit a single warning.
    - `basicConfig` only installs a handler if none exists, so we additionally
      force the root level so the env var still applies under pre-existing
      handlers (pytest, uvicorn, the CLI's own basicConfig).
    """
    level, bad_value = _resolve_level()
    logging.basicConfig(level=level, format=_LOG_FORMAT)
    logging.getLogger().setLevel(level)
    if bad_value is not None:
        logging.getLogger(__name__).warning(
            "Invalid DOCUMENT_SEARCH_LOG_LEVEL=%r; falling back to INFO", bad_value
        )
