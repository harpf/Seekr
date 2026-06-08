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

    # ── export / import ────────────────────────────────────────────────
    _EXPORT_TABLES = (
        "documents",
        "content_blocks",
        "user_tags",
        "document_tags",
        "principals",
        "user_groups",
        "document_acl",
    )

    def _dump_table(self, table: str) -> list[dict[str, Any]]:
        rows = self.store.conn.execute(f"SELECT * FROM {table}").fetchall()
        return [dict(r) for r in rows]

    def export_archive(self, out_path: Path | str) -> dict[str, Any]:
        """Write a portable zip: manifest.json + tables/<name>.json.

        Excludes `users` (password hashes) and `content_fts` (rebuilt on import).
        """
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        counts: dict[str, int] = {}
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
            for table in self._EXPORT_TABLES:
                data = self._dump_table(table)
                counts[table] = len(data)
                z.writestr(f"tables/{table}.json", json.dumps(data, default=str))
            manifest = {
                "format": "seekr-export",
                "version": 1,
                "created_at": datetime.now(tz=UTC).isoformat(),
                "counts": counts,
            }
            z.writestr("manifest.json", json.dumps(manifest, indent=2))
        return {"path": str(out_path), "counts": counts}

    def import_archive(self, archive_path: Path | str) -> dict[str, Any]:
        """Import documents + tags + ACLs from a seekr-export zip.

        Strategy (additive, idempotent):
          * principals matched by (type, external_id); inserted if absent.
          * documents matched by `path` (UNIQUE); inserted if absent.
          * Foreign keys are remapped from source IDs to target IDs via lookup
            on the natural keys above, so IDs need not align across databases.
          * content_blocks, user_tags, document_tags, document_acl are linked
            through the remapped document/principal IDs.
        """
        archive_path = Path(archive_path)
        with zipfile.ZipFile(archive_path) as z:
            manifest = json.loads(z.read("manifest.json"))
            if manifest.get("format") != "seekr-export":
                raise ValueError("Not a seekr-export archive")
            tables = {
                t: json.loads(z.read(f"tables/{t}.json"))
                for t in self._EXPORT_TABLES
            }

        conn = self.store.conn
        now = datetime.now(tz=UTC).isoformat()
        imported = {t: 0 for t in self._EXPORT_TABLES}

        # 1. principals: (type, external_id) -> target principal id
        principal_map: dict[int, int] = {}
        for p in tables["principals"]:
            existing = conn.execute(
                "SELECT id FROM principals WHERE type=? AND external_id=?",
                (p["type"], p["external_id"]),
            ).fetchone()
            if existing:
                principal_map[p["id"]] = existing["id"]
            else:
                cur = conn.execute(
                    "INSERT INTO principals(type, external_id, display_name, created_at) "
                    "VALUES(?,?,?,?)",
                    (p["type"], p["external_id"], p.get("display_name"),
                     p.get("created_at") or now),
                )
                principal_map[p["id"]] = cur.lastrowid
                imported["principals"] += 1

        # 2. documents: path -> target document id
        doc_map: dict[int, int] = {}
        doc_cols = (
            "path", "filename", "extension", "mime_type", "file_size",
            "modified_at", "created_at", "sha256", "indexed_at", "status",
            "error_message", "page_count", "slide_count", "word_count",
            "metadata_json", "owner_principal_id",
        )
        for d in tables["documents"]:
            existing = conn.execute(
                "SELECT id FROM documents WHERE path=?", (d["path"],)
            ).fetchone()
            if existing:
                doc_map[d["id"]] = existing["id"]
                continue
            owner = d.get("owner_principal_id")
            mapped_owner = principal_map.get(owner) if owner is not None else None
            placeholders = ",".join("?" * len(doc_cols))
            values = [d.get(c) for c in doc_cols]
            values[doc_cols.index("owner_principal_id")] = mapped_owner
            cur = conn.execute(
                f"INSERT INTO documents({','.join(doc_cols)}) VALUES({placeholders})",
                values,
            )
            doc_map[d["id"]] = cur.lastrowid
            imported["documents"] += 1

        # 3. content_blocks (+ FTS) for newly-imported docs only
        for b in tables["content_blocks"]:
            tgt_doc = doc_map.get(b["document_id"])
            if tgt_doc is None:
                continue
            already = conn.execute(
                "SELECT 1 FROM content_blocks WHERE document_id=? AND block_number=? "
                "AND block_type=?",
                (tgt_doc, b["block_number"], b["block_type"]),
            ).fetchone()
            if already:
                continue
            bcur = conn.execute(
                "INSERT INTO content_blocks(document_id, block_type, block_number, "
                "text, extractor, text_length, metadata_json) VALUES(?,?,?,?,?,?,?)",
                (tgt_doc, b["block_type"], b["block_number"], b["text"],
                 b["extractor"], b["text_length"], b.get("metadata_json")),
            )
            drow = conn.execute(
                "SELECT path, filename, extension FROM documents WHERE id=?",
                (tgt_doc,),
            ).fetchone()
            conn.execute(
                "INSERT INTO content_fts(document_id, block_id, path, filename, "
                "extension, block_type, block_number, text) VALUES(?,?,?,?,?,?,?,?)",
                (tgt_doc, bcur.lastrowid, drow["path"], drow["filename"],
                 drow["extension"], b["block_type"], str(b["block_number"]), b["text"]),
            )
            imported["content_blocks"] += 1

        # 4. user_tags: (user_id, name) — user_id is NOT remapped (users excluded).
        #    We match the importing DB's existing users by id where present.
        tag_map: dict[int, int] = {}
        for t in tables["user_tags"]:
            existing = conn.execute(
                "SELECT id FROM user_tags WHERE user_id=? AND name=?",
                (t["user_id"], t["name"]),
            ).fetchone()
            if existing:
                tag_map[t["id"]] = existing["id"]
                continue
            user_exists = conn.execute(
                "SELECT 1 FROM users WHERE id=?", (t["user_id"],)
            ).fetchone()
            if not user_exists:
                continue  # skip tags whose owning user wasn't imported
            cur = conn.execute(
                "INSERT INTO user_tags(user_id, name) VALUES(?,?)",
                (t["user_id"], t["name"]),
            )
            tag_map[t["id"]] = cur.lastrowid
            imported["user_tags"] += 1

        # 5. document_tags
        for dt in tables["document_tags"]:
            tgt_doc = doc_map.get(dt["document_id"])
            tgt_tag = tag_map.get(dt["tag_id"])
            if tgt_doc is None or tgt_tag is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO document_tags(user_id, document_id, tag_id, "
                "created_at) VALUES(?,?,?,?)",
                (dt["user_id"], tgt_doc, tgt_tag, dt.get("created_at") or now),
            )
            imported["document_tags"] += 1

        # 6. user_groups (membership) — remap principal, keep user_id if present
        for ug in tables["user_groups"]:
            tgt_principal = principal_map.get(ug["principal_id"])
            if tgt_principal is None:
                continue
            user_exists = conn.execute(
                "SELECT 1 FROM users WHERE id=?", (ug["user_id"],)
            ).fetchone()
            if not user_exists:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO user_groups(user_id, principal_id) VALUES(?,?)",
                (ug["user_id"], tgt_principal),
            )
            imported["user_groups"] += 1

        # 7. document_acl — remap both document and principal
        for a in tables["document_acl"]:
            tgt_doc = doc_map.get(a["document_id"])
            tgt_principal = principal_map.get(a["principal_id"])
            if tgt_doc is None or tgt_principal is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO document_acl(document_id, principal_id, "
                "permission, granted_at) VALUES(?,?,?,?)",
                (tgt_doc, tgt_principal, a["permission"], a.get("granted_at") or now),
            )
            imported["document_acl"] += 1

        conn.commit()
        return {"imported": imported}
