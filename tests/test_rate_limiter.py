import time

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
