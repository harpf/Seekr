"""Watches scan inboxes for stable files, claims them into staging, and enqueues
scan_ingest jobs. Polling is the robust default (SMB-friendly); watchdog events
are an optional low-latency accelerator layered on top (Task D2)."""
from __future__ import annotations

import logging
import shutil
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from document_search.services.scan_inbox_config import ScanInbox

log = logging.getLogger(__name__)

# Only files with these suffixes are picked up from an inbox.
SCAN_SUFFIXES = (".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff")


def staging_dir_for(data_dir: Path, inbox_id: str) -> Path:
    """Canonical staging path: <data_dir>/scan-staging/<inbox_id>/pending-review."""
    return Path(data_dir) / "scan-staging" / inbox_id / "pending-review"


def is_stable(path: Path, *, stability_seconds: int, now: float | None = None) -> bool:
    """A file is stable when it has not been modified for `stability_seconds`."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    current = time.time() if now is None else now
    return (current - mtime) >= stability_seconds


def _claim_destination(staging: Path, name: str) -> Path:
    """Non-colliding destination in staging. Safe for the single-process polling loop;
    a uuid suffix avoids overwriting a pre-existing staged file."""
    dest = staging / name
    if not dest.exists():
        return dest
    stem, suffix = Path(name).stem, Path(name).suffix
    return staging / f"{stem}_{uuid.uuid4().hex[:8]}{suffix}"


def scan_once(
    inbox: ScanInbox,
    *,
    data_dir: Path,
    enqueue: Callable[[str, str, str], None],
    now: float | None = None,
) -> int:
    """Scan one inbox once: move every stable, supported file into staging and
    enqueue it. Returns the number of files claimed. The move IS the claim — atomic
    on the same filesystem, so only one pass wins a given file."""
    inbox_path = Path(inbox.inbox_path)
    if not inbox_path.is_dir():
        log.warning("Scan inbox %s path missing: %s", inbox.id, inbox.inbox_path)
        return 0
    staging = staging_dir_for(data_dir, inbox.id)
    staging.mkdir(parents=True, exist_ok=True)
    claimed = 0
    for entry in sorted(inbox_path.iterdir()):
        if not entry.is_file() or entry.suffix.lower() not in SCAN_SUFFIXES:
            continue
        if not is_stable(entry, stability_seconds=inbox.stability_seconds, now=now):
            continue
        dest = _claim_destination(staging, entry.name)
        try:
            shutil.move(str(entry), str(dest))
        except OSError:
            log.warning("Failed to claim scan file %s", entry, exc_info=True)
            continue
        try:
            enqueue(inbox.id, str(dest), entry.name)
        except Exception:
            log.exception("Failed to enqueue scan_ingest for %s", dest)
        claimed += 1  # intentional: file was already moved; D2 recovery re-enqueues orphaned staging files
    return claimed
