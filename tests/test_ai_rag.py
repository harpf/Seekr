import json

import pytest

from document_search.services.ai_organizer import AiOrganizer
from document_search.services.ai_validation import AiValidationError, RagSummary


def _sources():
    # (document_id, block_number, filename, snippet_text)
    return [
        {"document_id": 11, "block_number": 0, "filename": "q3.pdf", "text": "Revenue rose 12%."},
        {"document_id": 11, "block_number": 3, "filename": "q3.pdf", "text": "Costs fell 4%."},
        {"document_id": 42, "block_number": 1, "filename": "memo.docx", "text": "Hiring frozen."},
    ]


def test_summarize_with_citations_returns_validated_summary(monkeypatch):
    org = AiOrganizer()

    def fake_generate(self, prompt, *, format=None, options=None, timeout=None):
        # Model cites S1 and S3 (both real) plus S9 (hallucinated).
        return {"response": json.dumps({
            "summary": "Revenue rose and hiring was frozen [S1][S3].",
            "citations": ["S1", "S3", "S9"],
        })}

    monkeypatch.setattr(AiOrganizer, "_generate", fake_generate)
    result = org.summarize_with_citations(query="how did the quarter go?", sources=_sources())
    assert isinstance(result, RagSummary)
    assert "Revenue rose" in result.summary
    # S9 dropped; S1 and S3 map to real sources.
    assert result.citations == ["S1", "S3"]


def test_summarize_with_citations_rejects_invalid_output(monkeypatch):
    org = AiOrganizer()

    def fake_generate(self, prompt, *, format=None, options=None, timeout=None):
        # Empty summary -> validator must reject.
        return {"response": json.dumps({"summary": "   ", "citations": []})}

    monkeypatch.setattr(AiOrganizer, "_generate", fake_generate)
    with pytest.raises(AiValidationError):
        org.summarize_with_citations(query="q", sources=_sources())


def test_summarize_prompt_enumerates_real_block_numbers(monkeypatch):
    org = AiOrganizer()
    captured = {}

    def fake_generate(self, prompt, *, format=None, options=None, timeout=None):
        captured["prompt"] = prompt
        return {"response": json.dumps({"summary": "ok", "citations": ["S1"]})}

    monkeypatch.setattr(AiOrganizer, "_generate", fake_generate)
    org.summarize_with_citations(query="q", sources=_sources())
    p = captured["prompt"]
    # Each source must be addressable by its real (document_id, block_number).
    assert "doc 11 block 0" in p
    assert "doc 11 block 3" in p
    assert "doc 42 block 1" in p
    assert "[S1]" in p and "[S2]" in p and "[S3]" in p


fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from document_search.app import create_app  # noqa: E402
from document_search.index.sqlite_store import SqliteStore  # noqa: E402


def _login(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _seed_doc_with_blocks(db_path):
    """Insert one document with two content blocks + FTS rows so search() returns hits."""
    from datetime import UTC, datetime
    db = SqliteStore(db_path)
    now = datetime.now(tz=UTC).isoformat()
    cur = db.conn.execute(
        "INSERT INTO documents(path, filename, extension, file_size, "
        "modified_at, sha256, indexed_at, status) VALUES(?,?,?,?,?,?,?,?)",
        ("/docs/q3.pdf", "q3.pdf", ".pdf", 100, now, "deadbeef", now, "ok"),
    )
    doc_id = cur.lastrowid
    for bn, text in ((0, "Quarterly revenue rose twelve percent year over year."),
                     (3, "Operating costs fell four percent in the same period.")):
        bc = db.conn.execute(
            "INSERT INTO content_blocks(document_id, block_type, block_number, text, "
            "extractor, text_length, metadata_json) VALUES(?,?,?,?,?,?,?)",
            (doc_id, "paragraph", bn, text, "test", len(text), "{}"),
        )
        db.conn.execute(
            "INSERT INTO content_fts(document_id, block_id, path, filename, extension, "
            "block_type, block_number, text) VALUES(?,?,?,?,?,?,?,?)",
            (doc_id, bc.lastrowid, "/docs/q3.pdf", "q3.pdf", ".pdf", "paragraph", str(bn), text),
        )
    db.conn.commit()
    # Re-run ACL backfill so the new doc is visible to existing users via 'public'.
    db._backfill_acl()
    return doc_id


def test_summarize_endpoint_returns_summary_and_real_citations(tmp_path, monkeypatch):
    db_path = tmp_path / "t.db"
    app = create_app(str(db_path))

    def fake_generate(self, prompt, *, format=None, options=None, timeout=None):
        import json
        return {"response": json.dumps({
            "summary": "Revenue rose and costs fell [S1][S2].",
            "citations": ["S1", "S2", "S7"],
        })}

    monkeypatch.setattr(AiOrganizer, "is_available", lambda self: True)
    monkeypatch.setattr(AiOrganizer, "_generate", fake_generate)

    with TestClient(app) as client:
        token = _login(client)
        doc_id = _seed_doc_with_blocks(db_path)
        r = client.post(
            "/api/ai/summarize",
            headers={"X-Auth-Token": token},
            json={"query": "revenue", "k": 5},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "Revenue rose" in body["summary"]
        # S7 hallucination dropped; remaining citations reference real blocks.
        labels = {s["label"] for s in body["sources"]}
        for cited in body["citations"]:
            assert cited in labels
        # Every returned source references a real (document_id, block_number).
        for s in body["sources"]:
            assert s["document_id"] == doc_id
            assert s["block_number"] in (0, 3)


def test_summarize_endpoint_502_when_ollama_unavailable(tmp_path, monkeypatch):
    app = create_app(str(tmp_path / "t.db"))
    monkeypatch.setattr(AiOrganizer, "is_available", lambda self: False)
    with TestClient(app) as client:
        token = _login(client)
        r = client.post(
            "/api/ai/summarize",
            headers={"X-Auth-Token": token},
            json={"query": "anything", "k": 5},
        )
        assert r.status_code == 502, r.text


def test_summarize_endpoint_422_on_invalid_ai_output(tmp_path, monkeypatch):
    db_path = tmp_path / "t.db"
    app = create_app(str(db_path))

    def bad_generate(self, prompt, *, format=None, options=None, timeout=None):
        import json
        return {"response": json.dumps({"summary": "   ", "citations": []})}

    monkeypatch.setattr(AiOrganizer, "is_available", lambda self: True)
    monkeypatch.setattr(AiOrganizer, "_generate", bad_generate)

    with TestClient(app) as client:
        token = _login(client)
        _seed_doc_with_blocks(db_path)
        r = client.post(
            "/api/ai/summarize",
            headers={"X-Auth-Token": token},
            json={"query": "revenue", "k": 5},
        )
        assert r.status_code == 422, r.text


def test_summarize_endpoint_empty_results(tmp_path, monkeypatch):
    """No matching docs -> 200 with empty sources and a 'no results' summary."""
    app = create_app(str(tmp_path / "t.db"))
    monkeypatch.setattr(AiOrganizer, "is_available", lambda self: True)
    with TestClient(app) as client:
        token = _login(client)
        r = client.post(
            "/api/ai/summarize",
            headers={"X-Auth-Token": token},
            json={"query": "nonexistentterm", "k": 5},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["sources"] == []
        assert body["citations"] == []
