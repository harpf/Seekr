import sqlite3
from datetime import UTC, datetime

import pytest

from document_search.index.sqlite_store import SqliteStore


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "test.db")


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


def test_jobs_has_cancel_requested_column(store):
    cols = {c[1] for c in store.conn.execute("PRAGMA table_info(jobs)").fetchall()}
    assert "cancel_requested" in cols


def test_cancelled_state_is_accepted(store):
    store.conn.execute(
        "INSERT INTO jobs(kind, state, payload_json, retry_count, max_retries, created_at) "
        "VALUES('demo', 'cancelled', '{}', 0, 0, ?)",
        (_now(),),
    )
    store.conn.commit()
    row = store.conn.execute("SELECT state FROM jobs WHERE kind='demo'").fetchone()
    assert row["state"] == "cancelled"


def test_invalid_state_still_rejected(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO jobs(kind, state, payload_json, retry_count, max_retries, created_at) "
            "VALUES('demo', 'bogus', '{}', 0, 0, ?)",
            (_now(),),
        )


def test_migration_from_old_constraint_preserves_rows_and_relaxes_check(tmp_path):
    """Simulate a pre-cancellation DB: build a jobs table with the OLD CHECK
    constraint and a row, then open it through SqliteStore and confirm the
    migration ran (cancel_requested exists, 'cancelled' now allowed, row kept)."""
    db = tmp_path / "legacy.db"

    raw = sqlite3.connect(db)
    raw.executescript(
        """
        CREATE TABLE jobs (
          id INTEGER PRIMARY KEY,
          kind TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('pending','running','succeeded','failed','interrupted')),
          payload_json TEXT NOT NULL,
          progress_json TEXT,
          result_json TEXT,
          error_message TEXT,
          retry_count INTEGER NOT NULL DEFAULT 0,
          max_retries INTEGER NOT NULL DEFAULT 0,
          owner_user_id INTEGER,
          created_at TEXT NOT NULL,
          started_at TEXT,
          finished_at TEXT,
          next_attempt_at TEXT
        );
        """
    )
    raw.execute(
        "INSERT INTO jobs(id, kind, state, payload_json, retry_count, max_retries, created_at) "
        "VALUES(7, 'legacy', 'succeeded', '{\"a\":1}', 2, 3, '2020-01-01T00:00:00+00:00')"
    )
    raw.commit()
    raw.close()

    store = SqliteStore(db)  # triggers _init_schema -> migration
    cols = {c[1] for c in store.conn.execute("PRAGMA table_info(jobs)").fetchall()}
    assert "cancel_requested" in cols
    row = store.conn.execute("SELECT * FROM jobs WHERE id=7").fetchone()
    assert row["kind"] == "legacy"
    assert row["state"] == "succeeded"
    assert row["retry_count"] == 2
    assert row["cancel_requested"] == 0
    store.conn.execute(
        "INSERT INTO jobs(kind, state, payload_json, retry_count, max_retries, created_at) "
        "VALUES('demo', 'cancelled', '{}', 0, 0, ?)",
        (_now(),),
    )
    store.conn.commit()
