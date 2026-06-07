"""Validation layer for all AI (LLM) outputs before they are persisted or returned.

AGENTS.md requires every LLM output be schema/format-validated before it is
persisted. This module is pure (no I/O, no Ollama). Validators take the parsed
JSON the model produced and either return a normalised result or raise
`AiValidationError`. Citation validation additionally guarantees that returned
citations reference only source ids that were actually supplied to the model —
hallucinated citations are dropped, never returned.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


class AiValidationError(ValueError):
    """Raised when an AI output does not satisfy its expected schema/format."""


@dataclass(slots=True)
class TagSuggestion:
    suggested_tags: list[str]


@dataclass(slots=True)
class RagSummary:
    summary: str
    citations: list[str]


def _clean_tags(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for t in value:
        if not isinstance(t, str):
            continue
        cleaned = t.strip().lower()[:30]
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out[:5]


def validate_tag_suggestion(raw: object) -> TagSuggestion:
    """Validate a single-document tag suggestion (shape of AiOrganizer.suggest)."""
    if not isinstance(raw, dict):
        raise AiValidationError("tag suggestion must be a JSON object")
    if "suggested_tags" not in raw:
        raise AiValidationError("tag suggestion missing 'suggested_tags'")
    tags = _clean_tags(raw.get("suggested_tags"))
    if not tags:
        raise AiValidationError("tag suggestion produced no valid tags")
    return TagSuggestion(suggested_tags=tags)


# Citation tokens look like S1, S2, ... (case-insensitive on input, normalised upper).
_CITATION_RE = re.compile(r"^[Ss](\d+)$")


def validate_summary(raw: object, *, allowed_source_ids: set[str]) -> RagSummary:
    """Validate a RAG summary and filter its citations to the supplied source set.

    `allowed_source_ids` is the set of citation ids ("S1", "S2", ...) that were
    actually presented to the model. Any citation the model returns that is not
    in this set is dropped (hallucination guard).
    """
    if not isinstance(raw, dict):
        raise AiValidationError("summary must be a JSON object")
    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise AiValidationError("summary must be a non-empty string")

    raw_citations = raw.get("citations", [])
    if not isinstance(raw_citations, list):
        raw_citations = []

    allowed_norm = {s.upper() for s in allowed_source_ids}
    seen: set[str] = set()
    citations: list[str] = []
    for c in raw_citations:
        if not isinstance(c, str):
            continue
        m = _CITATION_RE.match(c.strip())
        if not m:
            continue
        token = f"S{int(m.group(1))}"
        if token in allowed_norm and token not in seen:
            seen.add(token)
            citations.append(token)

    return RagSummary(summary=summary.strip()[:4000], citations=citations)


def validate_structure(raw: object) -> dict:
    """Validate the folder-taxonomy shape (mirrors suggest_structure output)."""
    if not isinstance(raw, dict):
        raise AiValidationError("structure must be a JSON object")
    structure = raw.get("suggested_structure")
    if not isinstance(structure, list):
        raise AiValidationError("structure missing 'suggested_structure' list")
    cleaned = []
    for item in structure:
        if not isinstance(item, dict) or "folder" not in item:
            continue
        folder = str(item.get("folder", "")).strip().lower()
        if not folder:
            continue
        cleaned.append({
            "folder": folder,
            "description": str(item.get("description", ""))[:200],
            "examples": [str(e) for e in item.get("examples", []) if e][:3],
        })
    if not cleaned:
        raise AiValidationError("structure had no valid folder entries")
    return {
        "suggested_structure": cleaned,
        "rationale": str(raw.get("rationale", ""))[:250] or None,
    }
