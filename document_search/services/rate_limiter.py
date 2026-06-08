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
