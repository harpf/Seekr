import json

import pytest

from document_search.index.sqlite_store import SqliteStore


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "test.db")
    # Seed the actor users the audit tests reference (ids 1 and 2). The
    # audit_log.actor_user_id FK requires these users to exist while
    # foreign_keys enforcement is ON.
    s.create_user("alice", "alice-password")
    s.create_user("bob", "bob-password")
    return s


def test_audit_log_table_exists(store):
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_log'"
    ).fetchall()
    assert len(rows) == 1


def test_audit_log_columns(store):
    cols = {c[1] for c in store.conn.execute("PRAGMA table_info(audit_log)").fetchall()}
    expected = {
        "id", "actor_user_id", "action", "target_type", "target_id",
        "detail_json", "ip", "created_at",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_audit_log_indexes_exist(store):
    idx = {r[1] for r in store.conn.execute("PRAGMA index_list(audit_log)").fetchall()}
    assert "idx_audit_actor" in idx
    assert "idx_audit_action" in idx
    assert "idx_audit_created" in idx


def test_record_audit_inserts_row(store):
    audit_id = store.record_audit(
        actor_user_id=1,
        action="search",
        target_type="query",
        target_id=None,
        detail={"query": "invoice", "result_count": 3},
        ip="127.0.0.1",
    )
    assert isinstance(audit_id, int)
    row = store.conn.execute("SELECT * FROM audit_log WHERE id=?", (audit_id,)).fetchone()
    assert row["actor_user_id"] == 1
    assert row["action"] == "search"
    assert row["target_type"] == "query"
    assert row["target_id"] is None
    assert json.loads(row["detail_json"]) == {"query": "invoice", "result_count": 3}
    assert row["ip"] == "127.0.0.1"
    assert row["created_at"] is not None


def test_record_audit_null_detail(store):
    audit_id = store.record_audit(
        actor_user_id=2, action="ssl.generate", target_type="ssl",
        target_id=None, detail=None, ip=None,
    )
    row = store.conn.execute("SELECT detail_json FROM audit_log WHERE id=?", (audit_id,)).fetchone()
    assert row["detail_json"] is None


def test_record_audit_coerces_target_id_to_text(store):
    audit_id = store.record_audit(
        actor_user_id=1, action="file.open", target_type="document",
        target_id=42, detail=None, ip=None,
    )
    row = store.conn.execute("SELECT target_id FROM audit_log WHERE id=?", (audit_id,)).fetchone()
    assert row["target_id"] == "42"  # stored as TEXT


def test_list_audit_returns_newest_first(store):
    for i in range(3):
        store.record_audit(actor_user_id=1, action="search", target_type="query",
                           target_id=None, detail={"i": i}, ip=None)
    rows = store.list_audit()
    assert len(rows) == 3
    # Newest (highest id) first
    assert rows[0]["id"] > rows[1]["id"] > rows[2]["id"]
    # Each row carries a decoded `detail` dict for convenience
    assert isinstance(rows[0]["detail"], dict)


def test_list_audit_filters_by_actor(store):
    store.record_audit(actor_user_id=1, action="search", target_type="query", target_id=None, detail=None, ip=None)
    store.record_audit(actor_user_id=2, action="search", target_type="query", target_id=None, detail=None, ip=None)
    rows = store.list_audit(actor_user_id=2)
    assert len(rows) == 1
    assert rows[0]["actor_user_id"] == 2


def test_list_audit_filters_by_action(store):
    store.record_audit(actor_user_id=1, action="search", target_type="query", target_id=None, detail=None, ip=None)
    store.record_audit(actor_user_id=1, action="file.open", target_type="document", target_id="9", detail=None, ip=None)
    rows = store.list_audit(action="file.open")
    assert len(rows) == 1
    assert rows[0]["action"] == "file.open"


def test_list_audit_filters_by_date_range(store):
    # Insert one row with a known old timestamp, one fresh row.
    store.conn.execute(
        "INSERT INTO audit_log(actor_user_id, action, target_type, target_id, detail_json, ip, created_at) "
        "VALUES(1, 'search', 'query', NULL, NULL, NULL, '2020-01-01T00:00:00+00:00')"
    )
    store.conn.commit()
    store.record_audit(actor_user_id=1, action="search", target_type="query", target_id=None, detail=None, ip=None)
    old_only = store.list_audit(date_to="2020-12-31T00:00:00+00:00")
    assert len(old_only) == 1
    assert old_only[0]["created_at"].startswith("2020")
    new_only = store.list_audit(date_from="2021-01-01T00:00:00+00:00")
    assert len(new_only) == 1
    assert not new_only[0]["created_at"].startswith("2020")


def test_list_audit_pagination(store):
    for _ in range(5):
        store.record_audit(actor_user_id=1, action="search", target_type="query", target_id=None, detail=None, ip=None)
    page1 = store.list_audit(limit=2, offset=0)
    page2 = store.list_audit(limit=2, offset=2)
    assert len(page1) == 2 and len(page2) == 2
    assert {r["id"] for r in page1}.isdisjoint({r["id"] for r in page2})


def test_count_audit_respects_filters(store):
    store.record_audit(actor_user_id=1, action="search", target_type="query", target_id=None, detail=None, ip=None)
    store.record_audit(actor_user_id=2, action="upload", target_type="document", target_id="1", detail=None, ip=None)
    assert store.count_audit() == 2
    assert store.count_audit(action="upload") == 1
    assert store.count_audit(actor_user_id=1) == 1
