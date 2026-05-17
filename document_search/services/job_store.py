"""Persistent job queue. The only module that writes to the `jobs` table.

Concurrency model: each method is one short SQL statement (or two within a
single connection transaction). The shared SqliteStore connection uses WAL
mode with a 5 s busy_timeout, so concurrent calls serialise cleanly.
"""
from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from document_search.index.sqlite_store import SqliteStore


_VALID_STATES = ("pending", "running", "succeeded", "failed", "interrupted")


class JobStore:
    def __init__(self, store: SqliteStore):
        self.store = store
        self.conn = store.conn
        # Serialises ALL access to the shared sqlite3.Connection. Python's
        # sqlite3 module allows the connection to be used from multiple threads
        # (check_same_thread=False), but concurrent .execute() calls on the
        # same connection are NOT safe — they intermittently raise
        # OperationalError('cannot commit transaction - SQL statements in
        # progress') or DatabaseError. The Worker poll thread and FastAPI
        # request threads share this JobStore instance, so every public method
        # below wraps its body in `with self._lock:`. RLock is used (rather
        # than Lock) to be defensive against future transitive calls between
        # JobStore methods; today none exist.
        self._lock = threading.RLock()

    @staticmethod
    def _now() -> str:
        return datetime.now(tz=UTC).isoformat()

    def enqueue(
        self,
        kind: str,
        payload: dict,
        owner_user_id: int | None = None,
        max_retries: int = 0,
    ) -> int:
        with self._lock:
            now = self._now()
            cur = self.conn.execute(
                "INSERT INTO jobs(kind, state, payload_json, retry_count, max_retries, "
                "owner_user_id, created_at) VALUES(?, 'pending', ?, 0, ?, ?, ?)",
                (kind, json.dumps(payload), max_retries, owner_user_id, now),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def get(self, job_id: int) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            return dict(row) if row else None

    def claim_next(self, kinds: list[str] | None = None) -> dict | None:
        """Atomically pick the next runnable pending job and mark it running.

        UPDATE ... RETURNING is atomic in SQLite 3.35+; we ship on Python 3.11
        which bundles SQLite >= 3.40.
        """
        with self._lock:
            now = self._now()
            if kinds:
                placeholders = ",".join("?" * len(kinds))
                sql = f"""
                    UPDATE jobs SET state='running', started_at=?
                    WHERE id = (
                      SELECT id FROM jobs
                      WHERE state='pending'
                        AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                        AND kind IN ({placeholders})
                      ORDER BY id LIMIT 1
                    )
                    RETURNING *
                """
                params: tuple = (now, now, *kinds)
            else:
                sql = """
                    UPDATE jobs SET state='running', started_at=?
                    WHERE id = (
                      SELECT id FROM jobs
                      WHERE state='pending'
                        AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                      ORDER BY id LIMIT 1
                    )
                    RETURNING *
                """
                params = (now, now)
            row = self.conn.execute(sql, params).fetchone()
            self.conn.commit()
            return dict(row) if row else None

    def update_progress(self, job_id: int, progress: dict) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE jobs SET progress_json=? WHERE id=?",
                (json.dumps(progress), job_id),
            )
            self.conn.commit()

    def mark_succeeded(self, job_id: int, result: dict | None) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE jobs SET state='succeeded', result_json=?, finished_at=? WHERE id=?",
                (json.dumps(result) if result is not None else None, self._now(), job_id),
            )
            self.conn.commit()

    def mark_failed(self, job_id: int, error_message: str) -> None:
        """Failure path. If retries are available, re-schedule with backoff;
        otherwise transition to 'failed'."""
        with self._lock:
            row = self.conn.execute(
                "SELECT retry_count, max_retries FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                return
            attempt = row["retry_count"] + 1
            if attempt > row["max_retries"]:
                self.conn.execute(
                    "UPDATE jobs SET state='failed', error_message=?, "
                    "retry_count=?, finished_at=? WHERE id=?",
                    (error_message, attempt, self._now(), job_id),
                )
            else:
                backoff_s = min(2 ** attempt, 600)
                next_at = (datetime.now(tz=UTC) + timedelta(seconds=backoff_s)).isoformat()
                self.conn.execute(
                    "UPDATE jobs SET state='pending', error_message=?, "
                    "retry_count=?, next_attempt_at=?, started_at=NULL WHERE id=?",
                    (error_message, attempt, next_at, job_id),
                )
            self.conn.commit()

    def mark_failed_permanent(self, job_id: int, error_message: str) -> None:
        """Mark a job as failed without honouring retry policy.
        Used for non-transient failures (e.g. no handler registered for the kind)."""
        with self._lock:
            self.conn.execute(
                "UPDATE jobs SET state='failed', error_message=?, finished_at=? WHERE id=?",
                (error_message, self._now(), job_id),
            )
            self.conn.commit()

    def mark_interrupted_running_jobs(self) -> int:
        """Called at app startup. Any job left in 'running' state is from a prior
        process that exited without finishing it; mark interrupted so operators
        can decide what to do."""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE jobs SET state='interrupted', finished_at=? WHERE state='running'",
                (self._now(),),
            )
            self.conn.commit()
            return cur.rowcount

    def list_jobs(
        self,
        owner_user_id: int | None = None,
        state: str | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        with self._lock:
            clauses = []
            params: list[Any] = []
            if owner_user_id is not None:
                clauses.append("owner_user_id=?")
                params.append(owner_user_id)
            if state:
                clauses.append("state=?")
                params.append(state)
            if kind:
                clauses.append("kind=?")
                params.append(kind)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            sql = f"SELECT * FROM jobs {where} ORDER BY id DESC LIMIT ?"
            params.append(limit)
            return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]
