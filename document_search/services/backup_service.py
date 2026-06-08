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
        return {
            "path": str(dest),
            "filename": dest.name,
            "size_bytes": size,
            "created_at": datetime.now(tz=UTC).isoformat(),
        }
