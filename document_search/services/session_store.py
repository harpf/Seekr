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
