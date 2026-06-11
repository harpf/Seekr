"""Sole read/write surface for the scan_review queue. Wraps a SqliteStore so it
shares the per-thread connection + WAL semantics of the rest of the index layer."""
from __future__ import annotations

import json
from datetime import UTC, datetime


class ScanReviewStore:
    def __init__(self, store) -> None:
        self._store = store
        self.conn = store.conn

    @staticmethod
    def _now() -> str:
        return datetime.now(tz=UTC).isoformat()

    def create_pending(self, *, inbox_id: str, document_id: int | None,
                       staging_path: str, original_filename: str,
                       suggested_folder: str | None, suggested_tags: list[str] | None,
                       ai_reasoning: str | None, ai_decision_id: int | None) -> int:
        now = self._now()
        cur = self.conn.execute(
            "INSERT INTO scan_review(inbox_id, document_id, staging_path, original_filename, "
            "status, suggested_folder, suggested_tags, ai_reasoning, ai_decision_id, "
            "created_at, updated_at) VALUES(?,?,?,?, 'pending', ?,?,?,?, ?,?)",
            (inbox_id, document_id, staging_path, original_filename, suggested_folder,
             json.dumps(suggested_tags or []), ai_reasoning, ai_decision_id, now, now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def create_error(self, *, inbox_id: str, staging_path: str,
                     original_filename: str, error_message: str) -> int:
        now = self._now()
        cur = self.conn.execute(
            "INSERT INTO scan_review(inbox_id, document_id, staging_path, original_filename, "
            "status, error_message, created_at, updated_at) "
            "VALUES(?,?,?,?, 'error', ?, ?, ?)",
            (inbox_id, None, staging_path, original_filename, error_message, now, now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def _row_to_dict(self, row) -> dict:
        d = dict(row)
        d["suggested_tags"] = json.loads(d.get("suggested_tags") or "[]")
        return d

    def get(self, review_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM scan_review WHERE id=?", (review_id,)
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_reviews(self, *, inbox_ids: list[str] | None, status: str | None) -> list[dict]:
        clauses, params = [], []
        if inbox_ids is not None:
            if not inbox_ids:
                return []
            clauses.append(f"inbox_id IN ({','.join('?' * len(inbox_ids))})")
            params.extend(inbox_ids)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM scan_review{where} ORDER BY created_at DESC", params
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def _set_status(self, review_id: int, status: str, reviewed_by: str | None) -> None:
        now = self._now()
        self.conn.execute(
            "UPDATE scan_review SET status=?, reviewed_by=COALESCE(?, reviewed_by), "
            "reviewed_at=CASE WHEN ? IS NULL THEN reviewed_at ELSE ? END, updated_at=? "
            "WHERE id=?",
            (status, reviewed_by, reviewed_by, now, now, review_id),
        )
        self.conn.commit()

    def mark_filed(self, review_id: int, reviewed_by: str) -> None:
        self._set_status(review_id, "filed", reviewed_by)

    def mark_rejected(self, review_id: int, reviewed_by: str) -> None:
        self._set_status(review_id, "rejected", reviewed_by)

    def set_pending(self, review_id: int) -> None:
        now = self._now()
        self.conn.execute(
            "UPDATE scan_review SET status='pending', error_message=NULL, updated_at=? WHERE id=?",
            (now, review_id),
        )
        self.conn.commit()

    def count_pending(self, inbox_ids: list[str]) -> int:
        if not inbox_ids:
            return 0
        row = self.conn.execute(
            f"SELECT COUNT(*) AS c FROM scan_review WHERE status='pending' "
            f"AND inbox_id IN ({','.join('?' * len(inbox_ids))})",
            inbox_ids,
        ).fetchone()
        return int(row["c"])

    def staging_paths_with_rows(self) -> set[str]:
        rows = self.conn.execute("SELECT staging_path FROM scan_review").fetchall()
        return {r["staging_path"] for r in rows}
