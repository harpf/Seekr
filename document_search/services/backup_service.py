"""Backup, restore, and portable export/import for the Seekr SQLite store.

This module is the ONLY place that:
  * calls `sqlite3.Connection.backup()` (the WAL-safe online backup primitive), and
  * reads/writes the backup directory.

WAL note: the database runs in `journal_mode=WAL`. A plain file copy of
`document_index.db` can capture a torn or stale snapshot because committed
pages may still live in the `-wal` sidecar. `Connection.backup()` performs a
consistent page-by-page online copy through the live connection, so the
resulting file is a complete, single-file, integrity-clean database.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from document_search.index.sqlite_store import SqliteStore

_BACKUP_PREFIX = "document_index_"
_BACKUP_SUFFIX = ".db"
_TS_FMT = "%Y%m%d_%H%M%S"


class BackupService:
    def __init__(
        self,
        store: SqliteStore,
        backup_dir: Path | str | None = None,
        keep: int = 14,
    ) -> None:
        self.store = store
        self.db_path = Path(store.db_path)
        if backup_dir is None:
            backup_dir = os.getenv(
                "DOCUMENT_SEARCH_BACKUP_DIR",
                str(self.db_path.parent / "backups"),
            )
        self.backup_dir = Path(backup_dir)
        self.keep = keep

    # ── helpers ────────────────────────────────────────────────────────
    @staticmethod
    def _timestamp() -> str:
        return datetime.now(tz=UTC).strftime(_TS_FMT)

    def _new_backup_path(self, ts: str | None = None) -> Path:
        ts = ts or self._timestamp()
        return self.backup_dir / f"{_BACKUP_PREFIX}{ts}{_BACKUP_SUFFIX}"

    # ── create ─────────────────────────────────────────────────────────
    def create_backup(self) -> dict[str, Any]:
        """Write a WAL-safe online backup to a timestamped file.

        Returns a dict: {"path", "filename", "size_bytes", "created_at"}.
        """
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        dest = self._new_backup_path()
        # If two backups land in the same second, disambiguate.
        n = 1
        while dest.exists():
            dest = self.backup_dir / f"{_BACKUP_PREFIX}{self._timestamp()}_{n}{_BACKUP_SUFFIX}"
            n += 1

        dest_conn = sqlite3.connect(dest)
        try:
            # backup() reads from the live source connection (incl. WAL pages)
            # and writes a consistent snapshot into dest_conn.
            self.store.conn.backup(dest_conn)
        finally:
            dest_conn.close()

        size = dest.stat().st_size
        self.prune()
        return {
            "path": str(dest),
            "filename": dest.name,
            "size_bytes": size,
            "created_at": datetime.now(tz=UTC).isoformat(),
        }

    # ── list / prune ───────────────────────────────────────────────────
    def list_backups(self) -> list[dict[str, Any]]:
        if not self.backup_dir.exists():
            return []
        out: list[dict[str, Any]] = []
        for p in self.backup_dir.iterdir():
            if not p.is_file():
                continue
            if not (p.name.startswith(_BACKUP_PREFIX) and p.name.endswith(_BACKUP_SUFFIX)):
                continue
            st = p.stat()
            out.append(
                {
                    "filename": p.name,
                    "path": str(p),
                    "size_bytes": st.st_size,
                    "created_at": datetime.fromtimestamp(st.st_mtime, tz=UTC).isoformat(),
                }
            )
        # Filename embeds the timestamp, so a reverse string sort is newest-first.
        out.sort(key=lambda r: r["filename"], reverse=True)
        return out

    def prune(self) -> list[str]:
        """Delete all but the newest `self.keep` backups. Returns deleted names."""
        if self.keep <= 0:
            return []
        backups = self.list_backups()
        deleted: list[str] = []
        for r in backups[self.keep :]:
            try:
                Path(r["path"]).unlink()
                deleted.append(r["filename"])
            except OSError:
                pass
        return deleted

    # ── restore ────────────────────────────────────────────────────────
    def _resolve_backup(self, filename: str) -> Path:
        """Resolve a backup filename safely (no path traversal)."""
        if "/" in filename or "\\" in filename or filename in ("", ".", ".."):
            raise ValueError("Invalid backup filename")
        if not (filename.startswith(_BACKUP_PREFIX) and filename.endswith(_BACKUP_SUFFIX)):
            raise ValueError("Not a backup file")
        candidate = (self.backup_dir / filename).resolve()
        if candidate.parent != self.backup_dir.resolve():
            raise ValueError("Backup escapes backup directory")
        if not candidate.exists():
            raise FileNotFoundError(f"Backup not found: {filename}")
        return candidate

    def restore_backup(self, filename: str) -> dict[str, Any]:
        """Restore the chosen backup over the live DB file.

        Safety contract:
          1. Take a *pre-restore* safety backup of the CURRENT database first.
          2. Validate the chosen backup opens cleanly (integrity_check).
          3. Copy it over `db_path`, removing stale -wal/-shm sidecars.
          4. Return {"restart_required": True}. The caller MUST restart the
             process; we never hot-swap the file under an open connection.
        """
        source = self._resolve_backup(filename)

        # 1. Safety backup of the current state (best-effort; uses the online API).
        try:
            safety = self.create_backup()
        except Exception:
            safety = None

        # 2. Validate the chosen backup is a clean SQLite database.
        check_conn = sqlite3.connect(source)
        try:
            ok = check_conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            check_conn.close()
        if ok != "ok":
            raise ValueError(f"Backup failed integrity_check: {ok}")

        # 3. Swap the file. Remove WAL/SHM sidecars so the restored DB is used.
        #    Overwrite in place (r+b) rather than reopening the destination with
        #    a fresh handle: on Windows the live connection memory-maps the DB
        #    file (PRAGMA mmap_size), so `shutil.copyfile`/`os.replace` fail with
        #    a sharing/EINVAL error while that handle is open. Writing through the
        #    existing inode works cross-platform; the stale live connection is
        #    abandoned anyway (restart_required=True).
        data = source.read_bytes()
        if self.db_path.exists():
            with open(self.db_path, "r+b") as fdst:
                fdst.seek(0)
                fdst.write(data)
                fdst.truncate()
        else:
            shutil.copyfile(source, self.db_path)
        for sidecar in (
            self.db_path.with_name(self.db_path.name + "-wal"),
            self.db_path.with_name(self.db_path.name + "-shm"),
        ):
            try:
                sidecar.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                # On Windows the live connection may still hold the sidecar open
                # (sharing violation). Harmless: the required restart abandons
                # that connection and SQLite discards stale -wal/-shm on open.
                pass

        return {
            "restart_required": True,
            "restored_from": source.name,
            "safety_backup": safety["filename"] if safety else None,
        }
