import json

import pytest

from document_search.index.sqlite_store import SqliteStore


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "test.db")


def test_ai_decisions_table_exists(store):
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_decisions'"
    ).fetchall()
    assert len(rows) == 1


def test_ai_decisions_columns(store):
    cols = {c[1] for c in store.conn.execute("PRAGMA table_info(ai_decisions)").fetchall()}
    expected = {
        "id", "kind", "model", "prompt_sha256", "document_id",
        "output_json", "applied", "user_id", "created_at",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_record_and_list_ai_decision(store):
    did = store.record_ai_decision(
        kind="bulk_tag",
        model="llama3.2",
        prompt_sha256="abc123",
        document_id=None,
        output={"suggested_tags": ["invoice", "2025"]},
        applied=1,
        user_id=7,
    )
    assert isinstance(did, int)
    rows = store.list_ai_decisions(limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "bulk_tag"
    assert row["model"] == "llama3.2"
    assert row["prompt_sha256"] == "abc123"
    assert row["applied"] == 1
    assert row["user_id"] == 7
    assert json.loads(row["output_json"]) == {"suggested_tags": ["invoice", "2025"]}
    assert row["created_at"]  # ISO timestamp present
