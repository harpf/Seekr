from pathlib import Path
import pytest
from document_search.index.sqlite_store import SqliteStore


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "test.db")


def test_acl_tables_exist(store):
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('principals','user_groups','document_acl')"
    ).fetchall()
    names = {r[0] for r in rows}
    assert names == {"principals", "user_groups", "document_acl"}


def test_documents_has_owner_principal_id(store):
    cols = store.conn.execute("PRAGMA table_info(documents)").fetchall()
    names = {c[1] for c in cols}
    assert "owner_principal_id" in names


def test_users_has_principal_id(store):
    cols = store.conn.execute("PRAGMA table_info(users)").fetchall()
    names = {c[1] for c in cols}
    assert "principal_id" in names
