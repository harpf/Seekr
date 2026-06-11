"""Watches scan inboxes for stable files, claims them into staging, and enqueues
scan_ingest jobs. Polling is the robust default (SMB-friendly); watchdog events
are an optional low-latency accelerator layered on top (Task D2)."""
from __future__ import annotations

import logging
import shutil
import threading
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


class ScanWatcherManager:
    """Owns one polling thread per enabled inbox; optionally augments each with a
    watchdog observer for low-latency event triggers. Reconfigure starts/stops
    threads live without an app restart."""

    def __init__(self, *, data_dir: Path,
                 enqueue: Callable[[str, str, str], None]) -> None:
        self._data_dir = Path(data_dir)
        self._enqueue = enqueue
        self._inboxes: dict[str, ScanInbox] = {}
        self._stops: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._observers: dict[str, object] = {}
        self._lock = threading.Lock()

    def active_inbox_ids(self) -> set[str]:
        with self._lock:
            return set(self._inboxes)

    def _poll_loop(self, inbox_id: str, stop: threading.Event) -> None:
        while not stop.is_set():
            with self._lock:
                inbox = self._inboxes.get(inbox_id)
            if inbox is None:
                return
            try:
                scan_once(inbox, data_dir=self._data_dir, enqueue=self._enqueue)
            except Exception:
                log.exception("scan_once failed for inbox %s", inbox_id)
            stop.wait(inbox.poll_interval_seconds)

    def _start_inbox(self, inbox: ScanInbox) -> None:
        stop = threading.Event()
        self._inboxes[inbox.id] = inbox
        self._stops[inbox.id] = stop
        t = threading.Thread(target=self._poll_loop, args=(inbox.id, stop),
                             name=f"scan-watch-{inbox.id}", daemon=True)
        self._threads[inbox.id] = t
        t.start()
        self._start_observer(inbox)

    def _start_observer(self, inbox: ScanInbox) -> None:
        """Optional watchdog accelerator. Absent dependency -> polling-only."""
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except Exception:
            return

        manager = self

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                with manager._lock:
                    ib = manager._inboxes.get(inbox.id)
                if ib is None:
                    return
                try:
                    scan_once(ib, data_dir=manager._data_dir, enqueue=manager._enqueue)
                except Exception:
                    log.exception("event-triggered scan_once failed for %s", inbox.id)

        try:
            obs = Observer()
            obs.schedule(_Handler(), inbox.inbox_path, recursive=False)
            obs.daemon = True
            obs.start()
            self._observers[inbox.id] = obs
        except Exception:
            log.warning("watchdog observer failed for %s; polling only", inbox.id, exc_info=True)

    def _stop_inbox(self, inbox_id: str) -> None:
        """Signal stop and collect handles; joining is done OUTSIDE the lock to
        avoid a deadlock where _poll_loop tries to acquire self._lock while the
        caller already holds it and is waiting on t.join()."""
        stop = self._stops.pop(inbox_id, None)
        if stop is not None:
            stop.set()
        t = self._threads.pop(inbox_id, None)
        obs = self._observers.pop(inbox_id, None)
        self._inboxes.pop(inbox_id, None)
        return t, obs  # type: ignore[return-value]

    def _join_handles(self, t: threading.Thread | None, obs: object | None,
                      inbox_id: str) -> None:
        """Join thread and observer OUTSIDE any lock."""
        if t is not None:
            t.join(timeout=2.0)
        if obs is not None:
            try:
                obs.stop()  # type: ignore[attr-defined]
                obs.join(timeout=2.0)  # type: ignore[attr-defined]
            except Exception:
                log.warning("Failed to stop observer for %s", inbox_id, exc_info=True)

    def reconfigure(self, inboxes: list[ScanInbox]) -> None:
        """Diff the desired enabled set against the running set; start/stop deltas."""
        desired = {ib.id: ib for ib in inboxes if ib.enabled}
        handles_to_join: list[tuple[str, threading.Thread | None, object | None]] = []
        with self._lock:
            running = set(self._inboxes)
            wanted = set(desired)
            for inbox_id in running - wanted:
                t, obs = self._stop_inbox(inbox_id)
                handles_to_join.append((inbox_id, t, obs))
            for inbox_id in wanted - running:
                self._start_inbox(desired[inbox_id])
            for inbox_id in wanted & running:
                self._inboxes[inbox_id] = desired[inbox_id]
        # Join outside the lock to avoid deadlock with _poll_loop's brief lock acquisition.
        for inbox_id, t, obs in handles_to_join:
            self._join_handles(t, obs, inbox_id)

    def recover_orphans(self, inboxes: list[ScanInbox], *,
                        known_staging_paths: set[str]) -> None:
        """Re-enqueue staged files that have no scan_review row (crash recovery)."""
        for inbox in inboxes:
            staging = staging_dir_for(self._data_dir, inbox.id)
            if not staging.is_dir():
                continue
            for f in sorted(staging.iterdir()):
                if f.is_file() and str(f) not in known_staging_paths:
                    try:
                        self._enqueue(inbox.id, str(f), f.name)
                    except Exception:
                        log.exception("Failed to recover orphan scan %s", f)

    def stop_all(self, timeout: float = 5.0) -> None:
        handles_to_join: list[tuple[str, threading.Thread | None, object | None]] = []
        with self._lock:
            for inbox_id in list(self._inboxes):
                t, obs = self._stop_inbox(inbox_id)
                handles_to_join.append((inbox_id, t, obs))
        for inbox_id, t, obs in handles_to_join:
            self._join_handles(t, obs, inbox_id)
