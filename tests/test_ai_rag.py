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
