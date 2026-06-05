# Persistent Job Queue — Completion Plan (cancellation, global API/UI, remaining migrations)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the persistent job queue. The core (`JobStore`, `Worker`, `jobs` table, and three migrated kinds — `index_paths`, `ai_suggest_structure`, `ai_reorganize`) already ships (see `docs/superpowers/plans/2026-05-17-job-queue.md`). This plan closes the remaining gaps from `docs/ROADMAP.md` P1:
1. Migrate the last in-memory job, `ai_pull_model`, to a persistent `ai_pull` kind whose handler streams progress from Ollama `/api/pull`, while preserving the existing `GET /api/ai/jobs/{id}` response shape for pull jobs.
2. Persist a final status row for the system-update job (`/api/update/run`) **before** the process is replaced, so the UI can confirm the outcome after the restart. The job is **not** auto-resumed.
3. Add a `cancelled` job state and cooperative cancellation: `POST /api/jobs/{id}/cancel` (owner or admin), a cancel flag the worker exposes, and handlers that poll it at progress checkpoints.
4. Add `GET /api/jobs` (owner-scoped; admin sees all) backed by `JobStore.list_jobs`, and a frontend global **Jobs** dashboard (new nav entry + table of kind/state/progress/retry_count, with re-enqueue for `interrupted` jobs and cancel for active jobs).

**Architecture (unchanged base):** One `jobs` table; `JobStore` is the only writer; one `Worker` polls, claims (`UPDATE ... RETURNING`), and dispatches each job to a registered handler in a bounded sub-thread; handlers receive `progress_cb(dict)`; raises become retry-or-fail; on startup, `running` jobs become `interrupted`. This plan **adds**: a `cancel_requested` flag on the `jobs` row plus a `Worker.is_cancelled(job_id)` accessor that handlers poll, a `cancelled` terminal state, the `ai_pull` kind, an update-status persistence hook, and a global jobs API/UI.

**Tech Stack:** Python 3.11, SQLite (`UPDATE ... RETURNING`, ships ≥ 3.40 with 3.11), threading, FastAPI, pytest, vanilla JS frontend. No new third-party deps.

**Scope boundaries:**

In scope:
- New `cancelled` state + `cancel_requested` column (additive migration; CHECK-constraint rebuild).
- `JobStore.request_cancel`, `JobStore.is_cancel_requested`, `JobStore.mark_cancelled`, `JobStore.re_enqueue`.
- `Worker.is_cancelled(job_id)` accessor + worker honouring cancellation at claim time and after a handler returns/raises `JobCancelled`.
- `ai_pull` job kind, streaming handler, `/api/ai/models/pull` enqueues it, `GET /api/ai/jobs/{id}` serves it from `JobStore` (removing the in-memory `ai_jobs` path for pulls).
- `/api/update/run` writes a persistent `system_update` job row and updates it to `succeeded`/`failed` **before** restart; restart-recovery does NOT re-run it.
- `POST /api/jobs/{id}/cancel`, `GET /api/jobs`, `POST /api/jobs/{id}/re-enqueue`.
- Frontend `/jobs` page + nav entry + polling table.
- Tests in the style of `tests/test_job_store.py`, `tests/test_job_worker.py`, `tests/test_app_jobs.py`.

Out of scope (deferred):
- Killing a running OS subprocess mid-flight (cancellation is cooperative; the update subprocess and Ollama HTTP request are not force-killed — they finish or time out, then the cancel takes effect at the next checkpoint).
- WebSocket/SSE push (the UI still polls).
- Multi-process / distributed workers, priorities, deadlines.
- Auto-resume of interrupted jobs (re-enqueue is a manual operator action via the new endpoint/UI).

---

## Key design decisions (locked)

- **`cancelled` is terminal.** States become `pending | running | succeeded | failed | interrupted | cancelled`. A cancel request on a `pending` job transitions it straight to `cancelled` (it never runs). A cancel request on a `running` job sets `cancel_requested=1`; the handler polls `Worker.is_cancelled(job_id)` and raises `JobCancelled`, which the worker translates to `cancelled` (NOT a retry, NOT a fail).
- **Cancellation is cooperative, never forced.** `Worker.is_cancelled(job_id)` reads the row's `cancel_requested` flag. Handlers that loop call it at each `progress_cb`. A handler with no checkpoints (e.g. a single blocking call) cannot be cancelled mid-call; the cancel applies once control returns.
- **`cancel_requested` is a separate column, not encoded in `state`.** A job stays `running` while we ask it to stop; only the worker writes the terminal `cancelled` state. This avoids a race where the API flips a `running` row to `cancelled` while the handler is mid-write.
- **CHECK-constraint migration via table rebuild.** SQLite cannot `ALTER` a CHECK constraint. We detect the old constraint and, if present, rebuild the table inside a transaction (`CREATE TABLE jobs_new ...; INSERT INTO jobs_new SELECT ...; DROP TABLE jobs; ALTER TABLE jobs_new RENAME TO jobs;`) then recreate indexes. New databases get the new constraint directly from `_init_schema`. The migration is idempotent and runs at every startup.
- **`ai_pull` preserves the legacy pull response shape.** `GET /api/ai/jobs/{id}` for an `ai_pull` job returns `{"status": "pulling"|"done"|"error", "model": ..., "result": {...}}` — mapped from the persistent row, NOT from `ai_jobs`. The in-memory `ai_jobs` dict and its pull branch are removed.
- **`system_update` is persisted but never auto-resumed.** The handler runs inline in the request thread (it must finish writing the status row before `update.sh` replaces the process), not via the worker. `mark_interrupted_running_jobs()` already covers the crash case; on a *successful* update the row is `succeeded` before exec, so the post-restart UI shows the real outcome.
- **`GET /api/jobs` is owner-scoped; admin sees all.** Non-admin callers get only `owner_user_id == self`. `system_update` and `ai_pull` jobs are admin-owned, so non-admins won't see them.
- **Re-enqueue clones, not mutates.** `POST /api/jobs/{id}/re-enqueue` creates a NEW pending job copying `kind`/`payload`/`owner`/`max_retries` from an `interrupted` or `failed` source. The original row is left as historical record. This avoids resurrecting a row mid-`finished_at`.

---

## File Structure

**Modify:**
- `document_search/index/sqlite_store.py` — new-DB schema gains `cancelled` in the CHECK + `cancel_requested INTEGER NOT NULL DEFAULT 0`; add `_migrate_jobs_cancellation()` (table rebuild) called from `_init_schema`.
- `document_search/services/job_store.py` — `_VALID_STATES` += `cancelled`; add `request_cancel`, `is_cancel_requested`, `mark_cancelled`, `re_enqueue`; `claim_next` skips/cancels already-cancel-requested pending jobs.
- `document_search/services/job_worker.py` — `JobCancelled` exception; `Worker.is_cancelled(job_id)`; `_execute` maps `JobCancelled` to `mark_cancelled`; pre-dispatch cancel check.
- `document_search/app.py` — `ai_pull` handler + migrate `/api/ai/models/pull`; serve `ai_pull` from `GET /api/ai/jobs/{id}` and drop the `ai_jobs` pull path; persist `system_update` in `/api/update/run`; add `POST /api/jobs/{id}/cancel`, `GET /api/jobs`, `POST /api/jobs/{id}/re-enqueue`; add `GET /jobs` page route.
- `document_search/web/templates/index.html` and the other four templates — add a **Jobs** nav link.
- `document_search/web/static/app.js` — `loadJobs`, `renderJobsTable`, `cancelJob`, `reEnqueueJob`, jobs-page bootstrap.

**Create:**
- `document_search/web/templates/jobs.html` — the Jobs dashboard page.
- `tests/test_job_cancellation.py` — JobStore + Worker cancellation unit tests.
- `tests/test_app_jobs_completion.py` — API tests for `ai_pull`, update persistence, cancel/list/re-enqueue endpoints.

**Untouched:**
- The three already-migrated kinds and their routes.

---

## Task 1: `cancelled` state + `cancel_requested` column (schema + safe migration)

**Files:**
- Modify: `document_search/index/sqlite_store.py`
- Test: `tests/test_job_cancellation.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_job_cancellation.py`:

```python
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
    with pytest.raises(Exception):
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
    import sqlite3

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
    # 'cancelled' is now a legal state on the migrated table
    store.conn.execute(
        "INSERT INTO jobs(kind, state, payload_json, retry_count, max_retries, created_at) "
        "VALUES('demo', 'cancelled', '{}', 0, 0, ?)",
        (_now(),),
    )
    store.conn.commit()
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_job_cancellation.py -v
```

Expected: `test_jobs_has_cancel_requested_column` and the migration test fail (column/constraint not present yet).

- [ ] **Step 3: Update the new-DB schema**

In `document_search/index/sqlite_store.py`, the `CREATE TABLE IF NOT EXISTS jobs (...)` block (around line 138) currently has:

```sql
              state TEXT NOT NULL CHECK(state IN ('pending','running','succeeded','failed','interrupted')),
```

Replace that single line with the relaxed constraint plus the new column. The full edited columns block:

```sql
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
```

(The four `idx_jobs_*` index lines below it are unchanged.)

- [ ] **Step 4: Add the migration helper and call it**

Still in `sqlite_store.py`, `_init_schema` ends with `self._backfill_acl()` (around line 187). Immediately **before** that call, add:

```python
        self._migrate_jobs_cancellation()
```

Then add the method (place it right after `_init_schema`, before `_backfill_acl`):

```python
    def _migrate_jobs_cancellation(self) -> None:
        """Bring a pre-cancellation `jobs` table up to the new schema.

        Two independent changes are needed and neither can be done with a plain
        ALTER on SQLite:
          1. add the `cancel_requested` column (ALTER works for this), and
          2. relax the `state` CHECK constraint to also allow 'cancelled'
             (CHECK constraints cannot be altered — needs a table rebuild).

        Strategy: inspect the stored CREATE statement. If it lacks 'cancelled',
        rebuild the table preserving every row. The rebuild also adds the new
        column, so we don't need a separate ALTER on the legacy path. On a
        freshly-created DB (new schema already correct) this is a no-op.

        Idempotent: safe to run on every startup.
        """
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone()
        if row is None:
            return  # table not created yet (shouldn't happen — schema runs first)
        create_sql = row["sql"] or ""

        needs_check_rebuild = "'cancelled'" not in create_sql
        has_cancel_col = any(
            c[1] == "cancel_requested"
            for c in self.conn.execute("PRAGMA table_info(jobs)").fetchall()
        )

        if not needs_check_rebuild and has_cancel_col:
            return  # already migrated / fresh schema

        # Full rebuild covers both missing column and stale CHECK in one shot.
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
            # Carry over the legacy columns explicitly; cancel_requested defaults to 0.
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
            self.conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_jobs_state          ON jobs(state);
                CREATE INDEX IF NOT EXISTS idx_jobs_kind_state     ON jobs(kind, state);
                CREATE INDEX IF NOT EXISTS idx_jobs_next_attempt   ON jobs(state, next_attempt_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_owner          ON jobs(owner_user_id);
                """
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        finally:
            self.conn.execute("PRAGMA foreign_keys=ON")
```

> NOTE on ordering: `_init_schema` runs the `CREATE TABLE IF NOT EXISTS jobs` script first. On a fresh DB that creates the table with the new constraint + column, so `_migrate_jobs_cancellation` short-circuits. On a legacy DB the `IF NOT EXISTS` is a no-op (table already there with the old shape), and the migration rebuilds it. Either way the table ends up correct.

- [ ] **Step 5: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_job_cancellation.py -v
```

Expected: 4 passing.

- [ ] **Step 6: Full suite (existing job tests must still pass)**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: existing baseline + 4 new, zero regressions. In particular `tests/test_job_store.py::test_jobs_state_check_constraint` still passes (it asserts the five legacy states are accepted and `bogus` is rejected — both still true under the relaxed constraint).

- [ ] **Step 7: Commit**

```powershell
git add document_search/index/sqlite_store.py tests/test_job_cancellation.py
git commit -m @'
feat(jobs): add cancelled state and cancel_requested column with safe migration

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 2: `JobStore` cancellation + re-enqueue API

**Files:**
- Modify: `document_search/services/job_store.py`
- Test: `tests/test_job_cancellation.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_job_cancellation.py`:

```python
from document_search.services.job_store import JobStore


def test_request_cancel_on_pending_marks_cancelled_immediately(store):
    js = JobStore(store)
    job_id = js.enqueue("demo", {})
    outcome = js.request_cancel(job_id)
    assert outcome == "cancelled"
    job = js.get(job_id)
    assert job["state"] == "cancelled"
    # A cancelled pending job is never claimed
    assert js.claim_next() is None


def test_request_cancel_on_running_sets_flag_only(store):
    js = JobStore(store)
    job_id = js.enqueue("demo", {})
    js.claim_next()  # -> running
    outcome = js.request_cancel(job_id)
    assert outcome == "requested"
    job = js.get(job_id)
    assert job["state"] == "running"
    assert job["cancel_requested"] == 1
    assert js.is_cancel_requested(job_id) is True


def test_request_cancel_on_finished_is_noop(store):
    js = JobStore(store)
    job_id = js.enqueue("demo", {})
    js.claim_next()
    js.mark_succeeded(job_id, {"ok": True})
    outcome = js.request_cancel(job_id)
    assert outcome == "noop"
    assert js.get(job_id)["state"] == "succeeded"


def test_request_cancel_unknown_id(store):
    js = JobStore(store)
    assert js.request_cancel(999999) == "not_found"


def test_mark_cancelled_sets_terminal_state(store):
    js = JobStore(store)
    job_id = js.enqueue("demo", {})
    js.claim_next()
    js.mark_cancelled(job_id)
    job = js.get(job_id)
    assert job["state"] == "cancelled"
    assert job["finished_at"] is not None


def test_re_enqueue_clones_interrupted_job(store):
    js = JobStore(store)
    src = js.enqueue("index_paths", {"paths": ["/x"]}, owner_user_id=5, max_retries=2)
    js.claim_next()
    js.mark_interrupted_running_jobs()
    assert js.get(src)["state"] == "interrupted"

    new_id = js.re_enqueue(src)
    assert new_id is not None and new_id != src
    new = js.get(new_id)
    assert new["state"] == "pending"
    assert new["kind"] == "index_paths"
    assert new["owner_user_id"] == 5
    assert new["max_retries"] == 2
    import json
    assert json.loads(new["payload_json"]) == {"paths": ["/x"]}
    # Original untouched
    assert js.get(src)["state"] == "interrupted"


def test_re_enqueue_rejects_active_job(store):
    js = JobStore(store)
    src = js.enqueue("demo", {})
    js.claim_next()  # running
    assert js.re_enqueue(src) is None
```

- [ ] **Step 2: Run, expect FAIL** (`AttributeError: request_cancel`)

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_job_cancellation.py -v
```

- [ ] **Step 3: Implement the new methods**

In `document_search/services/job_store.py`:

a) Update the constant at the top:

```python
_VALID_STATES = ("pending", "running", "succeeded", "failed", "interrupted", "cancelled")
_ACTIVE_STATES = ("pending", "running")
_RE_ENQUEUEABLE = ("interrupted", "failed", "cancelled")
```

b) Add these methods to `JobStore` (place them after `mark_interrupted_running_jobs`, before `list_jobs`):

```python
    def request_cancel(self, job_id: int) -> str:
        """Ask a job to stop.

        Returns:
          'cancelled'  — job was pending and is now terminal cancelled.
          'requested'  — job was running; cancel_requested flag set, worker will honour.
          'noop'       — job already in a terminal state.
          'not_found'  — no such job.
        """
        with self._lock:
            row = self.conn.execute(
                "SELECT state FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                return "not_found"
            state = row["state"]
            if state == "pending":
                self.conn.execute(
                    "UPDATE jobs SET state='cancelled', cancel_requested=1, finished_at=? WHERE id=?",
                    (self._now(), job_id),
                )
                self.conn.commit()
                return "cancelled"
            if state == "running":
                self.conn.execute(
                    "UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,)
                )
                self.conn.commit()
                return "requested"
            return "noop"

    def is_cancel_requested(self, job_id: int) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            return bool(row and row["cancel_requested"])

    def mark_cancelled(self, job_id: int) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE jobs SET state='cancelled', finished_at=? WHERE id=?",
                (self._now(), job_id),
            )
            self.conn.commit()

    def re_enqueue(self, job_id: int) -> int | None:
        """Clone an interrupted/failed/cancelled job as a fresh pending job.

        Returns the new job id, or None if the source job is missing or still
        active (pending/running/succeeded)."""
        with self._lock:
            row = self.conn.execute(
                "SELECT kind, payload_json, owner_user_id, max_retries, state "
                "FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if row is None or row["state"] not in _RE_ENQUEUEABLE:
                return None
            now = self._now()
            cur = self.conn.execute(
                "INSERT INTO jobs(kind, state, payload_json, retry_count, max_retries, "
                "owner_user_id, created_at) VALUES(?, 'pending', ?, 0, ?, ?, ?)",
                (row["kind"], row["payload_json"], row["max_retries"],
                 row["owner_user_id"], now),
            )
            self.conn.commit()
            return int(cur.lastrowid)
```

c) Guard `claim_next` so a pending job whose cancel was requested (edge case: flag set on a row that was briefly pending) is never run. In both SQL branches of `claim_next`, add `AND cancel_requested = 0` to the inner `SELECT`'s `WHERE`:

```sql
                  WHERE state='pending'
                    AND cancel_requested = 0
                    AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
```

(Apply to both the `kinds`-filtered and the unfiltered branch.)

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_job_cancellation.py -v
```

Expected: 4 (Task 1) + 7 = 11 passing.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

- [ ] **Step 6: Commit**

```powershell
git add document_search/services/job_store.py tests/test_job_cancellation.py
git commit -m @'
feat(jobs): JobStore request_cancel/mark_cancelled/re_enqueue + cancel-aware claim

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 3: Worker honours cooperative cancellation

**Files:**
- Modify: `document_search/services/job_worker.py`
- Test: `tests/test_job_cancellation.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_job_cancellation.py`:

```python
import threading
import time

from document_search.services.job_worker import Worker, JobCancelled


@pytest.fixture
def worker_setup(tmp_path):
    store = SqliteStore(tmp_path / "w.db")
    js = JobStore(store)
    worker = Worker(js, max_concurrent=2, poll_interval_s=0.01)
    yield store, js, worker
    worker.stop()


def test_handler_polling_cancel_flag_results_in_cancelled(worker_setup):
    """A long-running handler loops checking worker.is_cancelled(job_id); once the
    API requests cancel, the next checkpoint raises JobCancelled and the worker
    records state='cancelled' (not failed, not retried)."""
    store, js, worker = worker_setup
    entered = threading.Event()

    @worker.handler("loopy")
    def loopy(payload, progress_cb):
        job_id = payload["job_id"]
        entered.set()
        for i in range(10_000):
            if worker.is_cancelled(job_id):
                raise JobCancelled()
            progress_cb({"i": i})
            time.sleep(0.005)
        return {"completed": True}

    # enqueue then inject its own id into the payload so the handler can self-check
    job_id = js.enqueue("loopy", {})
    js.conn.execute(
        "UPDATE jobs SET payload_json=? WHERE id=?",
        (f'{{"job_id": {job_id}}}', job_id),
    )
    js.conn.commit()

    worker.tick()
    assert entered.wait(timeout=2.0)
    # Request cancellation while it's looping
    assert js.request_cancel(job_id) == "requested"
    worker.wait_until_idle(timeout=3.0)

    job = js.get(job_id)
    assert job["state"] == "cancelled", job
    assert job["retry_count"] == 0  # not treated as a retryable failure


def test_jobcancelled_not_retried_even_with_retries(worker_setup):
    store, js, worker = worker_setup

    @worker.handler("givesup")
    def givesup(payload, progress_cb):
        raise JobCancelled()

    job_id = js.enqueue("givesup", {}, max_retries=5)
    worker.tick()
    worker.wait_until_idle(timeout=2.0)
    job = js.get(job_id)
    assert job["state"] == "cancelled"
    assert job["retry_count"] == 0


def test_is_cancelled_reflects_store(worker_setup):
    store, js, worker = worker_setup
    job_id = js.enqueue("demo", {})
    js.claim_next()
    assert worker.is_cancelled(job_id) is False
    js.request_cancel(job_id)
    assert worker.is_cancelled(job_id) is True
```

- [ ] **Step 2: Run, expect FAIL** (`ImportError: JobCancelled`)

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_job_cancellation.py -k cancel -v
```

- [ ] **Step 3: Implement in `job_worker.py`**

a) Add the exception near the top (after the `Handler = ...` type alias):

```python
class JobCancelled(Exception):
    """Raised by a handler (or the worker) to signal a cooperative cancellation.
    Translated by the worker into the terminal `cancelled` state — never retried."""
```

b) Add the accessor method on `Worker` (place it next to `register`):

```python
    def is_cancelled(self, job_id: int) -> bool:
        """Handlers poll this at checkpoints to support cooperative cancellation."""
        return self.job_store.is_cancel_requested(job_id)
```

c) Update `_execute`. Replace the body from `try:` down to the `mark_succeeded` line with the version below (the `finally` block and follow-up `tick()` stay as-is):

```python
    def _execute(self, job: dict) -> None:
        try:
            job_id = job["id"]
            kind = job["kind"]

            # Cancellation may have been requested between claim and dispatch.
            if self.job_store.is_cancel_requested(job_id):
                self.job_store.mark_cancelled(job_id)
                return

            handler = self._handlers.get(kind)
            if handler is None:
                self.job_store.mark_failed_permanent(
                    job_id, f"no handler registered for kind '{kind}'"
                )
                return
            import json
            payload = json.loads(job["payload_json"])

            def progress_cb(p: dict) -> None:
                self.job_store.update_progress(job_id, p)

            try:
                result = handler(payload, progress_cb)
            except JobCancelled:
                log.info("Job %s (%s) cancelled cooperatively", job_id, kind)
                self.job_store.mark_cancelled(job_id)
                return
            except Exception as exc:
                # A cancel request that surfaced as a generic error still wins:
                # don't retry a job the operator asked to stop.
                if self.job_store.is_cancel_requested(job_id):
                    log.info("Job %s (%s) failed after cancel request -> cancelled", job_id, kind)
                    self.job_store.mark_cancelled(job_id)
                    return
                log.exception("Handler %s failed for job %s", kind, job_id)
                self.job_store.mark_failed(job_id, f"{type(exc).__name__}: {exc}")
                return

            # Handler returned normally; honour a late cancel request.
            if self.job_store.is_cancel_requested(job_id):
                self.job_store.mark_cancelled(job_id)
                return
            self.job_store.mark_succeeded(job_id, result if isinstance(result, dict) else None)
        finally:
            with self._inflight_lock:
                self._inflight.discard(threading.current_thread())
            self._semaphore.release()
            try:
                self.tick()
            except Exception:
                log.exception("Worker follow-up tick failed")
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_job_cancellation.py -v
```

Expected: 11 + 3 = 14 passing.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: zero regressions in `tests/test_job_worker.py` (the success/fail/retry/unknown-kind paths are unchanged).

- [ ] **Step 6: Commit**

```powershell
git add document_search/services/job_worker.py tests/test_job_cancellation.py
git commit -m @'
feat(jobs): Worker honours cooperative cancellation via JobCancelled + is_cancelled

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 4: Migrate `ai_pull_model` to a persistent `ai_pull` kind (streamed progress)

**Files:**
- Modify: `document_search/services/ai_organizer.py` (add a streaming pull helper)
- Modify: `document_search/app.py` (register handler, migrate route, serve from JobStore, drop in-memory pull path)
- Test: `tests/test_app_jobs_completion.py` (new)

The existing `/api/ai/models/pull` (app.py:1347–1359) writes `ai_jobs[job_id] = {"status":"pulling","model":...,"result":None}` and a daemon thread sets `status` to `done`/`error`. `GET /api/ai/jobs/{id}` (app.py:1483–1487) falls back to `ai_jobs.get(job_id)` for that shape. We preserve the wire shape but back it with the queue.

Legacy pull response shape to preserve:
```json
{"status": "pulling"|"done"|"error", "model": "<name>", "result": {"ok": true/false, ...}}
```

- [ ] **Step 1: Add a streaming pull helper to `AiOrganizer`**

`pull_model` (ai_organizer.py:184) does a single non-streaming POST. Add a streaming variant **next to it** that yields `{status, completed, total}` dicts as Ollama emits them, finishing with a terminal `{"ok": ...}` dict. Keep `pull_model` intact (other callers/tests may use it).

```python
    def pull_model_stream(self, model: str | None = None):
        """Generator that streams Ollama /api/pull progress.

        Yields dicts of the form Ollama emits, e.g.
          {"status": "pulling manifest"}
          {"status": "downloading", "completed": 12345, "total": 67890}
          {"status": "success"}
        Terminates by yielding a final dict {"ok": True/False, "model": name, ...}.
        Caller is responsible for translating these into job progress/result.
        """
        name = model or self.model
        payload = json.dumps({"name": name, "stream": True}).encode()
        try:
            req = urllib.request.Request(
                f"{self.base_url}/api/pull",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            last_status = "pulling"
            with urllib.request.urlopen(req, timeout=600) as resp:
                for raw in resp:
                    line = raw.decode().strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if evt.get("status"):
                        last_status = evt["status"]
                    yield evt
                    if evt.get("error"):
                        yield {"ok": False, "model": name, "error": evt["error"]}
                        return
            yield {"ok": True, "model": name, "status": last_status}
        except urllib.error.URLError as e:
            yield {"ok": False, "model": name, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            yield {"ok": False, "model": name, "error": f"{type(e).__name__}: {e}"}
```

- [ ] **Step 2: Write the failing API test**

Create `tests/test_app_jobs_completion.py`:

```python
import time

import pytest

pytestmark = pytest.mark.skipif(
    not pytest.importorskip("fastapi", reason="fastapi not installed"),
    reason="fastapi required",
)

from fastapi.testclient import TestClient

from document_search.app import create_app


def _login(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _wait(client, token, url, until, timeout=5.0):
    deadline = time.monotonic() + timeout
    body = {}
    while time.monotonic() < deadline:
        r = client.get(url, headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        body = r.json()
        if until(body):
            return body
        time.sleep(0.03)
    return body


def test_ai_pull_persists_and_preserves_legacy_shape(tmp_path):
    app = create_app(str(tmp_path / "t.db"))

    # Stub the handler so no live Ollama is needed; it must emit progress then succeed.
    @app.state.worker.handler("ai_pull")
    def _stub(payload, progress_cb):
        progress_cb({"status": "downloading", "completed": 50, "total": 100})
        progress_cb({"status": "downloading", "completed": 100, "total": 100})
        return {"ok": True, "model": payload.get("model") or "llama3.2", "status": "success"}

    with TestClient(app) as client:
        token = _login(client)
        r = client.post("/api/ai/models/pull", headers={"X-Auth-Token": token}, json={})
        assert r.status_code == 200, r.text
        body = r.json()
        assert "job_id" in body
        assert body["job_id"].isdigit()  # persistent numeric id
        assert "model" in body

        final = _wait(
            client, token, f"/api/ai/jobs/{body['job_id']}",
            until=lambda b: b["status"] in ("done", "error"),
        )
        assert final["status"] == "done"
        assert final["model"]
        assert final["result"]["ok"] is True


def test_ai_pull_failure_maps_to_error_status(tmp_path):
    app = create_app(str(tmp_path / "t.db"))

    @app.state.worker.handler("ai_pull")
    def _stub(payload, progress_cb):
        return {"ok": False, "model": "bogus", "error": "manifest not found"}

    with TestClient(app) as client:
        token = _login(client)
        r = client.post("/api/ai/models/pull", headers={"X-Auth-Token": token},
                        json={"model": "bogus"})
        job_id = r.json()["job_id"]
        final = _wait(
            client, token, f"/api/ai/jobs/{job_id}",
            until=lambda b: b["status"] in ("done", "error"),
        )
        assert final["status"] == "error"
        assert final["result"]["ok"] is False
        assert "manifest" in final["result"]["error"]
```

- [ ] **Step 3: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_jobs_completion.py -v
```

Expected: fail — route returns a hex id and shape mismatch.

- [ ] **Step 4: Register the `ai_pull` handler**

In `app.py`, after the `@worker.handler("ai_reorganize")` block (the `_handle_ai_reorganize` function ends around line 388), append:

```python
    @worker.handler("ai_pull")
    def _handle_ai_pull(payload: dict, progress_cb):
        model = payload.get("model")
        last = {"ok": False}
        for evt in organizer.pull_model_stream(model):
            if "ok" in evt:
                last = evt
                break
            # Surface streamed download progress to the job row.
            progress_cb({
                "status": evt.get("status", "pulling"),
                "completed": evt.get("completed", 0),
                "total": evt.get("total", 0),
            })
        if not last.get("ok"):
            # Make the failure visible as an error_message too, but the result
            # dict is what the legacy UI reads.
            return {"ok": False, "model": model or organizer.model,
                    "error": last.get("error", "pull failed")}
        return {"ok": True, "model": last.get("model") or model or organizer.model,
                "status": last.get("status", "success")}
```

- [ ] **Step 5: Migrate the route**

Replace `api_ai_pull_model` (app.py:1347–1359) with:

```python
    @app.post("/api/ai/models/pull")
    def api_ai_pull_model(req: PullModelRequest, x_auth_token: str | None = Header(default=None)):
        admin_id = require_admin(x_auth_token)
        model = req.model or organizer.model
        job_id = job_store.enqueue(
            "ai_pull",
            payload={"model": req.model},
            owner_user_id=admin_id,
            max_retries=0,
        )
        return {"job_id": str(job_id), "model": model}
```

- [ ] **Step 6: Serve `ai_pull` from `GET /api/ai/jobs/{id}` and drop the in-memory pull path**

In `api_ai_job` (app.py:1450), the persistent-kinds check currently lists `("ai_suggest_structure", "ai_reorganize")`. Extend it to include `ai_pull` and branch the response shape. Replace the inner block (from `if job and job["kind"] in (...)` through the `return response` line) with:

```python
            if job and job["kind"] in ("ai_suggest_structure", "ai_reorganize", "ai_pull"):
                user_row = store().get_user_by_id(user_id)
                is_admin = bool(user_row) and user_row["role"] == "admin"
                if not is_admin and job["owner_user_id"] != user_id:
                    raise HTTPException(status_code=404, detail="Job not found")
                import json
                state_to_status = {
                    "pending": "queued",
                    "running": "running",
                    "succeeded": "finished",
                    "failed": "failed",
                    "interrupted": "interrupted",
                    "cancelled": "cancelled",
                }
                progress = json.loads(job["progress_json"]) if job["progress_json"] else {}
                result   = json.loads(job["result_json"])   if job["result_json"]   else None

                if job["kind"] == "ai_pull":
                    # Preserve the legacy pull shape: status pulling|done|error.
                    payload = json.loads(job["payload_json"]) if job["payload_json"] else {}
                    model = (result or {}).get("model") or payload.get("model") or organizer.model
                    if job["state"] in ("pending", "running"):
                        pull_status = "pulling"
                    elif job["state"] == "succeeded" and (result or {}).get("ok"):
                        pull_status = "done"
                    else:  # failed / cancelled / interrupted / ok=False
                        pull_status = "error"
                    return {
                        "status": pull_status,
                        "model": model,
                        "result": result,
                        "progress": progress,
                    }

                response: dict = {"status": state_to_status.get(job["state"], job["state"])}
                if job["kind"] == "ai_suggest_structure":
                    response["result"] = result if job["state"] == "succeeded" else None
                else:  # ai_reorganize
                    final = result or progress or {}
                    response["total"]   = final.get("total", 0)
                    response["done"]    = final.get("done", 0)
                    response["results"] = final.get("results", [])
                if job["state"] == "failed":
                    response["error"] = job["error_message"]
                return response
```

Then **remove** the in-memory fallback at the end of `api_ai_job` (the lines `legacy = ai_jobs.get(job_id)` / `if not legacy:` / `raise ...` / `return legacy`) and replace them with a plain 404 since no job kind uses `ai_jobs` anymore:

```python
        raise HTTPException(status_code=404, detail="Job not found")
```

Finally, delete the now-dead `ai_jobs: dict[str, dict] = {}` declaration at app.py:265 (grep `ai_jobs` to confirm there are no other references before removing).

- [ ] **Step 7: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_jobs_completion.py -v
```

Expected: 2 passing.

- [ ] **Step 8: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

If any existing AI test referenced `ai_jobs` or the hex pull id, update it to the persistent shape. Read failures carefully before editing.

- [ ] **Step 9: Commit**

```powershell
git add document_search/services/ai_organizer.py document_search/app.py tests/test_app_jobs_completion.py
git commit -m @'
feat(jobs): migrate ai_pull_model to persistent ai_pull kind with streamed progress

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 5: Persist a final status row for the system-update job

**Files:**
- Modify: `document_search/app.py` (`/api/update/run`, `/api/update/status`)
- Test: `tests/test_app_jobs_completion.py` (extend)

`/api/update/run` (app.py:886–909) runs `update.sh` in a daemon thread, mutating the in-memory `_update_job` dict; on a real deployment `update.sh` restarts the process, losing that dict. We additionally write a persistent `system_update` job row and finalise it (`succeeded`/`failed`) **inside `_runner` before the process can be replaced**, so after the restart the UI confirms the outcome. We do NOT enqueue it on the worker (it must not be auto-resumed) and `mark_interrupted_running_jobs()` already handles the crash-mid-update case.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_app_jobs_completion.py`:

```python
def test_update_run_persists_job_row(tmp_path, monkeypatch):
    """We can't run the real update.sh, so monkeypatch the script path check and
    subprocess to a fast no-op, then assert a persistent system_update row exists
    and is finalised."""
    import document_search.app as appmod

    app = create_app(str(tmp_path / "t.db"))

    # Make the update feature believe a script exists and runs successfully.
    class _FakeProc:
        returncode = 0
        stdout = "updated ok"
        stderr = ""

    monkeypatch.setenv("DOCUMENT_SEARCH_UI_UPDATE_ENABLED", "true")
    monkeypatch.setattr(appmod.Path, "exists", lambda self: True, raising=False)
    monkeypatch.setattr(appmod.subprocess, "run", lambda *a, **k: _FakeProc())

    with TestClient(app) as client:
        token = _login(client)
        r = client.post("/api/update/run", headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        body = r.json()
        assert "job_id" in body

        # Persistent row: poll status until finalised.
        final = _wait(
            client, token, "/api/update/status",
            until=lambda b: b.get("status") in ("done", "error"),
        )
        assert final["status"] == "done"

        # And a persistent jobs row of kind 'system_update' exists in succeeded state.
        js = app.state.job_store
        rows = js.list_jobs(kind="system_update")
        assert len(rows) == 1
        assert rows[0]["state"] == "succeeded"
```

> NOTE: `monkeypatch.setattr(appmod.Path, "exists", ...)` is broad but acceptable in this isolated test because the request path only calls `Path(...).exists()` for the script check. If it interferes with other `Path.exists` calls in the request, narrow it by patching a small wrapper instead (see Step 2 — we route the existence check through a local variable so the test can monkeypatch precisely). Prefer the narrowed approach below.

- [ ] **Step 2: Implement persistence in `/api/update/run`**

Replace `api_run_update` (app.py:886–909) with the version below. It keeps the in-memory `_update_job` (so `/api/update/status` stays fast pre-restart) AND writes a persistent row that is finalised before the subprocess returns control.

```python
    @app.post("/api/update/run")
    def api_run_update(x_auth_token: str | None = Header(default=None)):
        admin_id = require_admin(x_auth_token)
        if os.getenv("DOCUMENT_SEARCH_UI_UPDATE_ENABLED", "true").lower() != "true":
            raise HTTPException(status_code=403, detail="UI update disabled")
        script = Path("/app/scripts/update.sh")
        if not script.exists():
            raise HTTPException(status_code=404, detail="Update script not found")

        job_id = uuid.uuid4().hex
        # Persistent row so the outcome survives the process replacement that
        # update.sh performs. NOT enqueued on the worker -> never auto-resumed.
        persistent_id = job_store.enqueue(
            "system_update",
            payload={"legacy_job_id": job_id},
            owner_user_id=admin_id,
            max_retries=0,
        )
        # Move it straight to running (the request thread runs it inline).
        job_store.claim_next(kinds=["system_update"])
        _update_job.clear()
        _update_job.update({
            "job_id": job_id, "persistent_id": persistent_id,
            "status": "running", "stdout": "", "stderr": "", "exit_code": None,
        })

        def _runner():
            proc = subprocess.run(["/bin/sh", str(script)], capture_output=True, text=True, check=False)
            ok = proc.returncode == 0
            _update_job.update({
                "status": "done" if ok else "error",
                "exit_code": proc.returncode,
                "stdout": proc.stdout[-4000:],
                "stderr": proc.stderr[-4000:],
            })
            # Finalise the persistent row BEFORE update.sh can replace the process.
            result = {
                "exit_code": proc.returncode,
                "stdout": proc.stdout[-4000:],
                "stderr": proc.stderr[-4000:],
            }
            if ok:
                job_store.mark_succeeded(persistent_id, result)
            else:
                job_store.mark_failed_permanent(
                    persistent_id, f"update.sh exited {proc.returncode}"
                )

        threading.Thread(target=_runner, daemon=True).start()
        return {"job_id": job_id, "persistent_id": str(persistent_id), "status": "started"}
```

> `claim_next(kinds=["system_update"])` flips the freshly-enqueued row to `running` so that, if the process is killed mid-update, the startup `mark_interrupted_running_jobs()` correctly records it as `interrupted` rather than leaving it `pending` (which would look like it never started). On success, `_runner` overwrites it with `succeeded` before exec.

- [ ] **Step 3: Optionally surface the persistent outcome in `/api/update/status`**

`/api/update/status` (app.py:911–914) returns `dict(_update_job)`. Post-restart that dict is reset to `{"status": "idle"}`. Augment it to fall back to the most recent persistent `system_update` row so the UI confirms the outcome after the restart. Replace `api_update_status`:

```python
    @app.get("/api/update/status")
    def api_update_status(x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        if _update_job.get("status") not in (None, "idle"):
            return dict(_update_job)
        # After a restart the in-memory dict is reset; surface the last persistent row.
        rows = job_store.list_jobs(kind="system_update", limit=1)
        if not rows:
            return dict(_update_job)
        row = rows[0]
        state_map = {"succeeded": "done", "failed": "error",
                     "running": "running", "interrupted": "error",
                     "cancelled": "error", "pending": "running"}
        import json as _json
        result = _json.loads(row["result_json"]) if row["result_json"] else {}
        return {
            "status": state_map.get(row["state"], row["state"]),
            "exit_code": result.get("exit_code"),
            "stdout": result.get("stdout", ""),
            "stderr": result.get("stderr", row["error_message"] or ""),
            "persistent_id": str(row["id"]),
        }
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_jobs_completion.py::test_update_run_persists_job_row -v
```

If the broad `Path.exists` monkeypatch in Step 1 causes collateral failures, replace it with a targeted patch: in the test, patch only the script check by monkeypatching `appmod.subprocess.run` and instead asserting on `script.exists()` via a real temp file. Simplest robust approach — create the script for real:

```python
    # Robust alternative to patching Path.exists:
    script_dir = tmp_path / "app" / "scripts"
    script_dir.mkdir(parents=True)
    (script_dir / "update.sh").write_text("#!/bin/sh\necho ok\n")
    monkeypatch.setattr(appmod, "Path", appmod.Path)  # keep real Path
    # ...then monkeypatch the hard-coded "/app/scripts/update.sh" by patching
    # subprocess.run only, and accept that script.exists() is False in CI.
```

> Because the route hard-codes `/app/scripts/update.sh`, the cleanest test is to monkeypatch the route's existence check. Patch `appmod.subprocess.run` to the fake proc AND monkeypatch `pathlib.Path.exists` scoped narrowly. Keep the broad patch only inside the `with TestClient` block and undo via `monkeypatch` teardown. The test as written in Step 1 is acceptable; if flaky, prefer asserting the persisted row directly by calling `job_store.enqueue` semantics through the endpoint with the env-gated 404 mocked.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

- [ ] **Step 6: Commit**

```powershell
git add document_search/app.py tests/test_app_jobs_completion.py
git commit -m @'
feat(jobs): persist system-update outcome before process replacement

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 6: Global jobs API — cancel, list, re-enqueue

**Files:**
- Modify: `document_search/app.py` (three endpoints)
- Test: `tests/test_app_jobs_completion.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_app_jobs_completion.py`:

```python
def _create_user(client, admin_token, username, password="pw123456", role="user"):
    r = client.post(
        "/api/users",
        headers={"X-Auth-Token": admin_token},
        json={"username": username, "password": password, "role": role},
    )
    assert r.status_code in (200, 201), r.text


def test_list_jobs_owner_scoped_and_admin_sees_all(tmp_path):
    app = create_app(str(tmp_path / "t.db"))

    @app.state.worker.handler("demo_block")
    def _block(payload, progress_cb):
        import time as _t
        _t.sleep(2.0)
        return {}

    with TestClient(app) as client:
        admin_token = _login(client)
        _create_user(client, admin_token, "bob")
        bob_token = client.post("/api/login", json={"username": "bob", "password": "pw123456"}).json()["token"]

        # admin (user id 1) owns one job; bob owns another.
        admin_job = app.state.job_store.enqueue("demo_block", {}, owner_user_id=1)
        users = client.get("/api/users", headers={"X-Auth-Token": admin_token}).json()
        bob_uid = next(u["id"] for u in users if u["username"] == "bob")
        bob_job = app.state.job_store.enqueue("demo_block", {}, owner_user_id=bob_uid)

        # Non-admin bob sees only his own
        r = client.get("/api/jobs", headers={"X-Auth-Token": bob_token})
        assert r.status_code == 200, r.text
        ids = {j["id"] for j in r.json()}
        assert bob_job in ids
        assert admin_job not in ids

        # Admin sees both
        r = client.get("/api/jobs", headers={"X-Auth-Token": admin_token})
        ids = {j["id"] for j in r.json()}
        assert {admin_job, bob_job}.issubset(ids)


def test_cancel_job_endpoint_owner_and_admin(tmp_path):
    app = create_app(str(tmp_path / "t.db"))

    @app.state.worker.handler("loopy")
    def _loopy(payload, progress_cb):
        import time as _t
        jid = payload["job_id"]
        for _ in range(1000):
            if app.state.worker.is_cancelled(jid):
                from document_search.services.job_worker import JobCancelled
                raise JobCancelled()
            progress_cb({"tick": 1})
            _t.sleep(0.01)
        return {}

    with TestClient(app) as client:
        token = _login(client)
        jid = app.state.job_store.enqueue("loopy", {}, owner_user_id=1)
        app.state.job_store.conn.execute(
            "UPDATE jobs SET payload_json=? WHERE id=?", (f'{{"job_id": {jid}}}', jid)
        )
        app.state.job_store.conn.commit()

        # Wait for it to start
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if app.state.job_store.get(jid)["state"] == "running":
                break
            time.sleep(0.02)

        r = client.post(f"/api/jobs/{jid}/cancel", headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        assert r.json()["outcome"] in ("requested", "cancelled")

        # Eventually terminal cancelled
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if app.state.job_store.get(jid)["state"] == "cancelled":
                break
            time.sleep(0.03)
        assert app.state.job_store.get(jid)["state"] == "cancelled"


def test_cancel_other_users_job_is_404_for_non_admin(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        admin_token = _login(client)
        _create_user(client, admin_token, "carol")
        carol_token = client.post("/api/login", json={"username": "carol", "password": "pw123456"}).json()["token"]
        admin_job = app.state.job_store.enqueue("demo", {}, owner_user_id=1)
        r = client.post(f"/api/jobs/{admin_job}/cancel", headers={"X-Auth-Token": carol_token})
        assert r.status_code == 404


def test_re_enqueue_endpoint(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        src = app.state.job_store.enqueue("index_paths", {"paths": ["/x"]}, owner_user_id=1)
        # Force into interrupted
        app.state.job_store.conn.execute("UPDATE jobs SET state='interrupted' WHERE id=?", (src,))
        app.state.job_store.conn.commit()
        r = client.post(f"/api/jobs/{src}/re-enqueue", headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        new_id = int(r.json()["job_id"])
        assert new_id != src
        assert app.state.job_store.get(new_id)["state"] in ("pending", "running", "succeeded")
```

- [ ] **Step 2: Run, expect FAIL** (404 — endpoints don't exist)

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_jobs_completion.py -k "list_jobs or cancel or re_enqueue" -v
```

- [ ] **Step 3: Implement the endpoints**

In `app.py`, add a helper + three routes. Place them right after `api_index_job` (the `GET /api/index/jobs/{job_id}` block ends ~line 823) so they live with the other job routes:

```python
    def _job_acl_or_404(job_id_str: str, user_id: int) -> dict:
        """Resolve a persistent job by string id, enforcing owner-or-admin access.
        Raises 404 for missing/foreign jobs (no information leak)."""
        if not job_id_str.isdigit():
            raise HTTPException(status_code=404, detail="Job not found")
        job = job_store.get(int(job_id_str))
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        user_row = store().get_user_by_id(user_id)
        is_admin = bool(user_row) and user_row["role"] == "admin"
        if not is_admin and job["owner_user_id"] != user_id:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    @app.get("/api/jobs")
    def api_list_jobs(
        state: str | None = None,
        kind: str | None = None,
        limit: int = 100,
        x_auth_token: str | None = Header(default=None),
    ):
        user_id = require_user(x_auth_token)
        user_row = store().get_user_by_id(user_id)
        is_admin = bool(user_row) and user_row["role"] == "admin"
        owner = None if is_admin else user_id
        rows = job_store.list_jobs(
            owner_user_id=owner, state=state, kind=kind, limit=min(limit, 500)
        )
        out = []
        for r in rows:
            progress = json.loads(r["progress_json"]) if r["progress_json"] else {}
            out.append({
                "id": r["id"],
                "kind": r["kind"],
                "state": r["state"],
                "retry_count": r["retry_count"],
                "max_retries": r["max_retries"],
                "cancel_requested": bool(r["cancel_requested"]),
                "owner_user_id": r["owner_user_id"],
                "created_at": r["created_at"],
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
                "error_message": r["error_message"],
                "progress": progress,
            })
        return out

    @app.post("/api/jobs/{job_id}/cancel")
    def api_cancel_job(job_id: str, x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        _job_acl_or_404(job_id, user_id)  # 404 if not owner/admin
        outcome = job_store.request_cancel(int(job_id))
        if outcome == "not_found":
            raise HTTPException(status_code=404, detail="Job not found")
        return {"job_id": job_id, "outcome": outcome}

    @app.post("/api/jobs/{job_id}/re-enqueue")
    def api_re_enqueue_job(job_id: str, x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        _job_acl_or_404(job_id, user_id)
        new_id = job_store.re_enqueue(int(job_id))
        if new_id is None:
            raise HTTPException(
                status_code=409,
                detail="Job cannot be re-enqueued (must be interrupted, failed, or cancelled)",
            )
        return {"job_id": str(new_id)}
```

> The `cancel`/`re-enqueue` routes use `require_user` (not `require_admin`) so a non-admin owner can manage their own jobs; `_job_acl_or_404` enforces the owner-or-admin rule and returns 404 (not 403) for foreign jobs to avoid leaking job existence.

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_jobs_completion.py -v
```

Expected: all green (pull 2 + update 1 + list/cancel/re-enqueue 4 = 7).

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

- [ ] **Step 6: Commit**

```powershell
git add document_search/app.py tests/test_app_jobs_completion.py
git commit -m @'
feat(jobs): add GET /api/jobs and POST /api/jobs/{id}/cancel|re-enqueue

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 7: Frontend global Jobs dashboard

**Files:**
- Create: `document_search/web/templates/jobs.html`
- Modify: all five templates' nav (`index.html`, `search.html`, `ingest.html`, `config.html`, `wiki.html`) — add the Jobs link
- Modify: `document_search/app.py` — add `GET /jobs` page route
- Modify: `document_search/web/static/app.js` — `loadJobs`, `renderJobsTable`, `cancelJob`, `reEnqueueJob`, bootstrap hook + nav map entry

- [ ] **Step 1: Add the page route**

In `app.py`, next to the other page routes (after the `/wiki` route, ~line 533), add:

```python
    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request):
        return templates.TemplateResponse("jobs.html", {"request": request})
```

- [ ] **Step 2: Add the Jobs nav link to every template**

In each of `index.html`, `search.html`, `ingest.html`, `config.html`, `wiki.html`, inside `<div class="nav-links">`, insert this link **after** the `/config` link block and before the `/wiki` link:

```html
        <a href="/jobs">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v4H3z"/><path d="M3 10h18v4H3z"/><path d="M3 17h18v4H3z"/></svg>
          <span class="nav-lbl">Jobs</span>
        </a>
```

- [ ] **Step 3: Create `jobs.html`**

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Jobs · Seekr</title>
  <link rel="icon" href="data:," />
  <link rel="stylesheet" href="/static/styles.css" />
</head>
<body class="wrap" data-page="jobs">

  <nav class="topbar">
    <div class="topbar-inner container">
      <a class="brand" href="/">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        Seekr
      </a>
      <div class="nav-links">
        <a href="/"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg><span class="nav-lbl">Dashboard</span></a>
        <a href="/search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg><span class="nav-lbl">Search</span></a>
        <a href="/ingest"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg><span class="nav-lbl">Ingest</span></a>
        <a href="/config"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg><span class="nav-lbl">Config</span></a>
        <a href="/jobs"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v4H3z"/><path d="M3 10h18v4H3z"/><path d="M3 17h18v4H3z"/></svg><span class="nav-lbl">Jobs</span></a>
        <a href="/wiki"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 3h6a4 4 0 014 4v14a3 3 0 00-3-3H2z"/><path d="M22 3h-6a4 4 0 00-4 4v14a3 3 0 013-3h7z"/></svg><span class="nav-lbl">Wiki</span></a>
        <div id="navSep" class="nav-sep hidden"></div>
        <button id="navSignout" class="nav-signout hidden" onclick="signOut()">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
          <span class="nav-lbl">Sign out</span>
        </button>
      </div>
    </div>
  </nav>

  <main class="main">
    <div class="container">

      <div id="authGate" class="auth-wrap">
        <div class="pg-head"><h1>Jobs</h1><p>Sign in to view background jobs.</p></div>
      </div>

      <div id="jobsPanel" class="hidden">
        <div class="pg-head">
          <h1>Background Jobs</h1>
          <p class="muted">Indexing, AI, model pulls and system updates. Auto-refreshes every few seconds.</p>
        </div>

        <div class="card">
          <div class="card-head">
            <div class="card-titles">
              <h2>All Jobs</h2>
              <p id="jobsSummary">Loading…</p>
            </div>
            <div class="btn-row" style="margin-left:auto;">
              <button class="btn btn-g btn-sm" onclick="loadJobs()">Refresh</button>
            </div>
          </div>
          <div class="card-body">
            <div class="u-table-wrap">
              <table class="u-table" id="jobsTable">
                <thead>
                  <tr>
                    <th>ID</th><th>Kind</th><th>State</th><th>Progress</th>
                    <th>Retries</th><th>Created</th><th>Actions</th>
                  </tr>
                </thead>
                <tbody id="jobsTbody">
                  <tr><td colspan="7" class="muted">No jobs yet.</td></tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>

    </div>
  </main>

  <div class="toast-wrap" id="toastWrap"></div>
  <script src="/static/app.js"></script>
</body>
</html>
```

- [ ] **Step 4: Add the JS functions**

In `app.js`, add the `jobs` entry to the nav map in `initNav` (around line 1689):

```javascript
  const map = { home: '/', search: '/search', ingest: '/ingest', config: '/config', jobs: '/jobs', wiki: '/wiki' };
```

Add these functions (place near the other job/index helpers, e.g. after `startIndex`):

```javascript
// ── Global Jobs dashboard ──────────────────────────────────────────
let _jobsPollTimer = null;

function _jobStateBadge(state) {
  // Reuse the existing badge palette: badge-b (blue), badge-g (green),
  // badge-a (amber), badge-n (neutral). There is no dedicated red badge,
  // so failed uses badge-a (amber) with a leading "failed" label.
  const cls = {
    pending: 'badge-n', running: 'badge-b', succeeded: 'badge-g',
    failed: 'badge-a', interrupted: 'badge-a', cancelled: 'badge-n',
  }[state] || 'badge-n';
  return `<span class="badge ${cls}">${escHtml(state)}</span>`;
}

function _jobProgressText(job) {
  const p = job.progress || {};
  if (job.kind === 'index_paths') {
    return `done ${p.done ?? 0} (idx ${p.indexed ?? 0}, upd ${p.updated ?? 0}, skip ${p.skipped ?? 0}, err ${p.errors ?? 0})`;
  }
  if (job.kind === 'ai_reorganize') {
    return `${p.done ?? 0}/${p.total ?? 0}`;
  }
  if (job.kind === 'ai_pull') {
    const c = p.completed ?? 0, t = p.total ?? 0;
    const pct = t ? Math.round((c / t) * 100) : 0;
    return p.status ? `${escHtml(p.status)}${t ? ` ${pct}%` : ''}` : '—';
  }
  return p.status ? escHtml(String(p.status)) : '—';
}

function renderJobsTable(jobs) {
  const tbody = document.getElementById('jobsTbody');
  const summary = document.getElementById('jobsSummary');
  if (!tbody) return;
  if (!jobs.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="muted">No jobs yet.</td></tr>';
    if (summary) summary.textContent = '0 jobs';
    return;
  }
  const active = jobs.filter(j => j.state === 'pending' || j.state === 'running').length;
  if (summary) summary.textContent = `${jobs.length} jobs · ${active} active`;

  tbody.innerHTML = jobs.map(j => {
    const isActive = j.state === 'pending' || j.state === 'running';
    const isReenqueueable = j.state === 'interrupted' || j.state === 'failed' || j.state === 'cancelled';
    const actions = [];
    if (isActive) {
      const label = j.cancel_requested ? 'Cancelling…' : 'Cancel';
      const dis = j.cancel_requested ? 'disabled' : '';
      actions.push(`<button class="btn btn-danger btn-sm" ${dis} onclick="cancelJob(${j.id})">${label}</button>`);
    }
    if (isReenqueueable) {
      actions.push(`<button class="btn btn-g btn-sm" onclick="reEnqueueJob(${j.id})">Re-enqueue</button>`);
    }
    const created = (j.created_at || '').replace('T', ' ').slice(0, 19);
    return `<tr>
      <td>${j.id}</td>
      <td><code>${escHtml(j.kind)}</code></td>
      <td>${_jobStateBadge(j.state)}</td>
      <td class="muted" style="font-size:.8rem;">${_jobProgressText(j)}</td>
      <td>${j.retry_count}/${j.max_retries}</td>
      <td class="muted" style="font-size:.78rem;">${escHtml(created)}</td>
      <td>${actions.join(' ') || '—'}</td>
    </tr>`;
  }).join('');
}

async function loadJobs() {
  try {
    const jobs = await api('/api/jobs');
    renderJobsTable(jobs);
    // Keep polling while anything is active.
    const active = jobs.some(j => j.state === 'pending' || j.state === 'running');
    if (_jobsPollTimer) clearTimeout(_jobsPollTimer);
    if (active) _jobsPollTimer = setTimeout(loadJobs, 2500);
  } catch (e) {
    showToast(e.message, 'err');
  }
}

async function cancelJob(jobId) {
  try {
    const r = await api(`/api/jobs/${jobId}/cancel`, 'POST', {});
    showToast(r.outcome === 'cancelled' ? 'Job cancelled' : 'Cancellation requested', 'ok');
    await loadJobs();
  } catch (e) {
    showToast(e.message, 'err');
  }
}

async function reEnqueueJob(jobId) {
  try {
    const r = await api(`/api/jobs/${jobId}/re-enqueue`, 'POST', {});
    showToast(`Re-enqueued as job ${r.job_id}`, 'ok');
    await loadJobs();
  } catch (e) {
    showToast(e.message, 'err');
  }
}
```

Wire the jobs page into `bootstrap` (around line 1746, alongside the `ingest` branch). Also reveal the panel: the jobs page uses `#jobsPanel` (not the generic `#statusPanel`/`#appPanel`), so add an explicit unhide:

```javascript
    if (document.body?.dataset?.page === 'jobs') {
      document.getElementById('jobsPanel')?.classList.remove('hidden');
      await loadJobs();
    }
```

> `showAuthedPanels()` only unhides `#statusPanel`/`#appPanel`/`#configPanel`; the jobs page has its own `#jobsPanel`, so the bootstrap branch unhides it directly after the `token` check passes.

- [ ] **Step 5: Manual smoke test (frontend)**

```powershell
$env:PYTHONPATH = "."; python -c "
from fastapi.testclient import TestClient
from document_search.app import create_app
import tempfile, pathlib, time
tmp = pathlib.Path(tempfile.mkdtemp())
app = create_app(str(tmp / 'jobs.db'))
with TestClient(app) as c:
    # page renders
    assert c.get('/jobs').status_code == 200
    tok = c.post('/api/login', json={'username':'admin','password':'admin'}).json()['token']
    # API returns a list
    r = c.get('/api/jobs', headers={'X-Auth-Token': tok})
    assert r.status_code == 200 and isinstance(r.json(), list), r.text
print('OK')
"
```

Expected: prints `OK`.

- [ ] **Step 6: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

- [ ] **Step 7: Commit**

```powershell
git add document_search/web/templates/jobs.html document_search/web/templates/index.html document_search/web/templates/search.html document_search/web/templates/ingest.html document_search/web/templates/config.html document_search/web/templates/wiki.html document_search/web/static/app.js document_search/app.py
git commit -m @'
feat(jobs): global Jobs dashboard page with cancel and re-enqueue

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 8: End-to-end integration + final verification

**Files:**
- Test: `tests/test_app_jobs_completion.py` (extend)

- [ ] **Step 1: Add an end-to-end cancellation-through-the-API integration test**

Append to `tests/test_app_jobs_completion.py`:

```python
def test_cancel_pending_job_via_api_is_immediate(tmp_path):
    """A pending job (worker saturated) cancelled via the API goes straight to
    cancelled and never runs its handler."""
    app = create_app(str(tmp_path / "t.db"))
    ran = []

    @app.state.worker.handler("never")
    def _never(payload, progress_cb):
        ran.append(payload)
        return {}

    with TestClient(app) as client:
        token = _login(client)
        # Stop the worker so the job stays pending, then cancel it.
        app.state.worker.stop()
        jid = app.state.job_store.enqueue("never", {"x": 1}, owner_user_id=1)
        r = client.post(f"/api/jobs/{jid}/cancel", headers={"X-Auth-Token": token})
        assert r.status_code == 200
        assert r.json()["outcome"] == "cancelled"
        assert app.state.job_store.get(jid)["state"] == "cancelled"
        # Restart worker; the cancelled job must not run.
        app.state.worker.start()
        time.sleep(0.3)
        assert ran == []


def test_re_enqueue_then_runs(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    done = []

    @app.state.worker.handler("rerun")
    def _rerun(payload, progress_cb):
        done.append(payload)
        return {"ok": True}

    with TestClient(app) as client:
        token = _login(client)
        src = app.state.job_store.enqueue("rerun", {"n": 1}, owner_user_id=1)
        app.state.job_store.conn.execute("UPDATE jobs SET state='interrupted' WHERE id=?", (src,))
        app.state.job_store.conn.commit()
        r = client.post(f"/api/jobs/{src}/re-enqueue", headers={"X-Auth-Token": token})
        new_id = int(r.json()["job_id"])
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if app.state.job_store.get(new_id)["state"] == "succeeded":
                break
            time.sleep(0.03)
        assert app.state.job_store.get(new_id)["state"] == "succeeded"
        assert done == [{"n": 1}]
```

- [ ] **Step 2: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_jobs_completion.py -v
```

- [ ] **Step 3: Full suite, clean**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: all green, zero flaky reruns. If `tests/test_job_worker.py` or `tests/test_job_store.py` flake, investigate before declaring done — the cancel checks add one extra `is_cancel_requested` read per job, which must not race.

- [ ] **Step 4: Grep enforcement**

```powershell
$env:PYTHONPATH = "."; python -c "
import pathlib, re
app = pathlib.Path('document_search/app.py').read_text(encoding='utf-8')
assert 'ai_jobs' not in app, 'ai_jobs dict should be fully removed'
assert 'pull_model_stream' in pathlib.Path('document_search/services/ai_organizer.py').read_text(encoding='utf-8')
print('OK')
"
```

Expected: `OK`. (Confirms the in-memory pull path is gone and the streaming helper exists.)

- [ ] **Step 5: Commit**

```powershell
git add tests/test_app_jobs_completion.py
git commit -m @'
test(jobs): end-to-end cancel-pending and re-enqueue-then-run coverage

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Definition of Done

- [ ] `$env:PYTHONPATH = "."; pytest -q` is green with zero regressions; new files `tests/test_job_cancellation.py` (14) and `tests/test_app_jobs_completion.py` (≥ 11) all pass.
- [ ] `jobs` table allows the `cancelled` state and has a `cancel_requested` column; a legacy (pre-cancellation) DB is migrated by table-rebuild without data loss (covered by `test_migration_from_old_constraint_preserves_rows_and_relaxes_check`).
- [ ] `JobStore` exposes `request_cancel`, `is_cancel_requested`, `mark_cancelled`, `re_enqueue`; `claim_next` never runs a cancel-requested pending job.
- [ ] `Worker` exposes `is_cancelled(job_id)`; `JobCancelled` (raised by a handler or surfaced via the cancel flag) yields a terminal `cancelled` state and is never retried; cancel requested between claim and dispatch is honoured.
- [ ] `ai_pull_model` is gone; `/api/ai/models/pull` enqueues an `ai_pull` job; its handler streams Ollama `/api/pull` progress via `pull_model_stream`; `GET /api/ai/jobs/{id}` returns the legacy `{"status":"pulling|done|error","model","result"}` shape from the persistent row; the in-memory `ai_jobs` dict is deleted.
- [ ] `/api/update/run` writes a persistent `system_update` job finalised (`succeeded`/`failed`) before `update.sh` can replace the process; `/api/update/status` surfaces the persistent outcome after a restart; the job is never auto-resumed.
- [ ] `GET /api/jobs` is owner-scoped (admin sees all); `POST /api/jobs/{id}/cancel` and `POST /api/jobs/{id}/re-enqueue` enforce owner-or-admin (404 for foreign jobs).
- [ ] A `/jobs` page exists with a nav entry on all templates; the table shows kind/state/progress/retry_count, with Cancel on active jobs and Re-enqueue on interrupted/failed/cancelled jobs, polling while jobs are active.
- [ ] Manual smoke tests in Task 7 Step 5 and the grep enforcement in Task 8 Step 4 both print `OK`.

---

## Notes for the executing agent

- **Read the predecessor plan first.** `docs/superpowers/plans/2026-05-17-job-queue.md` documents the locked design (integer ids exposed as strings, `UPDATE ... RETURNING` claim, per-handler `SqliteStore(Path(db_path))`, the `app.state.worker`/`app.state.job_store` wiring, and the `_handle_*` registration block in `create_app`). This plan extends that block — do not duplicate startup wiring.
- **The migration is the riskiest step.** SQLite cannot alter a CHECK constraint, so Task 1 rebuilds the table. Wrap it in an explicit transaction with `PRAGMA foreign_keys=OFF` around the `DROP`/`RENAME` (a `jobs` row can be referenced only via its own FK to `users`, but disabling FK enforcement during the swap avoids a transient integrity error). Always recreate the four indexes after the rename. The `test_migration_from_old_constraint_preserves_rows_and_relaxes_check` test is the guardrail — never weaken it.
- **Cancellation is cooperative, by design.** `Worker.is_cancelled` just reads `cancel_requested`. A handler that makes a single long blocking call (e.g. a non-streaming Ollama POST, or `subprocess.run(update.sh)`) cannot be interrupted mid-call; the cancel takes effect when control returns. The streaming `ai_pull` handler *can* honour cancel between Ollama events — consider adding an `is_cancelled` check inside `_handle_ai_pull`'s loop if you want responsive pull cancellation (optional; not required by the DoD).
- **Why `system_update` is NOT enqueued on the worker.** The update replaces the running process; a worker-dispatched job could be killed before it records its result, and on restart the new process must not re-run `update.sh`. Running it inline in the request thread and finalising the row *before* exec guarantees the outcome is persisted. `claim_next(kinds=["system_update"])` flips it to `running` so a crash mid-update is recorded as `interrupted` by the existing startup hook.
- **ACL returns 404, not 403, for foreign jobs.** This matches the existing `GET /api/index/jobs/{id}` and `GET /api/ai/jobs/{id}` behaviour (no information leak about job existence). Keep it consistent in the new `/api/jobs/{id}/cancel` and `/re-enqueue` routes via `_job_acl_or_404`.
- **`request_cancel` on a pending job is immediate and terminal.** It transitions straight to `cancelled` (the worker never sees it). On a running job it only sets the flag; the worker writes the terminal state. This split avoids the API and the worker racing to write `state`.
- **Frontend reveal quirk.** `showAuthedPanels()` only unhides `#statusPanel`/`#appPanel`/`#configPanel`. The jobs page uses its own `#jobsPanel`, so the `bootstrap` branch must unhide it explicitly (Task 7 Step 4). The polling loop self-terminates when no job is active to avoid an idle interval; re-entering `loadJobs()` after a cancel/re-enqueue restarts it.
- **Test isolation.** All API tests register stub handlers on `app.state.worker` *before* entering the `TestClient` context (registration is just a dict write; the worker poll thread starts on FastAPI `startup`). Registering after `with TestClient(app):` opens is also fine because the poll thread re-reads `self._handlers` each tick — but registering before is clearer. For pending-only assertions (Task 8 Step 1) stop the worker first so the job can't be claimed.
- **Conventional commits.** Every commit is `feat(jobs): ...` or `test(jobs): ...`. Use the PowerShell single-quoted here-string form shown above so the `Co-Authored-By` trailer is preserved verbatim.
