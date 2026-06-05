from datetime import UTC, datetime
from pathlib import Path

import pytest

from document_search.index.search_service import search
from document_search.index.sqlite_store import SqliteStore


@pytest.fixture
def populated_store(tmp_path):
    store = SqliteStore(tmp_path / "test.db")
    alice_id = store.create_user("alice", "pw")
    bob_id = store.create_user("bob", "pw")
    now = datetime.now(tz=UTC).isoformat()

    # Insert two docs, both indexed in FTS
    for path, sha, body in [
        ("/d/alice-secret.txt", "h1", "secret recipe for soup"),
        ("/d/shared.txt", "h2", "shared recipe for soup"),
    ]:
        cursor = store.conn.execute(
            "INSERT INTO documents(path, filename, extension, file_size, modified_at, sha256, indexed_at, status) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (path, Path(path).name, ".txt", len(body), now, sha, now, "ok"),
        )
        doc_id = cursor.lastrowid
        bcursor = store.conn.execute(
            "INSERT INTO content_blocks(document_id, block_type, block_number, text, extractor, text_length) "
            "VALUES(?,?,?,?,?,?)",
            (doc_id, "paragraph", 1, body, "txt", len(body)),
        )
        store.conn.execute(
            "INSERT INTO content_fts(document_id, block_id, path, filename, extension, block_type, block_number, text) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (doc_id, bcursor.lastrowid, path, Path(path).name, ".txt", "paragraph", "1", body),
        )
    store.conn.commit()
    # Re-open to run backfill so both docs get 'public' read
    store2 = SqliteStore(store.db_path)

    # Restrict alice-secret.txt to alice only
    public_id = store2.conn.execute(
        "SELECT id FROM principals WHERE type='group' AND external_id='public'"
    ).fetchone()["id"]
    secret_id = store2.conn.execute(
        "SELECT id FROM documents WHERE path='/d/alice-secret.txt'"
    ).fetchone()["id"]
    alice_p = store2.conn.execute(
        "SELECT principal_id FROM users WHERE username='alice'"
    ).fetchone()["principal_id"]
    store2.conn.execute(
        "DELETE FROM document_acl WHERE document_id=? AND principal_id=?",
        (secret_id, public_id),
    )
    store2.conn.execute(
        "INSERT INTO document_acl(document_id, principal_id, permission, granted_at) "
        "VALUES(?,?, 'read', ?)",
        (secret_id, alice_p, now),
    )
    store2.conn.commit()
    return store2, alice_id, bob_id


def test_search_returns_only_visible_docs_for_bob(populated_store):
    store, alice_id, bob_id = populated_store
    rows = search(store, "recipe", user_id=bob_id)
    paths = {r["path"] for r in rows}
    assert "/d/shared.txt" in paths
    assert "/d/alice-secret.txt" not in paths


def test_search_returns_both_for_alice(populated_store):
    store, alice_id, bob_id = populated_store
    rows = search(store, "recipe", user_id=alice_id)
    paths = {r["path"] for r in rows}
    assert "/d/shared.txt" in paths
    assert "/d/alice-secret.txt" in paths


def test_search_without_user_id_raises(populated_store):
    store, _, _ = populated_store
    with pytest.raises(ValueError, match="user_id is required"):
        search(store, "recipe")


def test_search_bypass_acl_returns_all(populated_store):
    store, _, _ = populated_store
    rows = search(store, "recipe", user_id=None, bypass_acl=True)
    paths = {r["path"] for r in rows}
    assert "/d/shared.txt" in paths
    assert "/d/alice-secret.txt" in paths


def test_browse_all_respects_acl_for_bob(populated_store):
    """Empty query goes through _browse_all — must also be ACL-filtered."""
    store, alice_id, bob_id = populated_store
    rows = search(store, "", limit=100, user_id=bob_id)
    paths = {r["path"] for r in rows}
    assert "/d/shared.txt" in paths
    assert "/d/alice-secret.txt" not in paths


def test_browse_all_bypass_acl(populated_store):
    store, _, _ = populated_store
    rows = search(store, "", limit=100, user_id=None, bypass_acl=True)
    paths = {r["path"] for r in rows}
    assert "/d/shared.txt" in paths
    assert "/d/alice-secret.txt" in paths


def test_cli_uses_bypass_acl():
    """document_search.main.cmd_search must pass bypass_acl=True to search()."""
    import inspect

    from document_search import main as main_mod
    src = inspect.getsource(main_mod.cmd_search)
    assert "bypass_acl=True" in src, "CLI search must explicitly bypass ACL"


def test_ha_search_uses_bypass_acl():
    """The HA search path bypasses ACL — it's gated by API key + per-key path_filter,
    not by a user identity. This must be explicit so a future audit can grep for it."""
    from pathlib import Path
    src = Path("document_search/app.py").read_text(encoding="utf-8")
    # Find the _ha_search_impl block and assert bypass_acl=True is in it.
    start = src.index("def _ha_search_impl")
    # Look at the next ~30 lines after the def.
    end = src.index("\n    def ", start + 1) if "\n    def " in src[start + 1:] else start + 2000
    snippet = src[start:end]
    assert "bypass_acl=True" in snippet, (
        "_ha_search_impl must explicitly pass bypass_acl=True; see ACL Foundation plan."
    )
