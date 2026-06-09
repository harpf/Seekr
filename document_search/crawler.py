from __future__ import annotations

import os
from collections.abc import Iterator
from fnmatch import fnmatch
from pathlib import Path

from document_search.config import AppConfig
from document_search.extractors import supported_extensions


def _is_hidden(path: Path) -> bool:
    return any(part.startswith(".") for part in path.parts)


def iter_documents(roots: list[Path], config: AppConfig) -> Iterator[Path]:
    # A file is crawled only if its suffix is both enabled by the operator
    # (config.supported_extensions) and backed by a registered extractor
    # (built-in or plugin). The registry — not a hard-coded map — is the source
    # of truth for "can we read this", so plugin file types flow through here.
    exts = {e.lower() for e in config.supported_extensions} & supported_extensions()
    for root in roots:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=config.follow_symlinks):
            dirnames[:] = [d for d in dirnames if d not in config.exclude_dirs and (not config.ignore_hidden or not d.startswith("."))]
            base = Path(dirpath)
            for name in filenames:
                path = base / name
                try:
                    if config.ignore_hidden and _is_hidden(path):
                        continue
                    if any(fnmatch(path.name, pat) for pat in config.exclude_patterns):
                        continue
                    if config.ignore_temp_office_files and path.name.startswith("~$"):
                        continue
                    suffix = path.suffix.lower()
                    if suffix not in exts:
                        continue
                    if path.stat().st_size > config.max_file_size_mb * 1024 * 1024:
                        continue
                    yield path.resolve()
                except (OSError, PermissionError):
                    continue
