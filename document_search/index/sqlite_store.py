from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from document_search.auth import hash_password, new_salt
from document_search.models import ExtractionResult, FileFingerprint

HISTORY_CAP = 20


class SqliteStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._configure_connection()
        self._init_schema()
        self.embed_fn = None  # optional Callable[[str], list[float] | None]; set by app when semantic enabled
        self.embed_model = "unknown"

    def _configure_connection(self) -> None:
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=NORMAL")   # safe with WAL, faster than FULL
        self.conn.execute("PRAGMA cache_size=-32000")    # 32 MB page cache
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self.conn.execute("PRAGMA mmap_size=268435456")  # 256 MB memory-mapped I/O
        self.conn.execute("PRAGMA busy_timeout=5000")    # wait up to 5s on a locked db

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
            CREATE TABLE IF NOT EXISTS principals (
              id INTEGER PRIMARY KEY,
              type TEXT NOT NULL CHECK(type IN ('user','group')),
              external_id TEXT NOT NULL,
              display_name TEXT,
              created_at TEXT NOT NULL,
              UNIQUE(type, external_id)
            );
            CREATE TABLE IF NOT EXISTS user_groups (
              user_id INTEGER NOT NULL,
              principal_id INTEGER NOT NULL,
              PRIMARY KEY (user_id, principal_id),
              FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
              FOREIGN KEY (principal_id) REFERENCES principals(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS document_acl (
              document_id INTEGER NOT NULL,
              principal_id INTEGER NOT NULL,
              permission TEXT NOT NULL CHECK(permission IN ('read','write')),
              granted_at TEXT NOT NULL,
              PRIMARY KEY (document_id, principal_id, permission),
              FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE,
              FOREIGN KEY (principal_id) REFERENCES principals(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_acl_doc        ON document_acl(document_id);
            CREATE INDEX IF NOT EXISTS idx_acl_principal  ON document_acl(principal_id);
            CREATE INDEX IF NOT EXISTS idx_user_groups_pid ON user_groups(principal_id);
            CREATE TABLE IF NOT EXISTS jobs (
              id INTEGER PRIMARY KEY,
              kind TEXT NOT NULL,
              state TEXT NOT NULL CHECK(state IN ('pending','running','succeeded','failed','interrupted','cancelled')),
              payload_json TEXT NOT NULL,
              progress_json TEXT,
              result_json TEXT,
              error_message TEXT,
              retry_count INTEGER NOT NULL DEFAULT 0,
              max_retries INTEGER NOT NULL DEFAULT 0,
              cancel_requested INTEGER NOT NULL DEFAULT 0,
              owner_user_id INTEGER,
              created_at TEXT NOT NULL,
              started_at TEXT,
              finished_at TEXT,
              next_attempt_at TEXT,
              FOREIGN KEY (owner_user_id) REFERENCES users(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_state          ON jobs(state);
            CREATE INDEX IF NOT EXISTS idx_jobs_kind_state     ON jobs(kind, state);
            CREATE INDEX IF NOT EXISTS idx_jobs_next_attempt   ON jobs(state, next_attempt_at);
            CREATE INDEX IF NOT EXISTS idx_jobs_owner          ON jobs(owner_user_id);
            CREATE TABLE IF NOT EXISTS block_embeddings (
              block_id INTEGER PRIMARY KEY,
              document_id INTEGER NOT NULL,
              dim INTEGER NOT NULL,
              vector BLOB NOT NULL,
              model TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY (block_id) REFERENCES content_blocks(id) ON DELETE CASCADE,
              FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_block_emb_doc ON block_embeddings(document_id);
            CREATE TABLE IF NOT EXISTS search_history (
              id INTEGER PRIMARY KEY,
              user_id INTEGER NOT NULL,
              query TEXT NOT NULL,
              filters_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS saved_searches (
              id INTEGER PRIMARY KEY,
              user_id INTEGER NOT NULL,
              name TEXT NOT NULL,
              query TEXT NOT NULL,
              filters_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              UNIQUE(user_id, name),
              FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_search_history_user ON search_history(user_id, id);
            CREATE INDEX IF NOT EXISTS idx_saved_searches_user ON saved_searches(user_id, id);
            """
        )
        # Migration: add role column for existing databases
        try:
            self.conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
            self.conn.commit()
        except Exception:
            pass
        # Migration: add owner_principal_id column for existing databases
        try:
            self.conn.execute("ALTER TABLE documents ADD COLUMN owner_principal_id INTEGER")
            self.conn.commit()
        except Exception:
            pass
        # Migration: add principal_id column on users (links user -> their 'user' principal)
        try:
            self.conn.execute("ALTER TABLE users ADD COLUMN principal_id INTEGER")
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
        self._migrate_jobs_cancellation()
        self._backfill_acl()

    def _migrate_jobs_cancellation(self) -> None:
        """Bring a pre-cancellation `jobs` table up to the new schema.

        SQLite can't ALTER a CHECK constraint, so if the stored CREATE statement
        lacks 'cancelled' we rebuild the table preserving every row (the rebuild
        also adds the cancel_requested column). On a fresh DB this is a no-op.
        Idempotent: safe on every startup.
        """
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone()
        if row is None:
            return
        create_sql = row["sql"] or ""

        needs_check_rebuild = "'cancelled'" not in create_sql
        has_cancel_col = any(
            c[1] == "cancel_requested"
            for c in self.conn.execute("PRAGMA table_info(jobs)").fetchall()
        )
        if not needs_check_rebuild and has_cancel_col:
            return

        self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self.conn.execute("BEGIN")
            self.conn.execute(
                """
                CREATE TABLE jobs_new (
                  id INTEGER PRIMARY KEY,
                  kind TEXT NOT NULL,
                  state TEXT NOT NULL CHECK(state IN
                    ('pending','running','succeeded','failed','interrupted','cancelled')),
                  payload_json TEXT NOT NULL,
                  progress_json TEXT,
                  result_json TEXT,
                  error_message TEXT,
                  retry_count INTEGER NOT NULL DEFAULT 0,
                  max_retries INTEGER NOT NULL DEFAULT 0,
                  cancel_requested INTEGER NOT NULL DEFAULT 0,
                  owner_user_id INTEGER,
                  created_at TEXT NOT NULL,
                  started_at TEXT,
                  finished_at TEXT,
                  next_attempt_at TEXT,
                  FOREIGN KEY (owner_user_id) REFERENCES users(id) ON DELETE SET NULL
                )
                """
            )
            self.conn.execute(
                """
                INSERT INTO jobs_new
                  (id, kind, state, payload_json, progress_json, result_json,
                   error_message, retry_count, max_retries, owner_user_id,
                   created_at, started_at, finished_at, next_attempt_at)
                SELECT
                   id, kind, state, payload_json, progress_json, result_json,
                   error_message, retry_count, max_retries, owner_user_id,
                   created_at, started_at, finished_at, next_attempt_at
                FROM jobs
                """
            )
            self.conn.execute("DROP TABLE jobs")
            self.conn.execute("ALTER TABLE jobs_new RENAME TO jobs")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state)"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_kind_state ON jobs(kind, state)"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_next_attempt ON jobs(state, next_attempt_at)"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_owner ON jobs(owner_user_id)"
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        finally:
            self.conn.execute("PRAGMA foreign_keys=ON")

    def _backfill_acl(self) -> None:
        """Idempotent backfill so existing data stays visible after ACL migration.

        Creates the 'public' group, ensures every existing user has a 'user'-type
        principal and is a member of 'public', and grants read on every existing
        document to 'public'. Safe to run on every startup.
        """
        from datetime import UTC, datetime
        now = datetime.now(tz=UTC).isoformat()

        self.conn.execute(
            "INSERT OR IGNORE INTO principals(type, external_id, display_name, created_at) "
            "VALUES('group', 'public', 'Everyone', ?)",
            (now,),
        )
        public_row = self.conn.execute(
            "SELECT id FROM principals WHERE type='group' AND external_id='public'"
        ).fetchone()
        public_id = public_row["id"]

        users_without_principal = self.conn.execute(
            "SELECT id, username FROM users WHERE principal_id IS NULL"
        ).fetchall()
        for u in users_without_principal:
            self.conn.execute(
                "INSERT OR IGNORE INTO principals(type, external_id, display_name, created_at) "
                "VALUES('user', ?, ?, ?)",
                (u["username"], u["username"], now),
            )
            p = self.conn.execute(
                "SELECT id FROM principals WHERE type='user' AND external_id=?",
                (u["username"],),
            ).fetchone()
            self.conn.execute(
                "UPDATE users SET principal_id=? WHERE id=?", (p["id"], u["id"])
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO user_groups(user_id, principal_id) VALUES(?, ?)",
                (u["id"], public_id),
            )

        # Grant 'public' read on every existing document that has no ACL yet
        self.conn.execute(
            """
            INSERT OR IGNORE INTO document_acl(document_id, principal_id, permission, granted_at)
            SELECT d.id, ?, 'read', ?
            FROM documents d
            WHERE NOT EXISTS (SELECT 1 FROM document_acl a WHERE a.document_id = d.id)
            """,
            (public_id, now),
        )
        self.conn.commit()

    # ── ACL management: groups & membership ────────────────────────────

    def create_group(self, name: str, display_name: str | None = None) -> int:
        """Create (or return existing) a 'group'-type principal. Idempotent on name."""
        now = datetime.now(tz=UTC).isoformat()
        self.conn.execute(
            "INSERT OR IGNORE INTO principals(type, external_id, display_name, created_at) "
            "VALUES('group', ?, ?, ?)",
            (name, display_name or name, now),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM principals WHERE type='group' AND external_id=?", (name,)
        ).fetchone()
        return int(row["id"])

    def list_groups(self) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT p.id, p.external_id, p.display_name, p.created_at,
                   (SELECT COUNT(*) FROM user_groups ug WHERE ug.principal_id = p.id) AS member_count
            FROM principals p
            WHERE p.type='group'
            ORDER BY p.external_id
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_group(self, principal_id: int) -> None:
        row = self.conn.execute(
            "SELECT type, external_id FROM principals WHERE id=?", (principal_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Group not found")
        if row["type"] != "group":
            raise ValueError("Principal is not a group")
        if row["external_id"] == "public":
            raise ValueError("The 'public' group cannot be deleted")
        self.conn.execute("DELETE FROM principals WHERE id=?", (principal_id,))
        self.conn.commit()

    def add_user_to_group(self, user_id: int, principal_id: int) -> None:
        grp = self.conn.execute(
            "SELECT type FROM principals WHERE id=?", (principal_id,)
        ).fetchone()
        if grp is None or grp["type"] != "group":
            raise ValueError("Target principal is not a group")
        if self.get_user_by_id(user_id) is None:
            raise ValueError("User not found")
        self.conn.execute(
            "INSERT OR IGNORE INTO user_groups(user_id, principal_id) VALUES(?, ?)",
            (user_id, principal_id),
        )
        self.conn.commit()

    def remove_user_from_group(self, user_id: int, principal_id: int) -> None:
        row = self.conn.execute(
            "SELECT external_id FROM principals WHERE id=?", (principal_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Group not found")
        if row["external_id"] == "public":
            raise ValueError("Membership in the 'public' group cannot be removed")
        self.conn.execute(
            "DELETE FROM user_groups WHERE user_id=? AND principal_id=?",
            (user_id, principal_id),
        )
        self.conn.commit()

    def list_group_members(self, principal_id: int) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT u.id AS user_id, u.username, u.role
            FROM user_groups ug
            JOIN users u ON u.id = ug.user_id
            WHERE ug.principal_id = ?
            ORDER BY u.username
            """,
            (principal_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── ACL management: document grants ────────────────────────────────

    _VALID_PERMISSIONS = ("read", "write")

    def grant(self, document_id: int, principal_id: int, permission: str) -> None:
        if permission not in self._VALID_PERMISSIONS:
            raise ValueError(f"permission must be one of {self._VALID_PERMISSIONS}")
        if self.conn.execute(
            "SELECT 1 FROM documents WHERE id=?", (document_id,)
        ).fetchone() is None:
            raise ValueError("Document not found")
        if self.conn.execute(
            "SELECT 1 FROM principals WHERE id=?", (principal_id,)
        ).fetchone() is None:
            raise ValueError("Principal not found")
        now = datetime.now(tz=UTC).isoformat()
        self.conn.execute(
            "INSERT OR IGNORE INTO document_acl(document_id, principal_id, permission, granted_at) "
            "VALUES(?, ?, ?, ?)",
            (document_id, principal_id, permission, now),
        )
        self.conn.commit()

    def revoke(self, document_id: int, principal_id: int, permission: str) -> None:
        if permission not in self._VALID_PERMISSIONS:
            raise ValueError(f"permission must be one of {self._VALID_PERMISSIONS}")
        self.conn.execute(
            "DELETE FROM document_acl WHERE document_id=? AND principal_id=? AND permission=?",
            (document_id, principal_id, permission),
        )
        self.conn.commit()

    def list_document_acl(self, document_id: int) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT a.principal_id, p.type AS principal_type, p.external_id,
                   p.display_name, a.permission, a.granted_at
            FROM document_acl a
            JOIN principals p ON p.id = a.principal_id
            WHERE a.document_id = ?
            ORDER BY p.type, p.external_id, a.permission
            """,
            (document_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_document(self, path: str):
        return self.conn.execute("SELECT * FROM documents WHERE path = ?", (path,)).fetchone()

    def get_document_by_id(self, document_id: int):
        return self.conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()

    def upsert_document(
        self,
        fp: FileFingerprint,
        ext: ExtractionResult,
        owner_principal_id: int | None = None,
    ) -> int:
        now = datetime.now(tz=UTC).isoformat()
        meta = ext.document_metadata
        self.conn.execute("DELETE FROM content_blocks WHERE document_id IN (SELECT id FROM documents WHERE path = ?)", (str(fp.path),))
        self.conn.execute("DELETE FROM content_fts WHERE path = ?", (str(fp.path),))
        self.conn.execute(
            """
            INSERT INTO documents(path, filename, extension, file_size, modified_at, sha256, indexed_at, status, error_message, page_count, slide_count, metadata_json, owner_principal_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
              filename=excluded.filename, extension=excluded.extension, file_size=excluded.file_size, modified_at=excluded.modified_at,
              sha256=excluded.sha256, indexed_at=excluded.indexed_at, status=excluded.status, error_message=excluded.error_message,
              page_count=excluded.page_count, slide_count=excluded.slide_count, metadata_json=excluded.metadata_json,
              owner_principal_id=COALESCE(excluded.owner_principal_id, documents.owner_principal_id)
            """,
            (
                str(fp.path), fp.path.name, fp.path.suffix.lower(), fp.file_size, fp.modified_at.isoformat(), fp.sha256, now,
                ext.status, ext.error_message, meta.get("page_count"), meta.get("slide_count"), str(meta), owner_principal_id,
            ),
        )
        doc_id = self.conn.execute("SELECT id FROM documents WHERE path = ?", (str(fp.path),)).fetchone()[0]
        # Foundational ACL: every newly indexed document gets public read.
        # Mirrors _backfill_acl so freshly indexed docs are immediately visible to all users.
        public_row = self.conn.execute(
            "SELECT id FROM principals WHERE type='group' AND external_id='public'"
        ).fetchone()
        if public_row is not None:
            self.conn.execute(
                "INSERT OR IGNORE INTO document_acl(document_id, principal_id, permission, granted_at) "
                "VALUES(?,?, 'read', ?)",
                (doc_id, public_row["id"], now),
            )
        path_str = str(fp.path)
        name_str = fp.path.name
        ext_str = fp.path.suffix.lower()
        fts_rows = []
        new_blocks: list[tuple[int, str]] = []
        for block in ext.blocks:
            cursor = self.conn.execute(
                "INSERT INTO content_blocks(document_id, block_type, block_number, text, extractor, text_length, metadata_json) VALUES(?,?,?,?,?,?,?)",
                (doc_id, block.block_type, block.block_number, block.text, block.extractor, len(block.text), str(block.metadata)),
            )
            fts_rows.append((doc_id, cursor.lastrowid, path_str, name_str, ext_str, block.block_type, str(block.block_number), block.text))
            new_blocks.append((cursor.lastrowid, block.text))
        if fts_rows:
            self.conn.executemany(
                "INSERT INTO content_fts(document_id, block_id, path, filename, extension, block_type, block_number, text) VALUES(?,?,?,?,?,?,?,?)",
                fts_rows,
            )
        if self.embed_fn is not None:
            for _block_id, _text in new_blocks:
                vec = self.embed_fn(_text)
                if vec:
                    self.upsert_block_embedding(_block_id, doc_id, vec, model=self.embed_model)
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

    def get_user_principal_id(self, user_id: int) -> int | None:
        row = self.conn.execute(
            "SELECT principal_id FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if not row:
            return None
        return row["principal_id"]

    def user_can_read_document(self, user_id: int, document_id: int) -> bool:
        from document_search.services.acl_service import can_read_document_subquery
        sql, params = can_read_document_subquery(user_id, document_id)
        return self.conn.execute(sql, tuple(params)).fetchone() is not None

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
        user_id = cursor.lastrowid
        self.conn.execute(
            "INSERT OR IGNORE INTO principals(type, external_id, display_name, created_at) "
            "VALUES('user', ?, ?, ?)",
            (username, username, now),
        )
        p_row = self.conn.execute(
            "SELECT id FROM principals WHERE type='user' AND external_id=?", (username,)
        ).fetchone()
        self.conn.execute(
            "UPDATE users SET principal_id=? WHERE id=?", (p_row["id"], user_id)
        )
        public_row = self.conn.execute(
            "SELECT id FROM principals WHERE type='group' AND external_id='public'"
        ).fetchone()
        if public_row:
            self.conn.execute(
                "INSERT OR IGNORE INTO user_groups(user_id, principal_id) VALUES(?, ?)",
                (user_id, public_row["id"]),
            )
        self.conn.commit()
        return user_id

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

    def upsert_block_embedding(
        self, block_id: int, document_id: int, vector: list[float], model: str
    ) -> None:
        from document_search.services.embedding_service import pack_vector
        now = datetime.now(tz=UTC).isoformat()
        self.conn.execute(
            """
            INSERT INTO block_embeddings(block_id, document_id, dim, vector, model, created_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(block_id) DO UPDATE SET
              document_id=excluded.document_id, dim=excluded.dim,
              vector=excluded.vector, model=excluded.model, created_at=excluded.created_at
            """,
            (block_id, document_id, len(vector), pack_vector(vector), model, now),
        )
        self.conn.commit()

    def get_blocks_without_embedding(self, limit: int = 500) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT cb.id AS block_id, cb.document_id AS document_id, cb.text AS text
            FROM content_blocks cb
            LEFT JOIN block_embeddings be ON be.block_id = cb.id
            WHERE be.block_id IS NULL AND length(cb.text) > 0
            ORDER BY cb.id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def iter_block_embeddings(self, document_ids: list[int]):
        """Yield (block_id, document_id, vector) for the given documents.

        Decodes the packed BLOB lazily; intended for the in-memory cosine scan
        over an ACL-filtered candidate set.
        """
        from document_search.services.embedding_service import unpack_vector
        if not document_ids:
            return
        placeholders = ",".join("?" * len(document_ids))
        rows = self.conn.execute(
            f"SELECT block_id, document_id, vector FROM block_embeddings "
            f"WHERE document_id IN ({placeholders})",
            tuple(document_ids),
        ).fetchall()
        for r in rows:
            yield r["block_id"], r["document_id"], unpack_vector(r["vector"])

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

    def record_search_history(self, user_id: int, query: str, filters: dict) -> None:
        """Record a search in the user's rolling history.

        No-op for empty queries (mirrors the old localStorage behaviour). The
        entry is de-duplicated on identical query+filters (re-running an
        identical search floats it back to the top), and the user's history is
        trimmed to the newest HISTORY_CAP rows.
        """
        if not query or not query.strip():
            return
        filters_json = json.dumps(filters or {}, sort_keys=True)
        now = datetime.now(tz=UTC).isoformat()
        self.conn.execute(
            "DELETE FROM search_history WHERE user_id=? AND query=? AND filters_json=?",
            (user_id, query, filters_json),
        )
        self.conn.execute(
            "INSERT INTO search_history(user_id, query, filters_json, created_at) VALUES(?,?,?,?)",
            (user_id, query, filters_json, now),
        )
        # Trim to the newest HISTORY_CAP rows for this user
        self.conn.execute(
            """
            DELETE FROM search_history
            WHERE user_id=? AND id NOT IN (
                SELECT id FROM search_history WHERE user_id=? ORDER BY id DESC LIMIT ?
            )
            """,
            (user_id, user_id, HISTORY_CAP),
        )
        self.conn.commit()

    def list_search_history(self, user_id: int, limit: int = HISTORY_CAP) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, query, filters_json, created_at FROM search_history "
            "WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        out = []
        for r in rows:
            try:
                filters = json.loads(r["filters_json"]) if r["filters_json"] else {}
            except (ValueError, TypeError):
                filters = {}
            out.append({
                "id": r["id"],
                "query": r["query"],
                "filters": filters,
                "created_at": r["created_at"],
            })
        return out

    def clear_search_history(self, user_id: int) -> int:
        cur = self.conn.execute("DELETE FROM search_history WHERE user_id=?", (user_id,))
        self.conn.commit()
        return cur.rowcount

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
