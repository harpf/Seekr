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
