from datetime import UTC, datetime
from pathlib import Path

import pytest

from document_search.index.sqlite_store import SqliteStore
from document_search.models import ContentBlock, ExtractionResult, FileFingerprint


def _fp(path: Path) -> FileFingerprint:
    return FileFingerprint(
        path=path,
        file_size=10,
        modified_at=datetime.now(tz=UTC),
        sha256="deadbeef",
    )


def _ext(path: Path, text: str = "hello world") -> ExtractionResult:
    return ExtractionResult(
        file_path=path,
        status="ok",
        document_metadata={},
        blocks=[
            ContentBlock(
                block_type="paragraph",
                block_number=1,
                text=text,
                extractor="txt",
                metadata={},
            )
        ],
        error_message=None,
    )


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "test.db")


def test_get_user_principal_id(store):
    uid = store.create_user("alice", "pw")
    pid = store.get_user_principal_id(uid)
    assert pid is not None
    row = store.conn.execute(
        "SELECT type, external_id FROM principals WHERE id=?", (pid,)
    ).fetchone()
    assert row["type"] == "user"
    assert row["external_id"] == "alice"


def test_get_user_principal_id_unknown_user(store):
    assert store.get_user_principal_id(99999) is None


def test_upsert_sets_owner_principal_id(store, tmp_path):
    uid = store.create_user("alice", "pw")
    alice_pid = store.get_user_principal_id(uid)
    doc_path = tmp_path / "owned.txt"
    doc_path.write_text("hello world", encoding="utf-8")
    doc_id = store.upsert_document(_fp(doc_path), _ext(doc_path), owner_principal_id=alice_pid)
    row = store.conn.execute(
        "SELECT owner_principal_id FROM documents WHERE id=?", (doc_id,)
    ).fetchone()
    assert row["owner_principal_id"] == alice_pid


def test_upsert_without_owner_leaves_null_and_public(store, tmp_path):
    doc_path = tmp_path / "crawled.txt"
    doc_path.write_text("hello world", encoding="utf-8")
    doc_id = store.upsert_document(_fp(doc_path), _ext(doc_path))
    row = store.conn.execute(
        "SELECT owner_principal_id FROM documents WHERE id=?", (doc_id,)
    ).fetchone()
    assert row["owner_principal_id"] is None
    public_id = store.conn.execute(
        "SELECT id FROM principals WHERE type='group' AND external_id='public'"
    ).fetchone()["id"]
    acl = store.conn.execute(
        "SELECT 1 FROM document_acl WHERE document_id=? AND principal_id=? AND permission='read'",
        (doc_id, public_id),
    ).fetchone()
    assert acl is not None


def _restrict_to_owner(store, doc_id):
    """Strip the public read ACL so only the owner can see a doc."""
    public_id = store.conn.execute(
        "SELECT id FROM principals WHERE type='group' AND external_id='public'"
    ).fetchone()["id"]
    store.conn.execute(
        "DELETE FROM document_acl WHERE document_id=? AND principal_id=?",
        (doc_id, public_id),
    )
    store.conn.commit()


def test_user_can_read_document_owner(store, tmp_path):
    uid = store.create_user("alice", "pw")
    alice_pid = store.get_user_principal_id(uid)
    p = tmp_path / "a.txt"
    p.write_text("x", encoding="utf-8")
    doc_id = store.upsert_document(_fp(p), _ext(p), owner_principal_id=alice_pid)
    _restrict_to_owner(store, doc_id)
    assert store.user_can_read_document(uid, doc_id) is True


def test_user_cannot_read_others_private_document(store, tmp_path):
    alice = store.create_user("alice", "pw")
    bob = store.create_user("bob", "pw")
    alice_pid = store.get_user_principal_id(alice)
    p = tmp_path / "a.txt"
    p.write_text("x", encoding="utf-8")
    doc_id = store.upsert_document(_fp(p), _ext(p), owner_principal_id=alice_pid)
    _restrict_to_owner(store, doc_id)
    assert store.user_can_read_document(alice, doc_id) is True
    assert store.user_can_read_document(bob, doc_id) is False


def test_user_can_read_public_document(store, tmp_path):
    bob = store.create_user("bob", "pw")
    p = tmp_path / "pub.txt"
    p.write_text("x", encoding="utf-8")
    doc_id = store.upsert_document(_fp(p), _ext(p))  # no owner -> public read
    assert store.user_can_read_document(bob, doc_id) is True


def test_user_can_read_missing_document_is_false(store):
    bob = store.create_user("bob", "pw")
    assert store.user_can_read_document(bob, 999999) is False
