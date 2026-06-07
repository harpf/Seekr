import pytest

from document_search.services.ai_validation import (
    AiValidationError,
    RagSummary,
    TagSuggestion,
    validate_structure,
    validate_summary,
    validate_tag_suggestion,
)


# ── Tag suggestion ────────────────────────────────────────────────────────
def test_validate_tag_suggestion_happy_path():
    out = validate_tag_suggestion({"suggested_tags": ["Invoice", "2025", "finance"]})
    assert isinstance(out, TagSuggestion)
    # Normalised: lowercased, stripped, deduped, capped at 5
    assert out.suggested_tags == ["invoice", "2025", "finance"]


def test_validate_tag_suggestion_rejects_non_dict():
    with pytest.raises(AiValidationError):
        validate_tag_suggestion("not a dict")


def test_validate_tag_suggestion_rejects_missing_tags():
    with pytest.raises(AiValidationError):
        validate_tag_suggestion({"reason": "no tags here"})


def test_validate_tag_suggestion_rejects_empty_after_cleaning():
    # All-whitespace / empty tags collapse to nothing -> invalid
    with pytest.raises(AiValidationError):
        validate_tag_suggestion({"suggested_tags": ["", "   ", None]})


def test_validate_tag_suggestion_caps_at_five():
    out = validate_tag_suggestion(
        {"suggested_tags": ["a", "b", "c", "d", "e", "f", "g"]}
    )
    assert out.suggested_tags == ["a", "b", "c", "d", "e"]


# ── RAG summary with citations ────────────────────────────────────────────
def test_validate_summary_keeps_only_real_citations():
    allowed = {"S1", "S2", "S3"}
    out = validate_summary(
        {"summary": "Revenue grew [S1] and costs fell [S2].", "citations": ["S1", "S2", "S9"]},
        allowed_source_ids=allowed,
    )
    assert isinstance(out, RagSummary)
    assert out.summary.startswith("Revenue grew")
    # S9 was hallucinated and is dropped; order preserved, deduped.
    assert out.citations == ["S1", "S2"]


def test_validate_summary_requires_nonempty_summary():
    with pytest.raises(AiValidationError):
        validate_summary({"summary": "   ", "citations": ["S1"]}, allowed_source_ids={"S1"})


def test_validate_summary_rejects_non_dict():
    with pytest.raises(AiValidationError):
        validate_summary(42, allowed_source_ids={"S1"})


def test_validate_summary_allows_zero_citations():
    # A summary that cites nothing valid is allowed (caller may still surface it),
    # but citations list must end up empty, never containing fakes.
    out = validate_summary(
        {"summary": "No supporting evidence found.", "citations": ["S5"]},
        allowed_source_ids={"S1"},
    )
    assert out.citations == []


# ── Structure (reused shape from existing suggest_structure) ───────────────
def test_validate_structure_happy_path():
    out = validate_structure(
        {
            "suggested_structure": [
                {"folder": "finance/invoices", "description": "x", "examples": ["a.pdf"]}
            ],
            "rationale": "ok",
        }
    )
    assert out["suggested_structure"][0]["folder"] == "finance/invoices"


def test_validate_structure_rejects_missing_list():
    with pytest.raises(AiValidationError):
        validate_structure({"rationale": "no structure"})
