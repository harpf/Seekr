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
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._configure_connection()
        self._init_schema()

    def _configure_connection(self) -> None:
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=NORMAL")   # safe with WAL, faster than FULL
        self.conn.execute("PRAGMA cache_size=-32000")    # 32 MB page cache
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self.conn.execute("PRAGMA mmap_size=268435456")  # 256 MB memory-mapped I/O

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
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
            CREATE INDEX IF NOT EXISTS idx_docs_modified_at  ON documents(modified_at);
            CREATE INDEX IF NOT EXISTS idx_docs_sha256       ON documents(sha256);
            CREATE INDEX IF NOT EXISTS idx_blocks_doc_id     ON content_blocks(document_id);
            CREATE INDEX IF NOT EXISTS idx_doc_tags_doc_id   ON document_tags(document_id);
            CREATE INDEX IF NOT EXISTS idx_doc_tags_tag_id   ON document_tags(tag_id);
            CREATE INDEX IF NOT EXISTS idx_user_tags_user_id ON user_tags(user_id);
            CREATE INDEX IF NOT EXISTS idx_marks_doc_id      ON user_document_marks(document_id);
            """
        )
        # Migration: add role column for existing databases
        try:
            self.conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
            self.conn.commit()
        except Exception:
            pass
        # Ensure at least one admin exists after migration
        try:
            admin_count = self.conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
            if admin_count == 0:
                self.conn.execute("UPDATE users SET role='admin' WHERE id=(SELECT MIN(id) FROM users)")
                self.conn.commit()
        except Exception:
            pass

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
        path_str = str(fp.path)
        name_str = fp.path.name
        ext_str = fp.path.suffix.lower()
        fts_rows = []
        for block in ext.blocks:
            cursor = self.conn.execute(
                "INSERT INTO content_blocks(document_id, block_type, block_number, text, extractor, text_length, metadata_json) VALUES(?,?,?,?,?,?,?)",
                (doc_id, block.block_type, block.block_number, block.text, block.extractor, len(block.text), str(block.metadata)),
            )
            fts_rows.append((doc_id, cursor.lastrowid, path_str, name_str, ext_str, block.block_type, str(block.block_number), block.text))
        if fts_rows:
            self.conn.executemany(
                "INSERT INTO content_fts(document_id, block_id, path, filename, extension, block_type, block_number, text) VALUES(?,?,?,?,?,?,?,?)",
                fts_rows,
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
            "INSERT INTO users(username,password_hash,salt,created_at,role) VALUES(?,?,?,?,?)",
            ("admin", hash_password(pwd, salt), salt, datetime.now(tz=UTC).isoformat(), "admin"),
        )
        self.conn.commit()

    def get_user(self, username: str):
        row = self.conn.execute(
            "SELECT id, username, password_hash, salt, role FROM users WHERE username=?", (username,)
        ).fetchone()
        if not row:
            return None
        return row

    def get_user_by_id(self, user_id: int):
        return self.conn.execute(
            "SELECT id, username, role, created_at FROM users WHERE id=?", (user_id,)
        ).fetchone()

    def list_users(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, username, role, created_at FROM users ORDER BY id"
        ).fetchall()
        return [dict(row) for row in rows]

    def create_user(self, username: str, password: str, role: str = "user") -> int:
        salt = new_salt()
        now = datetime.now(tz=UTC).isoformat()
        cursor = self.conn.execute(
            "INSERT INTO users(username, password_hash, salt, created_at, role) VALUES(?,?,?,?,?)",
            (username, hash_password(password, salt), salt, now, role),
        )
        self.conn.commit()
        return cursor.lastrowid

    def update_user_role(self, user_id: int, role: str) -> None:
        self.conn.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
        self.conn.commit()

    def delete_user(self, user_id: int) -> None:
        self.conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        self.conn.commit()

    def change_password(self, user_id: int, new_password: str) -> None:
        salt = new_salt()
        self.conn.execute(
            "UPDATE users SET password_hash=?, salt=? WHERE id=?",
            (hash_password(new_password, salt), salt, user_id),
        )
        self.conn.commit()

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

    def move_document(self, document_id: int, new_path: str) -> None:
        new_p = Path(new_path)
        self.conn.execute("DELETE FROM content_fts WHERE document_id = ?", (document_id,))
        self.conn.execute(
            "UPDATE documents SET path=?, filename=? WHERE id=?",
            (new_path, new_p.name, document_id),
        )
        blocks = self.conn.execute(
            "SELECT id, block_type, block_number, text FROM content_blocks WHERE document_id=?",
            (document_id,),
        ).fetchall()
        for block in blocks:
            self.conn.execute(
                "INSERT INTO content_fts(document_id, block_id, path, filename, extension, block_type, block_number, text) VALUES(?,?,?,?,?,?,?,?)",
                (document_id, block["id"], new_path, new_p.name, new_p.suffix.lower(), block["block_type"], block["block_number"], block["text"]),
            )
        self.conn.commit()

    def get_user_tags(self, user_id: int) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT ut.name, COUNT(dt.document_id) AS doc_count
            FROM user_tags ut
            LEFT JOIN document_tags dt ON dt.tag_id = ut.id AND dt.user_id = ut.user_id
            WHERE ut.user_id = ?
            GROUP BY ut.id, ut.name
            ORDER BY doc_count DESC, ut.name
            """,
            (user_id,),
        ).fetchall()
        return [{"name": r["name"], "count": r["doc_count"]} for r in rows]

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
