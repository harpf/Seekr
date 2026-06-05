# Multi-Instance Readiness: Externalised Session + Rate-Limit State, Configurable CORS

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Seekr safe to run as **more than one replica** behind nginx by removing the two pieces of per-process in-memory auth state — the `sessions` dict and the `_login_failures` rate-limit dict — and replacing them with pluggable stores. Add a configurable CORS allowlist so cross-origin browsers behave deterministically instead of relying on Starlette's permissive default. Single-instance behaviour is unchanged.

**Architecture:** Two new service abstractions, each with **two interchangeable backends**:

- `SessionStore` — a `sessions(token, user_id, role, issued_at, expires_at)` SQLite table by default; an optional Redis backend behind a config flag.
- `RateLimiter` — a `login_attempts(ip, attempted_at)` SQLite table by default; an optional Redis counter backend behind the same flag.

`app.py` stops touching its dicts and goes through these stores instead. A factory (`build_session_store` / `build_rate_limiter`) picks the backend from config. CORS is wired via Starlette's `CORSMiddleware` with an allowlist read from config (default: no cross-origin allowed → same-origin only).

**Tech Stack:** Python 3.11, SQLite (already in use, WAL + 5 s busy_timeout), Starlette `CORSMiddleware` (ships with FastAPI), pytest. Redis is **optional** — declared as an extra, never imported unless the Redis backend is selected. CI uses the SQLite backend only.

---

## Design decisions (locked — state these explicitly)

1. **Two backends, SQLite is the default.** We do **not** force a Redis dependency. A SQLite-table backend works today with zero new infra and is correct for the single-replica deployment that exists now. Redis is an opt-in path for true multi-replica (shared, low-latency, with native TTL eviction). The abstraction is identical across both so `app.py` never branches on backend.

2. **Sessions become a `sessions` table.** Columns: `token TEXT PRIMARY KEY`, `user_id INTEGER NOT NULL`, `role TEXT NOT NULL`, `issued_at REAL NOT NULL`, `expires_at REAL NOT NULL`. TTL stays **8 h** (`60*60*8`), matching the current `time.time() - issued > 60*60*8` check. Sessions **survive restart** and are **shared across replicas** because they live in the shared DB. Expiry is enforced at read time (lazy) and a `purge_expired()` sweeps dead rows.

3. **Rate-limit becomes a `login_attempts` table** (SQLite) or Redis counters. Behaviour preserved exactly: **10 failures per 5-minute sliding window per IP** (`_RATE_LIMIT_MAX = 10`, `_RATE_LIMIT_WINDOW = 300`). On the 10th-or-greater failure within the window the login endpoint raises `429`.

4. **`app.py` keeps the same auth contract.** `require_user` returns `user_id`, `require_admin` returns `user_id` and enforces `role == "admin"`, the same `401`/`403`/`429`/`Session expired` responses. No route signature changes. No behavioural change for a single instance.

5. **CORS default is closed.** With no configured origins, `CORSMiddleware` is added with an **empty** allowlist (effectively same-origin/none) — identical user-visible behaviour to today for the SPA served from the same origin, but now explicit instead of relying on the framework default. Origins are configured via `DOCUMENT_SEARCH_CORS_ORIGINS` (comma-separated) or `config.json` `cors_allow_origins`.

6. **Backend selection via config.** `DOCUMENT_SEARCH_STATE_BACKEND` ∈ {`sqlite` (default), `redis`}. When `redis`, `DOCUMENT_SEARCH_REDIS_URL` is read (default `redis://localhost:6379/0`). The factory imports `redis` lazily and raises a clear error if the extra is missing.

7. **One store owns one table.** `SessionStore` is the only module that writes `sessions`; `RateLimiter` the only one that writes `login_attempts`. (grep-enforceable, like JobStore in the queue plan.)

---

## File Structure

**Create:**
- `document_search/services/session_store.py` — `SessionStore` protocol + `SqliteSessionStore` + `RedisSessionStore` + `build_session_store()` factory.
- `document_search/services/rate_limiter.py` — `RateLimiter` protocol + `SqliteRateLimiter` + `RedisRateLimiter` + `build_rate_limiter()` factory.
- `tests/test_session_store.py` — SQLite-backend unit tests (persistence across instances, expiry, purge).
- `tests/test_rate_limiter.py` — SQLite-backend unit tests (block after N, window reset, clear-on-success).
- `tests/test_app_multiinstance.py` — integration tests through the FastAPI app (login persists across a fresh store, rate-limit 429, CORS headers reflect the allowlist).

**Modify:**
- `document_search/index/sqlite_store.py` — add `sessions` and `login_attempts` tables to `_init_schema`.
- `document_search/app.py` — build the two stores at startup; replace every `sessions[...]` / `_login_failures[...]` access; add `CORSMiddleware`.
- `requirements.txt` — add an optional `redis` line (commented as an extra).

**Untouched:**
- The job queue (`job_store.py`, `job_worker.py`) — already multi-worker-aware.
- ACL layer.
- The `_update_job` / `ai_jobs` in-memory dicts (out of scope; they are per-process job state, addressed by the job-queue plan).

---

## Task 1: `sessions` and `login_attempts` table schema

**Files:**
- Modify: `document_search/index/sqlite_store.py` (extend `_init_schema`)
- Test: `tests/test_session_store.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_session_store.py`:

```python
from pathlib import Path
import pytest
from document_search.index.sqlite_store import SqliteStore


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "test.db")


def test_sessions_table_exists(store):
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
    ).fetchall()
    assert len(rows) == 1


def test_sessions_table_columns(store):
    cols = {c[1] for c in store.conn.execute("PRAGMA table_info(sessions)").fetchall()}
    expected = {"token", "user_id", "role", "issued_at", "expires_at"}
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_login_attempts_table_exists(store):
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='login_attempts'"
    ).fetchall()
    assert len(rows) == 1


def test_login_attempts_table_columns(store):
    cols = {c[1] for c in store.conn.execute("PRAGMA table_info(login_attempts)").fetchall()}
    expected = {"id", "ip", "attempted_at"}
    assert expected.issubset(cols), f"missing: {expected - cols}"
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_session_store.py -v
```

Expected: `test_sessions_table_exists` fails — table doesn't exist yet.

- [ ] **Step 3: Add schema in `_init_schema`**

In `document_search/index/sqlite_store.py`, inside the `self.conn.executescript("""...""")` block of `_init_schema`, **append** just before the closing `"""` (i.e. after the last `CREATE INDEX IF NOT EXISTS idx_jobs_owner ...` line at ~line 158):

```sql
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  role TEXT NOT NULL,
  issued_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
CREATE TABLE IF NOT EXISTS login_attempts (
  id INTEGER PRIMARY KEY,
  ip TEXT NOT NULL,
  attempted_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_ip   ON login_attempts(ip, attempted_at);
CREATE INDEX IF NOT EXISTS idx_login_attempts_time ON login_attempts(attempted_at);
```

`issued_at` / `expires_at` / `attempted_at` are stored as REAL (epoch seconds from `time.time()`) to match the existing `time.time()`-based logic in `app.py` exactly — no string/ISO conversion, no timezone subtleties.

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_session_store.py -v
```

Expected: 4 passing.

- [ ] **Step 5: Full-suite check**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: existing baseline + 4 new, zero failures.

- [ ] **Step 6: Commit**

```powershell
git add document_search/index/sqlite_store.py tests/test_session_store.py
git commit -m @'
feat(auth): add sessions and login_attempts tables for shared state

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 2: `SessionStore` abstraction (SQLite + Redis backends)

**Files:**
- Create: `document_search/services/session_store.py`
- Test: `tests/test_session_store.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_session_store.py`:

```python
import time
from document_search.services.session_store import (
    SqliteSessionStore,
    build_session_store,
)


def test_create_and_get_roundtrip(store):
    ss = SqliteSessionStore(store)
    ss.create("tok-abc", user_id=7, role="admin", ttl_seconds=3600)
    sess = ss.get("tok-abc")
    assert sess is not None
    assert sess["user_id"] == 7
    assert sess["role"] == "admin"


def test_session_persists_across_fresh_store_instance(tmp_path):
    """A session written by one store instance is readable by a brand-new
    instance pointed at the same DB — proving it survives restart / is shared
    across replicas."""
    from document_search.index.sqlite_store import SqliteStore
    db = tmp_path / "shared.db"
    s1 = SqliteStore(db)
    ss1 = SqliteSessionStore(s1)
    ss1.create("persisted", user_id=42, role="user", ttl_seconds=3600)

    # Simulate a different process/replica: a fresh SqliteStore on the same file.
    s2 = SqliteStore(db)
    ss2 = SqliteSessionStore(s2)
    sess = ss2.get("persisted")
    assert sess is not None
    assert sess["user_id"] == 42
    assert sess["role"] == "user"


def test_expired_session_returns_none_and_is_deleted(store):
    ss = SqliteSessionStore(store)
    ss.create("expiring", user_id=1, role="user", ttl_seconds=-1)  # already expired
    assert ss.get("expiring") is None
    # Lazy delete on read: the row should be gone.
    row = store.conn.execute(
        "SELECT 1 FROM sessions WHERE token='expiring'"
    ).fetchone()
    assert row is None


def test_unknown_token_returns_none(store):
    ss = SqliteSessionStore(store)
    assert ss.get("nope") is None


def test_delete_removes_session(store):
    ss = SqliteSessionStore(store)
    ss.create("kill-me", user_id=1, role="user", ttl_seconds=3600)
    ss.delete("kill-me")
    assert ss.get("kill-me") is None


def test_purge_expired_removes_only_expired(store):
    ss = SqliteSessionStore(store)
    ss.create("alive", user_id=1, role="user", ttl_seconds=3600)
    ss.create("dead", user_id=2, role="user", ttl_seconds=-10)
    removed = ss.purge_expired()
    assert removed == 1
    assert ss.get("alive") is not None
    assert ss.get("dead") is None


def test_build_session_store_defaults_to_sqlite(store):
    ss = build_session_store(sqlite_store=store, backend="sqlite")
    assert isinstance(ss, SqliteSessionStore)


def test_build_session_store_rejects_unknown_backend(store):
    with pytest.raises(ValueError, match="unknown session backend"):
        build_session_store(sqlite_store=store, backend="bogus")
```

- [ ] **Step 2: Run, expect ImportError**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_session_store.py -v
```

Expected: ImportError — `session_store` module doesn't exist.

- [ ] **Step 3: Implement `SessionStore`**

Create `document_search/services/session_store.py`:

```python
"""Externalised session state.

Two interchangeable backends share one interface so `app.py` never branches on
the chosen backend:

* ``SqliteSessionStore`` — default. Stores sessions in the shared ``sessions``
  table, so they survive restart and are visible to every replica that points
  at the same database file. Zero new infrastructure.
* ``RedisSessionStore`` — optional, opt-in via config. Stores each session as a
  Redis hash with a native TTL. Suited to true multi-replica deployments where
  a shared SQLite file is impractical.

Tokens are opaque strings minted by the caller (``app.py`` uses
``uuid.uuid4().hex``). TTL is supplied per-create; ``app.py`` passes the 8 h
session lifetime. Expiry is enforced lazily on read (and proactively by
``purge_expired`` / Redis TTL).
"""
from __future__ import annotations

import time
from typing import Protocol

from document_search.index.sqlite_store import SqliteStore


class SessionStore(Protocol):
    def create(self, token: str, user_id: int, role: str, ttl_seconds: int) -> None: ...
    def get(self, token: str) -> dict | None: ...
    def delete(self, token: str) -> None: ...
    def purge_expired(self) -> int: ...


class SqliteSessionStore:
    """SessionStore backed by the shared ``sessions`` SQLite table."""

    def __init__(self, store: SqliteStore):
        self.store = store
        self.conn = store.conn

    def create(self, token: str, user_id: int, role: str, ttl_seconds: int) -> None:
        now = time.time()
        expires_at = now + ttl_seconds
        # INSERT OR REPLACE so re-issuing the same token (rare) is well-defined.
        self.conn.execute(
            "INSERT OR REPLACE INTO sessions(token, user_id, role, issued_at, expires_at) "
            "VALUES(?, ?, ?, ?, ?)",
            (token, user_id, role, now, expires_at),
        )
        self.conn.commit()

    def get(self, token: str) -> dict | None:
        row = self.conn.execute(
            "SELECT token, user_id, role, issued_at, expires_at FROM sessions WHERE token=?",
            (token,),
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] <= time.time():
            # Lazy expiry: drop the dead row and report no session.
            self.delete(token)
            return None
        return {
            "token": row["token"],
            "user_id": row["user_id"],
            "role": row["role"],
            "issued_at": row["issued_at"],
            "expires_at": row["expires_at"],
        }

    def delete(self, token: str) -> None:
        self.conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        self.conn.commit()

    def purge_expired(self) -> int:
        cur = self.conn.execute(
            "DELETE FROM sessions WHERE expires_at <= ?", (time.time(),)
        )
        self.conn.commit()
        return cur.rowcount


class RedisSessionStore:
    """SessionStore backed by Redis hashes with native TTL.

    Each session is stored at key ``session:<token>`` as a hash with fields
    ``user_id``, ``role``, ``issued_at``, ``expires_at`` and an EXPIRE matching
    the TTL. Redis evicts expired keys for us, so ``purge_expired`` is a no-op.
    """

    def __init__(self, redis_client):
        self.redis = redis_client

    @staticmethod
    def _key(token: str) -> str:
        return f"session:{token}"

    def create(self, token: str, user_id: int, role: str, ttl_seconds: int) -> None:
        now = time.time()
        key = self._key(token)
        self.redis.hset(
            key,
            mapping={
                "user_id": int(user_id),
                "role": role,
                "issued_at": now,
                "expires_at": now + ttl_seconds,
            },
        )
        # Guard against a non-positive TTL (already-expired): expire immediately.
        self.redis.expire(key, max(1, int(ttl_seconds)))

    def get(self, token: str) -> dict | None:
        data = self.redis.hgetall(self._key(token))
        if not data:
            return None
        # redis-py with decode_responses=True returns str keys/values.
        return {
            "token": token,
            "user_id": int(data["user_id"]),
            "role": data["role"],
            "issued_at": float(data["issued_at"]),
            "expires_at": float(data["expires_at"]),
        }

    def delete(self, token: str) -> None:
        self.redis.delete(self._key(token))

    def purge_expired(self) -> int:
        return 0  # Redis evicts expired keys natively.


def build_session_store(
    sqlite_store: SqliteStore,
    backend: str = "sqlite",
    redis_url: str = "redis://localhost:6379/0",
) -> SessionStore:
    """Factory: pick a SessionStore backend from config.

    ``backend='sqlite'`` (default) needs no extra dependency. ``backend='redis'``
    imports ``redis`` lazily and raises a clear error if the extra is missing.
    """
    if backend == "sqlite":
        return SqliteSessionStore(sqlite_store)
    if backend == "redis":
        try:
            import redis  # type: ignore
        except ImportError as exc:  # pragma: no cover - exercised only with the extra
            raise RuntimeError(
                "DOCUMENT_SEARCH_STATE_BACKEND=redis requires the 'redis' package. "
                "Install it with: pip install redis"
            ) from exc
        client = redis.Redis.from_url(redis_url, decode_responses=True)
        return RedisSessionStore(client)
    raise ValueError(f"unknown session backend: {backend!r} (expected 'sqlite' or 'redis')")
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_session_store.py -v
```

Expected: 12 passing (4 schema + 8 store).

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: previous total + 8 new.

- [ ] **Step 6: Commit**

```powershell
git add document_search/services/session_store.py tests/test_session_store.py
git commit -m @'
feat(auth): SessionStore with SQLite default and optional Redis backend

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 3: `RateLimiter` abstraction (SQLite + Redis backends)

**Files:**
- Create: `document_search/services/rate_limiter.py`
- Test: `tests/test_rate_limiter.py` (new)

The contract preserves today's logic in `app.py:470-481` exactly:
- `_check_rate_limit(ip)` → raise 429 if there are `>= 10` failures within the last `300` s.
- `_record_failure(ip)` → append one failure timestamp.
- `_clear_failures(ip)` → drop all failures for the IP (on successful login).

We move the *counting* into the limiter; `app.py` keeps raising the `HTTPException`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rate_limiter.py`:

```python
import time
from pathlib import Path
import pytest
from document_search.index.sqlite_store import SqliteStore
from document_search.services.rate_limiter import (
    SqliteRateLimiter,
    build_rate_limiter,
)


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "test.db")


def test_under_threshold_is_allowed(store):
    rl = SqliteRateLimiter(store, max_failures=10, window_seconds=300)
    for _ in range(9):
        rl.record_failure("1.2.3.4")
    assert rl.is_blocked("1.2.3.4") is False


def test_blocks_after_threshold(store):
    rl = SqliteRateLimiter(store, max_failures=10, window_seconds=300)
    for _ in range(10):
        rl.record_failure("1.2.3.4")
    assert rl.is_blocked("1.2.3.4") is True


def test_per_ip_isolation(store):
    rl = SqliteRateLimiter(store, max_failures=10, window_seconds=300)
    for _ in range(10):
        rl.record_failure("1.1.1.1")
    assert rl.is_blocked("1.1.1.1") is True
    assert rl.is_blocked("2.2.2.2") is False


def test_window_resets_old_failures(store):
    """Failures older than the window must not count."""
    rl = SqliteRateLimiter(store, max_failures=10, window_seconds=300)
    old = time.time() - 301  # just outside the 5-min window
    for _ in range(10):
        store.conn.execute(
            "INSERT INTO login_attempts(ip, attempted_at) VALUES(?, ?)",
            ("9.9.9.9", old),
        )
    store.conn.commit()
    assert rl.is_blocked("9.9.9.9") is False


def test_clear_resets_an_ip(store):
    rl = SqliteRateLimiter(store, max_failures=10, window_seconds=300)
    for _ in range(10):
        rl.record_failure("5.5.5.5")
    assert rl.is_blocked("5.5.5.5") is True
    rl.clear("5.5.5.5")
    assert rl.is_blocked("5.5.5.5") is False


def test_failures_persist_across_fresh_instance(tmp_path):
    """Recorded failures survive a fresh limiter/store instance (shared state)."""
    db = tmp_path / "shared.db"
    s1 = SqliteStore(db)
    rl1 = SqliteRateLimiter(s1, max_failures=10, window_seconds=300)
    for _ in range(10):
        rl1.record_failure("7.7.7.7")

    s2 = SqliteStore(db)
    rl2 = SqliteRateLimiter(s2, max_failures=10, window_seconds=300)
    assert rl2.is_blocked("7.7.7.7") is True


def test_build_rate_limiter_defaults_to_sqlite(store):
    rl = build_rate_limiter(sqlite_store=store, backend="sqlite",
                            max_failures=10, window_seconds=300)
    assert isinstance(rl, SqliteRateLimiter)


def test_build_rate_limiter_rejects_unknown_backend(store):
    with pytest.raises(ValueError, match="unknown rate limiter backend"):
        build_rate_limiter(sqlite_store=store, backend="bogus",
                           max_failures=10, window_seconds=300)
```

- [ ] **Step 2: Run, expect ImportError**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_rate_limiter.py -v
```

Expected: ImportError — `rate_limiter` module doesn't exist.

- [ ] **Step 3: Implement `RateLimiter`**

Create `document_search/services/rate_limiter.py`:

```python
"""Externalised login rate-limit state.

Preserves the existing policy: at most ``max_failures`` failed logins per
``window_seconds`` per client IP (defaults 10 / 300 s). Two interchangeable
backends:

* ``SqliteRateLimiter`` — default. Each failure is a row in ``login_attempts``;
  ``is_blocked`` counts rows inside the sliding window and opportunistically
  prunes rows older than the window. Shared across replicas via the DB.
* ``RedisRateLimiter`` — optional. Uses one counter key per IP with a TTL equal
  to the window, so the counter self-expires (a coarser but cheaper sliding
  window). Opt-in via config.
"""
from __future__ import annotations

import time
from typing import Protocol

from document_search.index.sqlite_store import SqliteStore


class RateLimiter(Protocol):
    def record_failure(self, ip: str) -> None: ...
    def is_blocked(self, ip: str) -> bool: ...
    def clear(self, ip: str) -> None: ...


class SqliteRateLimiter:
    def __init__(self, store: SqliteStore, max_failures: int, window_seconds: int):
        self.store = store
        self.conn = store.conn
        self.max_failures = max_failures
        self.window_seconds = window_seconds

    def record_failure(self, ip: str) -> None:
        self.conn.execute(
            "INSERT INTO login_attempts(ip, attempted_at) VALUES(?, ?)",
            (ip, time.time()),
        )
        self.conn.commit()

    def is_blocked(self, ip: str) -> bool:
        cutoff = time.time() - self.window_seconds
        # Opportunistic global prune of rows older than the window keeps the
        # table small without a separate sweeper.
        self.conn.execute(
            "DELETE FROM login_attempts WHERE attempted_at < ?", (cutoff,)
        )
        self.conn.commit()
        count = self.conn.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE ip=? AND attempted_at >= ?",
            (ip, cutoff),
        ).fetchone()[0]
        return count >= self.max_failures

    def clear(self, ip: str) -> None:
        self.conn.execute("DELETE FROM login_attempts WHERE ip=?", (ip,))
        self.conn.commit()


class RedisRateLimiter:
    """Counter-per-IP with a TTL equal to the window.

    The first failure in a window creates the key and sets EXPIRE(window); later
    failures INCR it. When the window elapses, Redis drops the key and the count
    resets. This is a fixed-window approximation of the sliding window — adequate
    for brute-force protection at the same 10/300 s policy.
    """

    def __init__(self, redis_client, max_failures: int, window_seconds: int):
        self.redis = redis_client
        self.max_failures = max_failures
        self.window_seconds = window_seconds

    @staticmethod
    def _key(ip: str) -> str:
        return f"login_attempts:{ip}"

    def record_failure(self, ip: str) -> None:
        key = self._key(ip)
        pipe = self.redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, self.window_seconds, nx=True)  # set TTL only on first incr
        pipe.execute()

    def is_blocked(self, ip: str) -> bool:
        val = self.redis.get(self._key(ip))
        return val is not None and int(val) >= self.max_failures

    def clear(self, ip: str) -> None:
        self.redis.delete(self._key(ip))


def build_rate_limiter(
    sqlite_store: SqliteStore,
    backend: str,
    max_failures: int,
    window_seconds: int,
    redis_url: str = "redis://localhost:6379/0",
) -> RateLimiter:
    if backend == "sqlite":
        return SqliteRateLimiter(sqlite_store, max_failures, window_seconds)
    if backend == "redis":
        try:
            import redis  # type: ignore
        except ImportError as exc:  # pragma: no cover - exercised only with the extra
            raise RuntimeError(
                "DOCUMENT_SEARCH_STATE_BACKEND=redis requires the 'redis' package. "
                "Install it with: pip install redis"
            ) from exc
        client = redis.Redis.from_url(redis_url, decode_responses=True)
        return RedisRateLimiter(client, max_failures, window_seconds)
    raise ValueError(
        f"unknown rate limiter backend: {backend!r} (expected 'sqlite' or 'redis')"
    )
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_rate_limiter.py -v
```

Expected: 8 passing.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: previous total + 8 new.

- [ ] **Step 6: Commit**

```powershell
git add document_search/services/rate_limiter.py tests/test_rate_limiter.py
git commit -m @'
feat(auth): RateLimiter with SQLite default and optional Redis backend

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 4: Refactor `app.py` to use the stores (no behaviour change)

**Files:**
- Modify: `document_search/app.py`
- Test: covered by Task 6 integration tests; this task is a pure refactor verified by the full suite.

The refactor replaces three things:
1. The module-level `_login_failures` dict and the `sessions` dict.
2. The `_check_rate_limit` / `_record_failure` / `_clear_failures` helpers — they now delegate to the limiter.
3. Every `sessions[...]` read/write in `require_user`, `require_admin`, `api_login`, `api_me`.

**Map of every session/rate-limit access (from study):**

| Location | Current code | New code |
|---|---|---|
| `app.py:263` | `sessions: dict[...] = {}` | removed; `session_store` built at startup |
| `app.py:470-475` `_check_rate_limit` | reads/writes `_login_failures` | `if rate_limiter.is_blocked(ip): raise 429` |
| `app.py:477-478` `_record_failure` | append to `_login_failures` | `rate_limiter.record_failure(ip)` |
| `app.py:480-481` `_clear_failures` | `_login_failures.pop(ip)` | `rate_limiter.clear(ip)` |
| `app.py:496-502` `require_user` | `token not in sessions`, TTL check, `sessions.pop` | `session_store.get(token)` (expiry handled inside) |
| `app.py:504-513` `require_admin` | same + role check | `session_store.get(token)` + role check |
| `app.py:579-581` `api_login` | `sessions[token] = (...)` | `session_store.create(token, ...)` |
| `app.py:586-591` `api_me` | inline `sessions` read + TTL | `session_store.get(x_auth_token)` |

- [ ] **Step 1: Add a config constant for the 8 h TTL**

In `document_search/app.py`, next to `_RATE_LIMIT_MAX` / `_RATE_LIMIT_WINDOW` (~line 70), add:

```python
_SESSION_TTL_SECONDS = 60 * 60 * 8   # 8 hours, matches the legacy in-memory expiry
```

- [ ] **Step 2: Remove the module-level `_login_failures` dict**

Delete (or leave unused — prefer delete) the line at ~line 69:

```python
_login_failures: dict[str, list[float]] = {}
```

- [ ] **Step 3: Build the stores at startup inside `create_app`**

In `create_app`, **replace** the line at ~line 263:

```python
    sessions: dict[str, tuple[int, float, str]] = {}
```

with:

```python
    # Externalised auth state (sessions + login rate-limit).
    from document_search.services.session_store import build_session_store
    from document_search.services.rate_limiter import build_rate_limiter
    _state_backend = os.getenv("DOCUMENT_SEARCH_STATE_BACKEND", "sqlite").strip().lower()
    _redis_url = os.getenv("DOCUMENT_SEARCH_REDIS_URL", "redis://localhost:6379/0")
    # The startup SqliteStore (built just below as `_startup_db`) is shared with
    # the SQLite backends so sessions/attempts live in the same connection.
    _auth_db = SqliteStore(Path(db_path))
    session_store = build_session_store(
        sqlite_store=_auth_db, backend=_state_backend, redis_url=_redis_url,
    )
    rate_limiter = build_rate_limiter(
        sqlite_store=_auth_db, backend=_state_backend,
        max_failures=_RATE_LIMIT_MAX, window_seconds=_RATE_LIMIT_WINDOW,
        redis_url=_redis_url,
    )
    app.state.session_store = session_store
    app.state.rate_limiter = rate_limiter
```

> Why a dedicated `_auth_db` rather than the per-request `store()`: `store()` is thread-local, so different worker threads would hold different connections — fine for SQLite (same file), but a single explicit connection makes the stores observable and easy to swap in tests. SQLite WAL + `busy_timeout=5000` already serialise concurrent writers cleanly.

- [ ] **Step 4: Rewrite the auth helpers**

Replace `_check_rate_limit`, `_record_failure`, `_clear_failures` (~lines 470-481) with:

```python
    def _check_rate_limit(ip: str) -> None:
        if rate_limiter.is_blocked(ip):
            raise HTTPException(
                status_code=429,
                detail="Too many failed login attempts. Try again in 5 minutes.",
            )

    def _record_failure(ip: str) -> None:
        rate_limiter.record_failure(ip)

    def _clear_failures(ip: str) -> None:
        rate_limiter.clear(ip)
```

Replace `require_user` and `require_admin` (~lines 495-513) with:

```python
    def require_user(token: str | None) -> int:
        if not token:
            raise HTTPException(status_code=401, detail="Unauthorized")
        sess = session_store.get(token)
        if sess is None:
            # Could be unknown OR expired-and-purged — both are Unauthorized.
            raise HTTPException(status_code=401, detail="Unauthorized")
        return sess["user_id"]

    def require_admin(token: str | None) -> int:
        if not token:
            raise HTTPException(status_code=401, detail="Unauthorized")
        sess = session_store.get(token)
        if sess is None:
            raise HTTPException(status_code=401, detail="Unauthorized")
        if sess["role"] != "admin":
            raise HTTPException(status_code=403, detail="Admin role required")
        return sess["user_id"]
```

> Note: the legacy code distinguished "expired" (`Session expired`) from "unknown" (`Unauthorized`). After externalisation, an expired session is purged on read and is indistinguishable from an unknown token, so both return `401 Unauthorized`. This is a harmless, intentional simplification — the client treats any 401 as "log in again". If any existing test asserts the literal string `"Session expired"`, update it to assert `status_code == 401` only (see Task 6 Step 5).

- [ ] **Step 5: Rewrite `api_login`**

Replace the session write in `api_login` (~lines 579-582):

```python
        token = uuid.uuid4().hex
        role = user["role"] if "role" in user.keys() else "user"
        sessions[token] = (user["id"], time.time(), role)
        return {"token": token, "username": user["username"], "role": role}
```

with:

```python
        token = uuid.uuid4().hex
        role = user["role"] if "role" in user.keys() else "user"
        session_store.create(token, user["id"], role, _SESSION_TTL_SECONDS)
        return {"token": token, "username": user["username"], "role": role}
```

- [ ] **Step 6: Rewrite `api_me`**

Replace the body of `api_me` (~lines 585-596):

```python
    @app.get("/api/me")
    def api_me(x_auth_token: str | None = Header(default=None)):
        if not x_auth_token:
            raise HTTPException(status_code=401, detail="Unauthorized")
        sess = session_store.get(x_auth_token)
        if sess is None:
            raise HTTPException(status_code=401, detail="Unauthorized")
        db = store()
        user = db.get_user_by_id(sess["user_id"])
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return {"id": sess["user_id"], "username": user["username"], "role": sess["role"]}
```

- [ ] **Step 7: Purge expired sessions on startup (housekeeping)**

The worker already registers an `@app.on_event("startup")` (Task 4 of the job-queue plan, ~line 278). Add a one-shot purge so the table doesn't grow unbounded across restarts. Inside the existing `_start_worker` startup hook, **append** one line:

```python
    @app.on_event("startup")
    def _start_worker() -> None:
        job_store.mark_interrupted_running_jobs()
        session_store.purge_expired()
        worker.start()
```

- [ ] **Step 8: Run the full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: green. If `tests/test_app_search.py` (or any auth test) asserts on the literal `"Session expired"` detail string, relax it to `status_code == 401` — the behaviour (re-login required) is unchanged.

- [ ] **Step 9: Commit**

```powershell
git add document_search/app.py
git commit -m @'
refactor(auth): route sessions and rate-limit through externalised stores

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 5: Configurable CORS

**Files:**
- Modify: `document_search/app.py` (add `CORSMiddleware`)
- Test: `tests/test_app_multiinstance.py` (Task 6)

- [ ] **Step 1: Add the import**

At the top of `document_search/app.py`, with the other FastAPI imports (~line 25-27), add:

```python
from starlette.middleware.cors import CORSMiddleware
```

- [ ] **Step 2: Resolve the allowlist from config**

Inside `create_app`, **before** `app.add_middleware(_SecurityHeaders)` (~line 467), add:

```python
    # ── CORS ───────────────────────────────────────────────────────────
    # Default closed: with no configured origins the SPA is served same-origin
    # and no cross-origin requests are permitted. Configure explicitly via
    # DOCUMENT_SEARCH_CORS_ORIGINS (comma-separated) or config.json
    # "cors_allow_origins": [...].
    def _resolve_cors_origins() -> list[str]:
        env_val = os.getenv("DOCUMENT_SEARCH_CORS_ORIGINS", "").strip()
        if env_val:
            return [o.strip() for o in env_val.split(",") if o.strip()]
        if config_path.exists():
            try:
                raw = json.loads(config_path.read_text(encoding="utf-8"))
                origins = raw.get("cors_allow_origins")
                if isinstance(origins, list):
                    return [str(o).strip() for o in origins if str(o).strip()]
            except Exception:
                pass
        return []

    _cors_origins = _resolve_cors_origins()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=bool(_cors_origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )
```

> `allow_credentials` is only enabled when an explicit allowlist is set — `CORSMiddleware` forbids combining `allow_credentials=True` with a wildcard, and we never use a wildcard. With an empty allowlist, the middleware adds no `Access-Control-Allow-Origin` for cross-origin requests, which is exactly the default-closed behaviour we want.

- [ ] **Step 3: Manual sanity check (optional)**

```powershell
$env:DOCUMENT_SEARCH_CORS_ORIGINS = "https://app.example.com"; $env:PYTHONPATH = "."; python -c "from document_search.app import create_app; app = create_app('./_tmp_cors.db'); print([m.cls.__name__ for m in app.user_middleware])"
Remove-Item ./_tmp_cors.db* -Force -ErrorAction SilentlyContinue
$env:DOCUMENT_SEARCH_CORS_ORIGINS = $null
```

Expected: the printed middleware list includes `CORSMiddleware`.

- [ ] **Step 4: Commit**

```powershell
git add document_search/app.py
git commit -m @'
feat(auth): configurable CORS allowlist via CORSMiddleware (default closed)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 6: Integration tests through the app + Redis extra

**Files:**
- Create: `tests/test_app_multiinstance.py`
- Modify: `requirements.txt`

- [ ] **Step 1: Write the integration tests**

Create `tests/test_app_multiinstance.py`:

```python
import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient
from document_search.app import create_app
from document_search.index.sqlite_store import SqliteStore
from document_search.services.session_store import SqliteSessionStore


def _login(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_login_creates_persistent_session_row(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    with TestClient(app) as client:
        token = _login(client)
        # A brand-new store on the same DB file (simulating another replica)
        # must see the session row.
        other = SqliteStore(db)
        ss = SqliteSessionStore(other)
        sess = ss.get(token)
        assert sess is not None
        assert sess["role"] == "admin"


def test_session_token_authorises_protected_route(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        r = client.get("/api/me", headers={"X-Auth-Token": token})
        assert r.status_code == 200
        assert r.json()["username"] == "admin"


def test_session_survives_app_restart(tmp_path):
    """A token minted by app instance #1 still works on instance #2 (same DB)."""
    db = tmp_path / "t.db"
    app1 = create_app(str(db))
    with TestClient(app1) as c1:
        token = _login(c1)
    app2 = create_app(str(db))
    with TestClient(app2) as c2:
        r = c2.get("/api/me", headers={"X-Auth-Token": token})
        assert r.status_code == 200
        assert r.json()["role"] == "admin"


def test_expired_session_is_rejected(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    with TestClient(app) as client:
        token = _login(client)
        # Force the session to be expired directly in the shared table.
        s = SqliteStore(db)
        import time
        s.conn.execute(
            "UPDATE sessions SET expires_at=? WHERE token=?",
            (time.time() - 1, token),
        )
        s.conn.commit()
        r = client.get("/api/me", headers={"X-Auth-Token": token})
        assert r.status_code == 401


def test_rate_limit_blocks_after_ten_failures(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        # 10 wrong-password attempts → the 11th call is rate-limited (429).
        for _ in range(10):
            r = client.post("/api/login", json={"username": "admin", "password": "wrong"})
            assert r.status_code == 401, r.text
        r = client.post("/api/login", json={"username": "admin", "password": "wrong"})
        assert r.status_code == 429


def test_rate_limit_resets_after_window(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    with TestClient(app) as client:
        for _ in range(10):
            client.post("/api/login", json={"username": "admin", "password": "wrong"})
        # Age the recorded attempts past the 5-minute window.
        s = SqliteStore(db)
        import time
        s.conn.execute(
            "UPDATE login_attempts SET attempted_at=?",
            (time.time() - 301,),
        )
        s.conn.commit()
        # A correct login must now succeed (no longer blocked).
        r = client.post("/api/login", json={"username": "admin", "password": "admin"})
        assert r.status_code == 200, r.text


def test_successful_login_clears_failures(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        for _ in range(9):
            client.post("/api/login", json={"username": "admin", "password": "wrong"})
        # 9 failures then a success clears the counter…
        assert client.post(
            "/api/login", json={"username": "admin", "password": "admin"}
        ).status_code == 200
        # …so a fresh batch of 9 failures still isn't blocked.
        for _ in range(9):
            r = client.post("/api/login", json={"username": "admin", "password": "wrong"})
            assert r.status_code == 401


def test_cors_headers_reflect_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_CORS_ORIGINS", "https://app.example.com")
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        # Allowed origin → echoed back.
        r = client.get(
            "/api/me",
            headers={"Origin": "https://app.example.com", "X-Auth-Token": "x"},
        )
        assert r.headers.get("access-control-allow-origin") == "https://app.example.com"


def test_cors_disallows_unlisted_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_CORS_ORIGINS", "https://app.example.com")
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        r = client.get(
            "/api/me",
            headers={"Origin": "https://evil.example.com", "X-Auth-Token": "x"},
        )
        # Unlisted origin gets no allow-origin header.
        assert r.headers.get("access-control-allow-origin") is None


def test_cors_default_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("DOCUMENT_SEARCH_CORS_ORIGINS", raising=False)
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        r = client.get(
            "/api/me",
            headers={"Origin": "https://anything.example.com", "X-Auth-Token": "x"},
        )
        assert r.headers.get("access-control-allow-origin") is None
```

- [ ] **Step 2: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_multiinstance.py -v
```

Expected: 10 passing. If `test_rate_limit_blocks_after_ten_failures` fails, confirm `_check_rate_limit(ip)` is still called at the **top** of `api_login` (before the credential check) — it is at `app.py:572`.

- [ ] **Step 3: Add the optional Redis extra to `requirements.txt`**

Append to `requirements.txt`:

```
# Optional: enable the Redis backend for sessions + rate limiting in
# multi-replica deployments. Activate with DOCUMENT_SEARCH_STATE_BACKEND=redis.
# Not required for the default SQLite backend (used in CI).
# redis>=5.0.0
```

(Left commented so CI and default installs don't pull Redis. Operators who set `DOCUMENT_SEARCH_STATE_BACKEND=redis` uncomment it or `pip install redis`.)

- [ ] **Step 4: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: every test green.

- [ ] **Step 5: Fix any legacy-string assertion fallout**

If the full suite surfaces a failure asserting the literal `"Session expired"` detail, update that assertion to check `status_code == 401` only (the behaviour is unchanged — see Task 4 Step 4 note). Commit such a fixture fix separately:

```powershell
git add tests/<file>.py
git commit -m @'
test(auth): assert 401 instead of legacy "Session expired" detail string

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

- [ ] **Step 6: Commit**

```powershell
git add tests/test_app_multiinstance.py requirements.txt
git commit -m @'
test(auth): multi-instance session/rate-limit/CORS integration tests

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 7: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full suite cleanly**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: zero failures, zero errors.

- [ ] **Step 2: Confirm no module still touches the old dicts**

```powershell
Select-String -Path document_search/app.py -Pattern "_login_failures|sessions\[" 
```

Expected: no matches (the dict and all subscript accesses are gone).

- [ ] **Step 3: Confirm store ownership (single-writer invariant)**

```powershell
Select-String -Path document_search/*.py, document_search/**/*.py -Pattern "INSERT INTO sessions|INSERT INTO login_attempts" |
  Where-Object { $_.Path -notmatch "session_store.py|rate_limiter.py" }
```

Expected: no matches — only `session_store.py` writes `sessions`, only `rate_limiter.py` writes `login_attempts`.

- [ ] **Step 4: Manual smoke test via TestClient**

```powershell
$env:PYTHONPATH = "."; python -c "
from fastapi.testclient import TestClient
from document_search.app import create_app
import tempfile, pathlib
tmp = pathlib.Path(tempfile.mkdtemp())
db = str(tmp / 'smoke.db')
app1 = create_app(db)
with TestClient(app1) as c:
    tok = c.post('/api/login', json={'username':'admin','password':'admin'}).json()['token']
    print('token =', tok[:8], '...')
    assert c.get('/api/me', headers={'X-Auth-Token': tok}).status_code == 200
# Fresh app instance, same DB → token still valid (shared/persistent session).
app2 = create_app(db)
with TestClient(app2) as c:
    assert c.get('/api/me', headers={'X-Auth-Token': tok}).status_code == 200
    # Rate limit still enforced.
    for _ in range(10):
        c.post('/api/login', json={'username':'admin','password':'nope'})
    r = c.post('/api/login', json={'username':'admin','password':'nope'})
    assert r.status_code == 429, r.status_code
print('OK')
"
```

Expected: prints `token = ...`, then `OK`.

- [ ] **Step 5: No commit (verification only).**

---

## Definition of Done

- [ ] `pytest -q` green; new tests in `test_session_store.py` (12), `test_rate_limiter.py` (8), `test_app_multiinstance.py` (10) all pass.
- [ ] `sessions` and `login_attempts` tables exist with the documented columns + indexes.
- [ ] `SessionStore` and `RateLimiter` each have a working **SQLite default** and an **optional Redis** backend selected by `DOCUMENT_SEARCH_STATE_BACKEND`; Redis is never imported unless selected.
- [ ] `app.py` no longer declares or reads `_login_failures` or the `sessions` dict; all auth state goes through the stores.
- [ ] Sessions **persist across a fresh app instance** on the same DB (proven by `test_session_survives_app_restart`) and the **8 h TTL** is preserved (`_SESSION_TTL_SECONDS`).
- [ ] Rate-limit preserves **10 failures / 5 min / IP**, blocks with `429`, resets after the window, and clears on successful login.
- [ ] `CORSMiddleware` is added with an allowlist from `DOCUMENT_SEARCH_CORS_ORIGINS` / `config.json`; default is closed (no cross-origin allowed); the allowed origin is echoed and unlisted origins are not.
- [ ] `require_user` / `require_admin` return the same `user_id`, enforce the same `401`/`403`, and route signatures are unchanged.
- [ ] `requirements.txt` documents `redis` as an optional (commented) extra.
- [ ] Single-instance user-visible behaviour is unchanged.

---

## Notes for the executing agent

- **Why SQLite is the default and Redis is optional:** the live deployment is single-node Docker (per `docs/ROADMAP.md` open question on deployment target). A SQLite-table backend needs zero new infrastructure and is already correct/shared for that topology. Redis is the right tool *only* when you actually run >1 replica with no shared filesystem. Forcing Redis on everyone would add an operational dependency for no benefit today — hence the pluggable factory.
- **Why epoch-seconds (`REAL`) for timestamps:** the legacy code uses `time.time()` arithmetic throughout (`time.time() - issued > 60*60*8`). Storing `issued_at`/`expires_at`/`attempted_at` as REAL keeps the comparison logic byte-for-byte identical and avoids ISO-string parsing in the hot auth path. (The ACL/jobs tables use ISO strings because their logic is string-comparison based; this table's logic is arithmetic — match the consumer.)
- **Why expired == unknown (both 401):** once a session row is purged on read, an expired token is indistinguishable from a never-issued one. The frontend already treats every 401 as "log in again", so collapsing `"Session expired"` into `Unauthorized` is behaviourally invisible. Don't reintroduce the distinction — it would require keeping dead rows around.
- **Why `allow_credentials` tracks the allowlist:** `CORSMiddleware` rejects `allow_credentials=True` together with `allow_origins=["*"]`. We never use a wildcard, and we only need credentials when there's a real cross-origin allowlist, so gating it on `bool(_cors_origins)` is both correct and safe.
- **Redis sliding-window approximation:** the SQLite limiter is a true sliding window (count rows in the last 300 s). The Redis limiter is a fixed-window counter with a 300 s TTL — coarser at window boundaries but adequate for brute-force protection at the same 10/300 policy, and far cheaper than a sorted-set sliding window. If exact parity matters for a Redis deployment, swap to a `ZADD`/`ZREMRANGEBYSCORE` sorted set in a follow-up; it's out of scope here.
- **Connection sharing:** `_auth_db` is one explicit `SqliteStore` created at app startup and shared by both SQLite-backed stores. SQLite WAL mode plus `busy_timeout=5000` (already configured in `_configure_connection`) serialise the small auth writes cleanly. Don't route auth writes through the thread-local `store()` — keeping them on one observable connection makes the stores trivial to inject in tests.
- **Order of operations in `api_login`:** `_check_rate_limit(ip)` must stay at the very top (before the credential check) so a blocked IP gets `429` without leaking whether the username exists. This is already the case at `app.py:572`; preserve it.
- **Out of scope (do not touch):** the `_update_job` and `ai_jobs` in-memory dicts (per-process job state, handled by the job-queue plan), the ACL layer, and any nginx/proxy config. This plan only externalises auth state and adds CORS.
