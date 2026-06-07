from datetime import UTC, datetime
from pathlib import Path

import pytest

from document_search.index.sqlite_store import SqliteStore
from document_search.models import ContentBlock, ExtractionResult, FileFingerprint

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from document_search.app import create_app  # noqa: E402


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


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    # Hermetic: uploads + db inside tmp_path. DOCUMENT_SEARCH_UPLOAD_ROOT is read at
    # create_app() time, so set it before constructing the app.
    monkeypatch.setenv("DOCUMENT_SEARCH_UPLOAD_ROOT", str(tmp_path / "uploads"))
    monkeypatch.chdir(tmp_path)
    # create_app mounts static/templates via relative paths resolved against cwd,
    # so provide those directories inside the hermetic tmp_path.
    (tmp_path / "document_search" / "web" / "static").mkdir(parents=True, exist_ok=True)
    (tmp_path / "document_search" / "web" / "templates").mkdir(parents=True, exist_ok=True)
    app = create_app(str(tmp_path / "index.db"))
    return TestClient(app), tmp_path


def _admin_headers(client):
    r = client.post("/api/login", json={"username": "admin", "password": "admin"})
    return {"X-Auth-Token": r.json()["token"]}


def _make_second_user(client, admin_headers, username="bob", password="bob-password"):
    r = client.post(
        "/api/users",
        json={"username": username, "password": password, "role": "user"},
        headers=admin_headers,
    )
    assert r.status_code in (200, 201), r.text
    r = client.post("/api/login", json={"username": username, "password": password})
    return {"X-Auth-Token": r.json()["token"]}


def test_upload_sets_uploader_as_owner(app_client):
    client, _ = app_client
    admin = _admin_headers(client)
    files = {"file": ("note.txt", b"hello upload owner test", "text/plain")}
    r = client.post("/api/upload", files=files, headers=admin)
    assert r.status_code == 200, r.text
    doc_id = r.json()["document_id"]
    assert doc_id is not None

    import os
    from pathlib import Path as _Path

    from document_search.index.sqlite_store import SqliteStore
    store = SqliteStore(_Path(os.path.join(os.getcwd(), "index.db")))
    owner = store.conn.execute(
        "SELECT owner_principal_id FROM documents WHERE id=?", (doc_id,)
    ).fetchone()["owner_principal_id"]
    admin_pid = store.conn.execute(
        "SELECT principal_id FROM users WHERE username='admin'"
    ).fetchone()["principal_id"]
    assert owner == admin_pid


def test_crawl_default_owner_env_sets_owner(tmp_path, monkeypatch):
    """When SEEKR_DEFAULT_OWNER_PRINCIPAL names a principal external_id, the
    resolver returns its id; unset returns None."""
    from datetime import UTC, datetime

    from document_search.index.sqlite_store import SqliteStore
    store = SqliteStore(tmp_path / "index.db")
    now = datetime.now(tz=UTC).isoformat()
    store.conn.execute(
        "INSERT OR IGNORE INTO principals(type, external_id, display_name, created_at) "
        "VALUES('group','staff','Staff',?)",
        (now,),
    )
    store.conn.commit()
    monkeypatch.setenv("SEEKR_DEFAULT_OWNER_PRINCIPAL", "staff")

    from document_search.app import _resolve_default_owner_principal_id
    pid = _resolve_default_owner_principal_id(store)
    expected = store.conn.execute(
        "SELECT id FROM principals WHERE type='group' AND external_id='staff'"
    ).fetchone()["id"]
    assert pid == expected


def test_crawl_default_owner_unset_is_none(tmp_path, monkeypatch):
    from document_search.index.sqlite_store import SqliteStore
    store = SqliteStore(tmp_path / "index.db")
    monkeypatch.delenv("SEEKR_DEFAULT_OWNER_PRINCIPAL", raising=False)
    from document_search.app import _resolve_default_owner_principal_id
    assert _resolve_default_owner_principal_id(store) is None


def _login(client, username, password):
    r = client.post("/api/login", json={"username": username, "password": password})
    return {"X-Auth-Token": r.json()["token"]}


def _seed_private_doc_for_admin(client, tmp_path):
    """Upload one doc as admin, then replace its public ACL with an explicit
    admin-only read grant so only admin sees it.

    Note: stripping the public ACL alone is not enough — SqliteStore._backfill_acl
    re-grants public read to any document that has *no* ACL rows on the next store
    construction (TestClient dispatches requests across worker threads, each
    building a fresh thread-local store). Leaving an explicit owner-principal ACL
    keeps the document private and stable.
    """
    admin = _admin_headers(client)
    files = {"file": ("admin-only.txt", b"private admin content", "text/plain")}
    r = client.post("/api/upload", files=files, headers=admin)
    doc_id = r.json()["document_id"]
    import os
    from datetime import UTC, datetime
    from pathlib import Path as _Path

    from document_search.index.sqlite_store import SqliteStore
    store = SqliteStore(_Path(os.path.join(os.getcwd(), "index.db")))
    public_id = store.conn.execute(
        "SELECT id FROM principals WHERE type='group' AND external_id='public'"
    ).fetchone()["id"]
    admin_pid = store.conn.execute(
        "SELECT principal_id FROM users WHERE username='admin'"
    ).fetchone()["principal_id"]
    store.conn.execute(
        "DELETE FROM document_acl WHERE document_id=? AND principal_id=?",
        (doc_id, public_id),
    )
    store.conn.execute(
        "INSERT OR IGNORE INTO document_acl(document_id, principal_id, permission, granted_at) "
        "VALUES(?,?, 'read', ?)",
        (doc_id, admin_pid, datetime.now(tz=UTC).isoformat()),
    )
    store.conn.commit()
    return doc_id


def test_status_count_is_acl_filtered(app_client):
    client, tmp_path = app_client
    admin = _admin_headers(client)
    bob = _make_second_user(client, admin)
    _seed_private_doc_for_admin(client, tmp_path)

    admin_status = client.get("/api/status", headers=admin).json()
    bob_status = client.get("/api/status", headers=bob).json()
    assert admin_status["documents"] == 1
    assert bob_status["documents"] == 0


def test_files_open_forbidden_for_non_reader(app_client):
    client, tmp_path = app_client
    admin = _admin_headers(client)
    bob = _make_second_user(client, admin)
    doc_id = _seed_private_doc_for_admin(client, tmp_path)

    r_admin = client.get(f"/api/files/open?document_id={doc_id}", headers=admin)
    assert r_admin.status_code == 200, r_admin.text
    r_bob = client.get(f"/api/files/open?document_id={doc_id}", headers=bob)
    assert r_bob.status_code == 403


def test_mark_forbidden_for_non_reader(app_client):
    client, tmp_path = app_client
    admin = _admin_headers(client)
    bob = _make_second_user(client, admin)
    doc_id = _seed_private_doc_for_admin(client, tmp_path)
    r = client.post(
        "/api/documents/mark",
        json={"document_id": doc_id, "is_marked": True},
        headers=bob,
    )
    assert r.status_code == 403


def test_tags_forbidden_for_non_reader(app_client):
    client, tmp_path = app_client
    admin = _admin_headers(client)
    bob = _make_second_user(client, admin)
    doc_id = _seed_private_doc_for_admin(client, tmp_path)
    r = client.post(
        "/api/documents/tags",
        json={"document_id": doc_id, "tags": ["x"]},
        headers=bob,
    )
    assert r.status_code == 403


def test_suggest_structure_sample_is_acl_filtered(tmp_path):
    from datetime import UTC, datetime

    from document_search.app import _sample_documents_for_user
    from document_search.index.sqlite_store import SqliteStore
    store = SqliteStore(tmp_path / "index.db")
    alice = store.create_user("alice", "pw")
    bob = store.create_user("bob", "pw")
    alice_pid = store.get_user_principal_id(alice)
    now = datetime.now(tz=UTC).isoformat()

    for path, owner in [("/d/alice.txt", alice_pid), ("/d/pub.txt", None)]:
        store.conn.execute(
            "INSERT INTO documents(path, filename, extension, file_size, modified_at, "
            "sha256, indexed_at, status, owner_principal_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (path, Path(path).name, ".txt", 1, now, path, now, "ok", owner),
        )
    store.conn.commit()
    # Re-open to backfill public read on both; then make /d/alice.txt owner-only.
    store = SqliteStore(tmp_path / "index.db")
    public_id = store.conn.execute(
        "SELECT id FROM principals WHERE type='group' AND external_id='public'"
    ).fetchone()["id"]
    alice_doc = store.conn.execute("SELECT id FROM documents WHERE path='/d/alice.txt'").fetchone()["id"]
    store.conn.execute(
        "DELETE FROM document_acl WHERE document_id=? AND principal_id=?",
        (alice_doc, public_id),
    )
    # owner-grant so it stays private (backfill only re-publicizes ACL-less docs)
    store.conn.execute(
        "INSERT OR IGNORE INTO document_acl(document_id, principal_id, permission, granted_at) "
        "VALUES(?,?, 'read', ?)",
        (alice_doc, alice_pid, now),
    )
    store.conn.commit()

    alice_paths = {r["path"] for r in _sample_documents_for_user(store, alice, 50)}
    bob_paths = {r["path"] for r in _sample_documents_for_user(store, bob, 50)}
    assert "/d/alice.txt" in alice_paths and "/d/pub.txt" in alice_paths
    assert "/d/alice.txt" not in bob_paths and "/d/pub.txt" in bob_paths
