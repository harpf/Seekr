from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from document_search.app import create_app
from document_search.index.sqlite_store import SqliteStore


def _login(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _seed_doc(db_path: Path, file_path: Path, body: str, ext: str = ".txt",
              blocks: list[str] | None = None, owner_principal_id=None) -> int:
    """Insert a document + content blocks + FTS rows. Returns document_id.

    owner_principal_id=None means the post-open backfill grants 'public' read
    (visible to everyone). Pass a specific principal id to restrict it.
    """
    store = SqliteStore(db_path)
    # Ensure the default admin exists *before* the ACL backfill so that the
    # backfill grants admin its 'public' group membership (ensure_default_admin
    # creates the admin with a NULL principal; the next _backfill_acl wires up the
    # principal + user_groups row). Without this the admin is not a member of the
    # 'public' group and public-read docs are invisible to it.
    store.ensure_default_admin()
    now = datetime.now(tz=UTC).isoformat()
    cur = store.conn.execute(
        "INSERT INTO documents(path, filename, extension, file_size, modified_at, "
        "sha256, indexed_at, status, owner_principal_id) VALUES(?,?,?,?,?,?,?,?,?)",
        (str(file_path), file_path.name, ext, len(body), now, "hash" + file_path.name,
         now, "ok", owner_principal_id),
    )
    doc_id = cur.lastrowid
    for i, btext in enumerate(blocks or [body], start=1):
        bcur = store.conn.execute(
            "INSERT INTO content_blocks(document_id, block_type, block_number, text, "
            "extractor, text_length) VALUES(?,?,?,?,?,?)",
            (doc_id, "paragraph", i, btext, "txt", len(btext)),
        )
        store.conn.execute(
            "INSERT INTO content_fts(document_id, block_id, path, filename, extension, "
            "block_type, block_number, text) VALUES(?,?,?,?,?,?,?,?)",
            (doc_id, bcur.lastrowid, str(file_path), file_path.name, ext, "paragraph", str(i), btext),
        )
    store.conn.commit()
    # Run the ACL backfill now that the doc + admin both exist: grants 'public'
    # read on the new (unowned) doc and wires admin into the 'public' group. This
    # mirrors what the app does at startup (see tests/test_app_audit.py).
    if owner_principal_id is None:
        store._backfill_acl()
    store.conn.commit()
    store.conn.close()
    return doc_id


def test_preview_text_returns_block_text(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    f = tmp_path / "report.txt"
    f.write_text("alpha beta gamma", encoding="utf-8")
    doc_id = _seed_doc(db, f, "alpha beta gamma", blocks=["alpha beta", "gamma delta"])
    with TestClient(app) as client:
        token = _login(client)
        r = client.get(f"/api/files/preview-text?document_id={doc_id}",
                       headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["filename"] == "report.txt"
        assert [b["text"] for b in body["blocks"]] == ["alpha beta", "gamma delta"]
        assert body["truncated"] is False


def test_preview_text_404_for_unknown_doc(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    with TestClient(app) as client:
        token = _login(client)
        r = client.get("/api/files/preview-text?document_id=999999",
                       headers={"X-Auth-Token": token})
        assert r.status_code == 404


def test_preview_serves_inline_with_text_content_type(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    f = tmp_path / "note.txt"
    f.write_text("hello inline", encoding="utf-8")
    doc_id = _seed_doc(db, f, "hello inline", ext=".txt")
    with TestClient(app) as client:
        token = _login(client)
        r = client.get(f"/api/files/preview?document_id={doc_id}",
                       headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        assert r.content == b"hello inline"
        assert r.headers["content-type"].startswith("text/plain")
        assert r.headers["content-disposition"].lower().startswith("inline")
        assert r.headers["x-preview-kind"] == "text"


def test_preview_pdf_content_type_and_kind(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    f = tmp_path / "doc.pdf"
    # Minimal bytes; we only assert headers, not PDF validity.
    f.write_bytes(b"%PDF-1.4\n%%EOF\n")
    doc_id = _seed_doc(db, f, "pdf body", ext=".pdf")
    with TestClient(app) as client:
        token = _login(client)
        r = client.get(f"/api/files/preview?document_id={doc_id}",
                       headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("application/pdf")
        assert r.headers["x-preview-kind"] == "pdf"
        assert r.headers["content-disposition"].lower().startswith("inline")


def test_preview_unsupported_extension_still_inline_with_kind(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    f = tmp_path / "archive.zip"
    f.write_bytes(b"PK\x03\x04zip")
    doc_id = _seed_doc(db, f, "zip", ext=".zip")
    with TestClient(app) as client:
        token = _login(client)
        r = client.get(f"/api/files/preview?document_id={doc_id}",
                       headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        assert r.headers["x-preview-kind"] == "unsupported"


def test_preview_is_acl_gated_non_owner_404(tmp_path):
    """A document restricted to 'admin'-only is invisible to a second user.

    Reconciliation: the NEW preview/preview-text endpoints return 404 on an ACL
    miss (no existence leak). The pre-existing /api/files/open endpoint is also
    ACL-gated but returns 403 (its established contract — see
    tests/test_acl_enforcement.py). So we assert 404 for the two preview URLs and
    403 for /open.
    """
    db = tmp_path / "t.db"
    app = create_app(str(db))

    # Create the default admin + a second, non-admin user, and a doc only admin sees.
    seed = SqliteStore(db)
    seed.ensure_default_admin()
    seed.create_user("bob", "bobpw")
    now = datetime.now(tz=UTC).isoformat()
    secret = tmp_path / "secret.txt"
    secret.write_text("classified", encoding="utf-8")
    # Owner = admin's user principal; do NOT grant 'public'.
    admin_principal = seed.conn.execute(
        "SELECT principal_id FROM users WHERE username='admin'"
    ).fetchone()["principal_id"]
    cur = seed.conn.execute(
        "INSERT INTO documents(path, filename, extension, file_size, modified_at, sha256, "
        "indexed_at, status, owner_principal_id) VALUES(?,?,?,?,?,?,?,?,?)",
        (str(secret), "secret.txt", ".txt", 10, now, "hsecret", now, "ok", admin_principal),
    )
    doc_id = cur.lastrowid
    bcur = seed.conn.execute(
        "INSERT INTO content_blocks(document_id, block_type, block_number, text, extractor, text_length) "
        "VALUES(?,?,?,?,?,?)", (doc_id, "paragraph", 1, "classified", "txt", 10),
    )
    seed.conn.execute(
        "INSERT INTO content_fts(document_id, block_id, path, filename, extension, block_type, block_number, text) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (doc_id, bcur.lastrowid, str(secret), "secret.txt", ".txt", "paragraph", "1", "classified"),
    )
    # Remove any auto-granted 'public' read so bob truly can't see it.
    public_id = seed.conn.execute(
        "SELECT id FROM principals WHERE type='group' AND external_id='public'"
    ).fetchone()
    if public_id:
        seed.conn.execute(
            "DELETE FROM document_acl WHERE document_id=? AND principal_id=?",
            (doc_id, public_id["id"]),
        )
    seed.conn.commit()
    seed.conn.close()

    with TestClient(app) as client:
        bob = _login(client, "bob", "bobpw")
        # bob cannot preview or preview-text the admin-only doc -> 404 (no leak).
        for url in (f"/api/files/preview?document_id={doc_id}",
                    f"/api/files/preview-text?document_id={doc_id}"):
            r = client.get(url, headers={"X-Auth-Token": bob})
            assert r.status_code == 404, f"{url} -> {r.status_code}"
        # The pre-existing /open endpoint is ACL-gated too, but returns 403.
        r = client.get(f"/api/files/open?document_id={doc_id}", headers={"X-Auth-Token": bob})
        assert r.status_code == 403, f"/open -> {r.status_code}"
        # admin CAN preview it.
        admin = _login(client, "admin", "admin")
        r = client.get(f"/api/files/preview?document_id={doc_id}",
                       headers={"X-Auth-Token": admin})
        assert r.status_code == 200, r.text
