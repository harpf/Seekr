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


fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from document_search.app import create_app  # noqa: E402
from document_search.services.ai_organizer import AiOrganizer, OrganizationSuggestion  # noqa: E402


def _login(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _seed_two_docs(db_path):
    from datetime import UTC, datetime
    db = SqliteStore(db_path)
    now = datetime.now(tz=UTC).isoformat()
    ids = []
    for name in ("alpha.pdf", "beta.pdf"):
        cur = db.conn.execute(
            "INSERT INTO documents(path, filename, extension, file_size, "
            "modified_at, sha256, indexed_at, status) VALUES(?,?,?,?,?,?,?,?)",
            (f"/docs/{name}", name, ".pdf", 10, now, name[:6], now, "ok"),
        )
        ids.append(cur.lastrowid)
    db.conn.commit()
    db._backfill_acl()
    return ids


def _wait(client, token, job_id, timeout=5.0):
    import time
    deadline = time.monotonic() + timeout
    body = {}
    while time.monotonic() < deadline:
        body = client.get(f"/api/ai/jobs/{job_id}", headers={"X-Auth-Token": token}).json()
        if body.get("status") in ("finished", "failed", "interrupted"):
            return body
        time.sleep(0.05)
    return body


def test_bulk_tag_applies_tags_and_records_provenance(tmp_path, monkeypatch):
    db_path = tmp_path / "t.db"
    app = create_app(str(db_path))

    def fake_suggest(self, *, file_path, extracted_text="", tags, metadata):
        return OrganizationSuggestion(
            suggested_subpath="finance/invoices",
            suggested_tags=["invoice", "2025"],
            reason="looks like an invoice",
            model="llama3.2",
        )

    monkeypatch.setattr(AiOrganizer, "suggest", fake_suggest)

    with TestClient(app) as client:
        token = _login(client)
        doc_ids = _seed_two_docs(db_path)
        r = client.post(
            "/api/ai/bulk-tag/start",
            headers={"X-Auth-Token": token},
            json={"limit": 10, "apply": True},
        )
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        body = _wait(client, token, job_id)
        assert body["status"] == "finished", body
        assert body["total"] == 2
        assert body["done"] == 2
        assert len(body["results"]) == 2
        for item in body["results"]:
            assert item["status"] == "applied"
            assert item["applied_tags"] == ["invoice", "2025"]

        # Tags were actually written for the admin user on both docs.
        db = SqliteStore(db_path)
        for doc_id in doc_ids:
            tagged = db.conn.execute(
                "SELECT ut.name FROM document_tags dt JOIN user_tags ut ON ut.id=dt.tag_id "
                "WHERE dt.document_id=?",
                (doc_id,),
            ).fetchall()
            names = {row["name"] for row in tagged}
            assert {"invoice", "2025"}.issubset(names)

        # Provenance: one ai_decisions row per applied document.
        decisions = db.list_ai_decisions(kind="bulk_tag")
        assert len(decisions) == 2
        for d in decisions:
            assert d["model"] == "llama3.2"
            assert d["prompt_sha256"]
            assert d["applied"] == 1


def test_bulk_tag_propose_mode_does_not_write_tags(tmp_path, monkeypatch):
    db_path = tmp_path / "t.db"
    app = create_app(str(db_path))

    def fake_suggest(self, *, file_path, extracted_text="", tags, metadata):
        return OrganizationSuggestion(suggested_tags=["draft"], reason="x", model="llama3.2")

    monkeypatch.setattr(AiOrganizer, "suggest", fake_suggest)

    with TestClient(app) as client:
        token = _login(client)
        _seed_two_docs(db_path)
        r = client.post(
            "/api/ai/bulk-tag/start",
            headers={"X-Auth-Token": token},
            json={"limit": 10, "apply": False},
        )
        job_id = r.json()["job_id"]
        body = _wait(client, token, job_id)
        assert body["status"] == "finished"
        for item in body["results"]:
            assert item["status"] == "proposed"
            assert item["applied_tags"] == []

        db = SqliteStore(db_path)
        total_tags = db.conn.execute("SELECT COUNT(*) AS c FROM document_tags").fetchone()["c"]
        assert total_tags == 0  # nothing written in propose mode
        # Provenance still recorded, but applied=0.
        decisions = db.list_ai_decisions(kind="bulk_tag")
        assert len(decisions) == 2
        assert all(d["applied"] == 0 for d in decisions)


def test_bulk_tag_skips_invalid_ai_output(tmp_path, monkeypatch):
    """When the suggestion has no usable tags, the validator rejects it and the
    document is skipped (no tags written, no provenance applied)."""
    db_path = tmp_path / "t.db"
    app = create_app(str(db_path))

    def empty_suggest(self, *, file_path, extracted_text="", tags, metadata):
        return OrganizationSuggestion(suggested_tags=None, reason="no idea", model="llama3.2")

    monkeypatch.setattr(AiOrganizer, "suggest", empty_suggest)

    with TestClient(app) as client:
        token = _login(client)
        _seed_two_docs(db_path)
        r = client.post(
            "/api/ai/bulk-tag/start",
            headers={"X-Auth-Token": token},
            json={"limit": 10, "apply": True},
        )
        job_id = r.json()["job_id"]
        body = _wait(client, token, job_id)
        assert body["status"] == "finished"
        for item in body["results"]:
            assert item["status"] == "skipped"
            assert "validation" in item["reason"].lower() or "no valid" in item["reason"].lower()

        db = SqliteStore(db_path)
        assert db.conn.execute("SELECT COUNT(*) AS c FROM document_tags").fetchone()["c"] == 0


def test_bulk_tag_requires_admin(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        admin = _login(client)
        client.post(
            "/api/users",
            headers={"X-Auth-Token": admin},
            json={"username": "bob", "password": "bobpass12", "role": "user"},
        )
        bob = _login(client, "bob", "bobpass12")
        r = client.post(
            "/api/ai/bulk-tag/start",
            headers={"X-Auth-Token": bob},
            json={"limit": 5, "apply": True},
        )
        assert r.status_code == 403, r.text
