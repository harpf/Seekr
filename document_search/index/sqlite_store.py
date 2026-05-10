from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from document_search.auth import UserRecord, hash_password, new_salt
from document_search.models import ExtractionResult, FileFingerprint


class SqliteStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS documents (
              id INTEGER PRIMARY KEY,
              path TEXT UNIQUE NOT NULL,
              filename TEXT NOT NULL,
              extension TEXT NOT NULL,
              mime_type TEXT,
              file_size INTEGER NOT NULL,
              modified_at TEXT NOT NULL,
              created_at TEXT,
              sha256 TEXT NOT NULL,
              indexed_at TEXT NOT NULL,
              status TEXT NOT NULL,
              error_message TEXT,
              page_count INTEGER,
              slide_count INTEGER,
              word_count INTEGER,
              metadata_json TEXT
            );
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY,
              username TEXT UNIQUE NOT NULL,
              password_hash TEXT NOT NULL,
              salt TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_document_marks (
              user_id INTEGER NOT NULL,
              document_id INTEGER NOT NULL,
              is_marked INTEGER NOT NULL DEFAULT 1,
              updated_at TEXT NOT NULL,
              PRIMARY KEY (user_id, document_id),
              FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
              FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS user_tags (
              id INTEGER PRIMARY KEY,
              user_id INTEGER NOT NULL,
              name TEXT NOT NULL,
              UNIQUE(user_id, name),
              FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS document_tags (
              user_id INTEGER NOT NULL,
              document_id INTEGER NOT NULL,
              tag_id INTEGER NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY (user_id, document_id, tag_id),
              FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
              FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE,
              FOREIGN KEY (tag_id) REFERENCES user_tags(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS content_blocks (
              id INTEGER PRIMARY KEY,
              document_id INTEGER NOT NULL,
              block_type TEXT NOT NULL,
              block_number INTEGER NOT NULL,
              text TEXT NOT NULL,
              extractor TEXT NOT NULL,
              text_length INTEGER NOT NULL,
              metadata_json TEXT,
              FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS content_fts USING fts5(
              document_id UNINDEXED,
              block_id UNINDEXED,
              path,
              filename,
              extension,
              block_type,
              block_number,
              text
            );
            """
        )

    def get_document(self, path: str):
        return self.conn.execute("SELECT * FROM documents WHERE path = ?", (path,)).fetchone()

    def get_document_by_id(self, document_id: int):
        return self.conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()

    def upsert_document(self, fp: FileFingerprint, ext: ExtractionResult) -> int:
        now = datetime.now(tz=UTC).isoformat()
        meta = ext.document_metadata
        self.conn.execute("DELETE FROM content_blocks WHERE document_id IN (SELECT id FROM documents WHERE path = ?)", (str(fp.path),))
        self.conn.execute("DELETE FROM content_fts WHERE path = ?", (str(fp.path),))
        self.conn.execute(
            """
            INSERT INTO documents(path, filename, extension, file_size, modified_at, sha256, indexed_at, status, error_message, page_count, slide_count, metadata_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
              filename=excluded.filename, extension=excluded.extension, file_size=excluded.file_size, modified_at=excluded.modified_at,
              sha256=excluded.sha256, indexed_at=excluded.indexed_at, status=excluded.status, error_message=excluded.error_message,
              page_count=excluded.page_count, slide_count=excluded.slide_count, metadata_json=excluded.metadata_json
            """,
            (
                str(fp.path), fp.path.name, fp.path.suffix.lower(), fp.file_size, fp.modified_at.isoformat(), fp.sha256, now,
                ext.status, ext.error_message, meta.get("page_count"), meta.get("slide_count"), str(meta),
            ),
        )
        doc_id = self.conn.execute("SELECT id FROM documents WHERE path = ?", (str(fp.path),)).fetchone()[0]
        for block in ext.blocks:
            cursor = self.conn.execute(
                "INSERT INTO content_blocks(document_id, block_type, block_number, text, extractor, text_length, metadata_json) VALUES(?,?,?,?,?,?,?)",
                (doc_id, block.block_type, block.block_number, block.text, block.extractor, len(block.text), str(block.metadata)),
            )
            block_id = cursor.lastrowid
            self.conn.execute(
                "INSERT INTO content_fts(document_id, block_id, path, filename, extension, block_type, block_number, text) VALUES(?,?,?,?,?,?,?,?)",
                (doc_id, block_id, str(fp.path), fp.path.name, fp.path.suffix.lower(), block.block_type, str(block.block_number), block.text),
            )
        self.conn.commit()
        return doc_id

    def ensure_default_admin(self) -> None:
        row = self.conn.execute("SELECT id FROM users LIMIT 1").fetchone()
        if row:
            return
        salt = new_salt()
        pwd = os.getenv("DOCUMENT_SEARCH_DEFAULT_PASSWORD", "admin")
        self.conn.execute(
            "INSERT INTO users(username,password_hash,salt,created_at) VALUES(?,?,?,?)",
            ("admin", hash_password(pwd, salt), salt, datetime.now(tz=UTC).isoformat()),
        )
        self.conn.commit()

    def get_user(self, username: str):
        row = self.conn.execute("SELECT id,username,password_hash,salt FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            return None
        return row

    def set_mark(self, user_id: int, document_id: int, is_marked: bool) -> None:
        self.conn.execute(
            """
            INSERT INTO user_document_marks(user_id, document_id, is_marked, updated_at)
            VALUES(?,?,?,?)
            ON CONFLICT(user_id, document_id) DO UPDATE SET is_marked=excluded.is_marked, updated_at=excluded.updated_at
            """,
            (user_id, document_id, 1 if is_marked else 0, datetime.now(tz=UTC).isoformat()),
        )
        self.conn.commit()

    def set_tags(self, user_id: int, document_id: int, tags: list[str]) -> None:
        cleaned = sorted({t.strip().lower() for t in tags if t.strip()})
        self.conn.execute("DELETE FROM document_tags WHERE user_id=? AND document_id=?", (user_id, document_id))
        for tag in cleaned:
            self.conn.execute(
                "INSERT INTO user_tags(user_id, name) VALUES(?,?) ON CONFLICT(user_id, name) DO NOTHING",
                (user_id, tag),
            )
            tag_id = self.conn.execute("SELECT id FROM user_tags WHERE user_id=? AND name=?", (user_id, tag)).fetchone()[0]
            self.conn.execute(
                "INSERT INTO document_tags(user_id, document_id, tag_id, created_at) VALUES(?,?,?,?)",
                (user_id, document_id, tag_id, datetime.now(tz=UTC).isoformat()),
            )
        self.conn.commit()

    def get_doc_marks_and_tags(self, user_id: int, document_ids: list[int]) -> dict[int, dict[str, object]]:
        if not document_ids:
            return {}
        placeholders = ",".join("?" for _ in document_ids)
        marks_rows = self.conn.execute(
            f"SELECT document_id, is_marked FROM user_document_marks WHERE user_id=? AND document_id IN ({placeholders})",
            (user_id, *document_ids),
        ).fetchall()
        tag_rows = self.conn.execute(
            f"""
            SELECT dt.document_id, ut.name
            FROM document_tags dt JOIN user_tags ut ON ut.id=dt.tag_id
            WHERE dt.user_id=? AND dt.document_id IN ({placeholders})
            """,
            (user_id, *document_ids),
        ).fetchall()
        out = {d: {"is_marked": False, "tags": []} for d in document_ids}
        for row in marks_rows:
            out[row["document_id"]]["is_marked"] = bool(row["is_marked"])
        for row in tag_rows:
            out[row["document_id"]]["tags"].append(row["name"])
        return out

    def remove_missing(self) -> int:
        rows = self.conn.execute("SELECT id,path FROM documents").fetchall()
        removed = 0
        for row in rows:
            if not Path(row["path"]).exists():
                self.conn.execute("DELETE FROM content_blocks WHERE document_id = ?", (row["id"],))
                self.conn.execute("DELETE FROM content_fts WHERE document_id = ?", (row["id"],))
                self.conn.execute("DELETE FROM documents WHERE id = ?", (row["id"],))
                removed += 1
        self.conn.commit()
        return removed
