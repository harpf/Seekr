# AI RAG Summarisation, Bulk Auto-Tagging & AI Provenance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the three P2 "AI features" from `docs/ROADMAP.md` on top of the existing Ollama + persistent-job-queue infrastructure: (1) **RAG search summarisation** — a synchronous endpoint that retrieves the top-K ACL-filtered search hits for a query, prompts Ollama to summarise them *with citations back to real source blocks*, and returns the summary plus a list of cited sources; (2) **Bulk auto-tagging** — a new `ai_bulk_tag` persistent job that iterates owner/ACL-scoped documents, reuses the existing single-file `AiOrganizer.suggest()` logic, and applies (or proposes) tags with incremental progress; (3) **AI output validation + provenance** — a single validation layer (`ai_validation.py`) that every AI output must pass before persistence, plus an `ai_decisions` provenance table recording model name, prompt hash, input document, validated output, and timestamp. This satisfies the AGENTS.md requirements (`AGENTS.md:42` "LLM-Ausgaben validieren (Schema/Format), bevor sie persistiert werden" and `AGENTS.md:43` "Herkunft … von AI-Entscheidungen dokumentierbar halten").

**Architecture:** AI calls stay encapsulated in `document_search/services/ai_organizer.py` (the only module that talks to Ollama). A **new pure module** `document_search/services/ai_validation.py` holds dataclass/JSON-schema validators — no I/O, no Ollama — so they are trivially unit-testable. A **new method** `AiOrganizer.summarize_with_citations()` performs the RAG generate call and returns a validated `RagSummary`. The **RAG retrieval** reuses `document_search/index/search_service.search()` exactly as `api_search` does (`app.py:1584`), so ACL filtering is inherited for free. A **new provenance table** `ai_decisions` is added to `SqliteStore._init_schema`, written only through a new `SqliteStore.record_ai_decision()` helper. The **bulk-tag job** is registered as a worker handler (`ai_bulk_tag`) following the exact pattern of the existing `ai_reorganize` handler (`app.py:350-391`); it calls `organizer.suggest()` per document, validates the result, applies tags via the existing `SqliteStore.set_tags()` (`sqlite_store.py:385`), and records provenance. Routes follow existing auth conventions: `require_user` for read/RAG, `require_admin` for the bulk mutation.

**Tech Stack:** Python 3.11, SQLite (FTS5), FastAPI, pytest. **No new third-party dependencies** — validation is hand-rolled (stdlib `dataclasses` + explicit checks), matching the existing `_safe_subpath`/`_safe_tags` style in `ai_organizer.py:321-333`. Tests **never** hit a live model: every AI call is monkeypatched (the organizer method or the underlying `_generate` call is stubbed).

**Scope boundaries:**

In scope:
- `ai_decisions` provenance table + `SqliteStore.record_ai_decision()` / `list_ai_decisions()` helpers.
- `ai_validation.py`: `validate_summary`, `validate_tag_suggestion`, `validate_structure`, raising a typed `AiValidationError` on malformed output.
- `AiOrganizer.summarize_with_citations()` + a small internal `_generate()` seam so tests can stub one place.
- Synchronous `POST /api/ai/summarize` (top-K RAG over ACL-filtered search, returns summary + cited sources).
- `ai_bulk_tag` persistent job kind + handler + `POST /api/ai/bulk-tag/start` (admin) returning `{"job_id": ...}`, served by the existing `GET /api/ai/jobs/{id}`.
- Provenance writes wired into the bulk-tag handler.
- Tests: validator unit tests; RAG citation-integrity test (citations reference real block numbers); RAG rejects invalid AI output; bulk-tag applies tags and records provenance; bulk-tag respects ACL/owner scope.

Out of scope (deferred):
- Streaming/SSE of the summary (sync endpoint returns the whole answer).
- Vector / semantic retrieval (RAG retrieval uses the existing FTS5 `search()` only — the hybrid-search roadmap item is separate).
- A UI surface for browsing `ai_decisions` (table + write path only; read helper exists for tests).
- Re-validating or migrating the already-persisted `ai_reorganize` / `ai_suggest_structure` outputs (those keep their current behaviour; only *new* code paths route through the validator).
- Changing `/api/ai/pull-model`, `/api/system/update`, or the existing AI job handlers' business logic.

---

## Key design decisions (locked)

- **RAG is synchronous, not a job.** A summary over top-K (default 5) blocks is a single short Ollama `generate` with a bounded timeout (`organizer.timeout`, default 30 s). The existing job queue is for long fan-out work; one generate call doesn't justify a job round-trip. The endpoint returns `502` if Ollama is unavailable rather than enqueuing.
- **Citations reference real block numbers.** Retrieval returns rows from `search()` which already carry `document_id` and `block_number` (`search_service.py:124-130`). The prompt enumerates sources as `[S1] doc <document_id> block <block_number>`. The validator rejects any citation id (`S1`, `S2`, …) the model emits that wasn't in the supplied source set — so a hallucinated `[S9]` is dropped, and the returned `sources` list always maps back to a real `(document_id, block_number)`.
- **Validation is mandatory before persistence.** `ai_bulk_tag` calls `validate_tag_suggestion()` before `set_tags()`; on `AiValidationError` the document is recorded as `skipped` (with the reason) and **no tags are written**. The RAG endpoint validates the summary before returning; invalid output yields `422` (sync) — nothing is persisted for RAG anyway.
- **Provenance is best-effort but always attempted on a successful AI decision.** Every applied bulk-tag decision writes one `ai_decisions` row (model, sha256 prompt hash, input document id, validated JSON output, ISO-8601 UTC timestamp). A provenance write failure is logged but does not roll back the tag (the tag is the user-visible effect; the audit row is secondary).
- **`apply` vs `propose` for bulk tagging.** The job payload carries `apply: bool` (default `False`). When `apply=False` the job only *proposes* tags (returned in results, no DB write, but provenance is still recorded with `applied=0`). When `apply=True` it writes tags via `set_tags()` and records provenance with `applied=1`. This mirrors the existing reorganize "suggest then apply" split (`/api/ai/reorganize/start` → `/api/ai/reorganize/apply`).
- **Bulk-tag scope.** Documents are selected exactly like the AI reorganize handler: from `documents`, filtered to those visible to the requesting owner via the same ACL subquery `search_service`/`acl_service` uses, capped by `limit`. Tags are written under the requesting admin's `user_id` (tags are per-user in this schema — `user_tags.user_id`, `sqlite_store.py:66-72`).
- **One stub seam.** `AiOrganizer` gains a private `_generate(self, prompt, *, format=None, options=None, timeout=None) -> dict` that wraps the `urllib` POST to `/api/generate`. `summarize_with_citations()` calls it. Tests monkeypatch `_generate` (or the higher-level method) — never the network. The existing `suggest()`/`suggest_structure()` are **not** refactored onto `_generate` in this plan (keeps the diff small; they stay as-is).

---

## File Structure

**Create:**
- `document_search/services/ai_validation.py` — pure validators + `AiValidationError` + result dataclasses.
- `tests/test_ai_validation.py` — unit tests for the validators.
- `tests/test_ai_rag.py` — RAG endpoint + `summarize_with_citations` tests (Ollama stubbed).
- `tests/test_ai_bulk_tag.py` — bulk-tag job tests (Ollama stubbed) incl. provenance + ACL scope.

**Modify:**
- `document_search/index/sqlite_store.py` — add `ai_decisions` table to `_init_schema`; add `record_ai_decision()` + `list_ai_decisions()` helpers.
- `document_search/services/ai_organizer.py` — add `RagSummary` import path use, `_generate()` seam, `summarize_with_citations()`.
- `document_search/app.py` — register `ai_bulk_tag` worker handler; add `POST /api/ai/summarize` and `POST /api/ai/bulk-tag/start`; extend `GET /api/ai/jobs/{id}` to serve `ai_bulk_tag`.

**Untouched (must not change behaviour):**
- Existing `suggest()`, `suggest_structure()`, `ask()` in `ai_organizer.py`.
- `ai_suggest_structure` / `ai_reorganize` handlers and their routes.
- `/api/ai/pull-model`, `/api/system/update`.

---

## Task 1: `ai_decisions` provenance table + store helpers

**Files:**
- Modify: `document_search/index/sqlite_store.py` (extend `_init_schema`; add helpers)
- Test: `tests/test_ai_bulk_tag.py` (new — provenance unit slice)

- [ ] **Step 1: Write the failing test**

Create `tests/test_ai_bulk_tag.py`:

```python
import json
from pathlib import Path

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
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_ai_bulk_tag.py -v
```

Expected: `test_ai_decisions_table_exists` fails — table does not exist yet.

- [ ] **Step 3: Add the table to `_init_schema`**

In `document_search/index/sqlite_store.py`, inside the `self.conn.executescript("""...""")` block in `_init_schema`, **append** immediately after the last jobs index line (`CREATE INDEX IF NOT EXISTS idx_jobs_owner ... ON jobs(owner_user_id);`, currently `sqlite_store.py:158`) and **before** the closing `"""`:

```sql
            CREATE TABLE IF NOT EXISTS ai_decisions (
              id INTEGER PRIMARY KEY,
              kind TEXT NOT NULL,
              model TEXT,
              prompt_sha256 TEXT NOT NULL,
              document_id INTEGER,
              output_json TEXT NOT NULL,
              applied INTEGER NOT NULL DEFAULT 0,
              user_id INTEGER,
              created_at TEXT NOT NULL,
              FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE SET NULL,
              FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ai_decisions_kind ON ai_decisions(kind);
            CREATE INDEX IF NOT EXISTS idx_ai_decisions_doc  ON ai_decisions(document_id);
            CREATE INDEX IF NOT EXISTS idx_ai_decisions_user ON ai_decisions(user_id);
```

- [ ] **Step 4: Add the store helpers**

In `document_search/index/sqlite_store.py`, add these two methods. Put them right after `set_tags()` (which ends at `sqlite_store.py:398`). `datetime`/`UTC` are already imported at module top (used by `set_tags`, `sqlite_store.py:396`); `json` is needed — if it is not already imported at the top of the file, add `import json` to the existing import block.

```python
    def record_ai_decision(
        self,
        *,
        kind: str,
        model: str | None,
        prompt_sha256: str,
        document_id: int | None,
        output: dict,
        applied: int,
        user_id: int | None,
    ) -> int:
        """Persist an audit row for a single AI decision (AGENTS.md provenance).

        `output` must already be the *validated* output dict. Returns the new row id.
        """
        cur = self.conn.execute(
            "INSERT INTO ai_decisions"
            "(kind, model, prompt_sha256, document_id, output_json, applied, user_id, created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                kind,
                model,
                prompt_sha256,
                document_id,
                json.dumps(output),
                int(applied),
                user_id,
                datetime.now(tz=UTC).isoformat(),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_ai_decisions(
        self,
        document_id: int | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> list:
        clauses = []
        params: list = []
        if document_id is not None:
            clauses.append("document_id=?")
            params.append(document_id)
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        return self.conn.execute(
            f"SELECT * FROM ai_decisions {where} ORDER BY id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
```

- [ ] **Step 5: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_ai_bulk_tag.py -v
```

Expected: 3 passing.

- [ ] **Step 6: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: previous baseline + 3 new, zero failures.

- [ ] **Step 7: Commit**

```powershell
git add document_search/index/sqlite_store.py tests/test_ai_bulk_tag.py
git commit -m @'
feat(ai): add ai_decisions provenance table and store helpers

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 2: AI output validation layer (`ai_validation.py`)

**Files:**
- Create: `document_search/services/ai_validation.py`
- Test: `tests/test_ai_validation.py` (new)

This is a pure, I/O-free module. Validators take the raw parsed JSON (a `dict`) the model returned and either return a normalised dataclass or raise `AiValidationError`. Citation validation is the load-bearing part for RAG: it filters the model's claimed citations down to the set of source ids actually provided.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ai_validation.py`:

```python
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
```

- [ ] **Step 2: Run, expect ImportError**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_ai_validation.py -v
```

Expected: ImportError — module does not exist.

- [ ] **Step 3: Implement `ai_validation.py`**

Create `document_search/services/ai_validation.py`:

```python
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
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_ai_validation.py -v
```

Expected: 11 passing.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: previous total + 11, zero failures.

- [ ] **Step 6: Commit**

```powershell
git add document_search/services/ai_validation.py tests/test_ai_validation.py
git commit -m @'
feat(ai): add LLM output validation layer with citation hallucination guard

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 3: `AiOrganizer.summarize_with_citations()` + `_generate()` seam

**Files:**
- Modify: `document_search/services/ai_organizer.py`
- Test: `tests/test_ai_rag.py` (new — organizer-level slice)

We add a single private `_generate()` method that performs the Ollama POST, and a public `summarize_with_citations()` that builds a cited prompt, calls `_generate()`, parses, and validates. Tests stub `_generate` so no network is touched.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ai_rag.py`:

```python
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
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_ai_rag.py -v
```

Expected: AttributeError — `summarize_with_citations` / `_generate` do not exist yet.

- [ ] **Step 3: Implement the prompt, `_generate`, and `summarize_with_citations`**

In `document_search/services/ai_organizer.py`:

(a) **Add the prompt constant.** After the existing `_ASK_PROMPT` block (ends `ai_organizer.py:23`), insert:

```python
_SUMMARY_PROMPT = """\
You are a document search assistant. Summarise the answer to the user's question \
using ONLY the numbered sources below. Cite the sources you use inline with their \
labels in square brackets, e.g. [S1] or [S2][S3]. Do not invent sources or labels.

Question: {query}

Sources:
{sources}

Respond with ONLY a JSON object matching this schema exactly:
{{
  "summary": "2-5 sentence answer in plain text, with inline [S#] citations",
  "citations": ["S1", "S2"]
}}

Rules:
- summary: plain text, 2-5 sentences, only facts supported by the sources
- citations: the list of source labels you actually used (subset of those shown)
- If the sources do not answer the question, say so in the summary and return an empty citations list
"""
```

(b) **Add the `_generate` seam.** Inside `class AiOrganizer`, add this method (place it right after `__init__`, before `is_available`, around `ai_organizer.py:99`):

```python
    def _generate(
        self,
        prompt: str,
        *,
        format: str | None = None,
        options: dict | None = None,
        timeout: int | None = None,
    ) -> dict:
        """POST to Ollama /api/generate and return the parsed JSON envelope.

        Single network seam: tests monkeypatch this method to avoid a live model.
        Returns the raw Ollama response dict (with a 'response' string field).
        """
        body: dict = {"model": self.model, "prompt": prompt, "stream": False}
        if format is not None:
            body["format"] = format
        if options is not None:
            body["options"] = options
        payload = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{self.base_url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
            return json.loads(resp.read())
```

(c) **Add `summarize_with_citations`.** Add this method after `ask()` (which ends `ai_organizer.py:318`), before the module-level `_safe_subpath` helper:

```python
    def summarize_with_citations(
        self,
        *,
        query: str,
        sources: list[dict],
    ) -> "RagSummary":
        """RAG: summarise `sources` to answer `query`, with validated citations.

        `sources` is a list of dicts each carrying at least `document_id`,
        `block_number`, `filename`, and `text` (a snippet/excerpt). Sources are
        labelled S1..SN in order; the returned RagSummary.citations only ever
        contains labels that were actually supplied (hallucinations are dropped
        by the validator).

        Raises AiValidationError if the model output cannot be validated.
        """
        from document_search.services.ai_validation import validate_summary

        lines = []
        allowed: set[str] = set()
        for i, s in enumerate(sources, start=1):
            label = f"S{i}"
            allowed.add(label)
            excerpt = str(s.get("text", "")).strip().replace("\n", " ")[:500]
            lines.append(
                f"[{label}] doc {s['document_id']} block {s['block_number']} "
                f"({s.get('filename', '')}): {excerpt}"
            )
        prompt = _SUMMARY_PROMPT.format(query=query[:500], sources="\n".join(lines))
        raw = self._generate(
            prompt,
            format="json",
            options={"temperature": 0.2, "num_predict": 400},
        )
        try:
            parsed = json.loads(raw.get("response", "{}"))
        except json.JSONDecodeError as e:
            from document_search.services.ai_validation import AiValidationError
            raise AiValidationError(f"summary response was not valid JSON: {e}")
        return validate_summary(parsed, allowed_source_ids=allowed)
```

(Note: `RagSummary` is referenced only as a forward-ref string in the signature, so no import is needed at module load; the validator import is local to the method, matching the file's existing lazy-import style for `acl_service`.)

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_ai_rag.py -v
```

Expected: 3 passing.

- [ ] **Step 5: Confirm the legacy organizer test still passes**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_ai_organizer.py -v
```

Expected: `test_ai_organizer_placeholder` still passes (we did not touch `suggest()`).

- [ ] **Step 6: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: previous total + 3, zero failures.

- [ ] **Step 7: Commit**

```powershell
git add document_search/services/ai_organizer.py tests/test_ai_rag.py
git commit -m @'
feat(ai): add summarize_with_citations RAG method and _generate seam

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 4: `POST /api/ai/summarize` RAG endpoint

**Files:**
- Modify: `document_search/app.py`
- Test: `tests/test_ai_rag.py` (extend — endpoint slice)

The endpoint retrieves top-K ACL-filtered hits via `search()` (same call as `api_search`, `app.py:1584`), maps each hit row to a source dict, calls `organizer.summarize_with_citations()`, and returns `{summary, citations, sources}` where each entry of `sources` carries the real `document_id`, `block_number`, `filename`, and its `label`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ai_rag.py`:

```python
import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from document_search.app import create_app  # noqa: E402
from document_search.index.sqlite_store import SqliteStore  # noqa: E402
from document_search.services.ai_organizer import AiOrganizer  # noqa: E402


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
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_ai_rag.py -v
```

Expected: the four endpoint tests fail with 404 (route not registered).

- [ ] **Step 3: Add the route in `app.py`**

In `document_search/app.py`, add the route next to the other `/api/ai/*` routes. A good anchor is immediately after `api_ai_suggest_structure` (the route registered at `app.py:647`). Insert:

```python
    @app.post("/api/ai/summarize")
    def api_ai_summarize(
        req: dict,
        x_auth_token: str | None = Header(default=None),
    ):
        user_id = require_user(x_auth_token)
        query = str(req.get("query", "")).strip()
        if not query:
            raise HTTPException(status_code=400, detail="query must not be empty")
        try:
            k = int(req.get("k", 5))
        except (TypeError, ValueError):
            k = 5
        k = max(1, min(k, 10))

        if not organizer.is_available():
            raise HTTPException(status_code=502, detail="AI model is not available")

        db = store()
        try:
            rows = search(db, query, k, None, None, None, None, None, None, user_id)
        except sqlite3.OperationalError as e:
            raise HTTPException(status_code=400, detail=f"Search query error: {e}")

        sources: list[dict] = []
        for i, row in enumerate(rows, start=1):
            r = dict(row)
            sources.append({
                "label": f"S{i}",
                "document_id": r["document_id"],
                "block_number": r["block_number"],
                "filename": r["filename"],
                "path": r["path"],
                "text": r.get("snippet") or "",
            })

        if not sources:
            return {"summary": "No matching documents were found.", "citations": [], "sources": []}

        from document_search.services.ai_validation import AiValidationError
        try:
            result = organizer.summarize_with_citations(query=query, sources=sources)
        except AiValidationError as e:
            raise HTTPException(status_code=422, detail=f"AI output validation failed: {e}")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"AI summarisation failed: {type(e).__name__}")

        cited = set(result.citations)
        return {
            "summary": result.summary,
            "citations": result.citations,
            "sources": [
                {
                    "label": s["label"],
                    "document_id": s["document_id"],
                    "block_number": s["block_number"],
                    "filename": s["filename"],
                    "path": s["path"],
                    "cited": s["label"] in cited,
                }
                for s in sources
            ],
        }
```

The snippet retrieval uses the FTS `snippet()` text (`search_service.py:127`); when a query has no FTS match it falls into `_browse_all` which returns `snippet=None` — the `text` field then becomes `""`, which is acceptable (the model still gets the doc/block addressing). `search`, `sqlite3`, `store`, `require_user`, `Header`, and `HTTPException` are all already imported/in scope in `app.py` (see `app.py:1584` for the identical `search()` call and imports).

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_ai_rag.py -v
```

Expected: 7 passing (3 organizer-level from Task 3 + 4 endpoint).

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: previous total + 4, zero failures.

- [ ] **Step 6: Commit**

```powershell
git add document_search/app.py tests/test_ai_rag.py
git commit -m @'
feat(ai): add POST /api/ai/summarize RAG endpoint with cited sources

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 5: `ai_bulk_tag` job handler + routes

**Files:**
- Modify: `document_search/app.py` (register `ai_bulk_tag` handler; add `POST /api/ai/bulk-tag/start`; extend `GET /api/ai/jobs/{id}`)
- Test: `tests/test_ai_bulk_tag.py` (extend — job + route + provenance + ACL)

The handler mirrors `_handle_ai_reorganize` (`app.py:350-391`): owner-scoped document selection, per-document `organizer.suggest()`, but adds **validation before persistence** and **provenance recording**. Selection is ACL-scoped using the same `visible_document_ids_subquery` that `search_service` uses (`search_service.py:72-75`), keyed on the job's `owner_user_id`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ai_bulk_tag.py`:

```python
import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from document_search.app import create_app  # noqa: E402
from document_search.index.sqlite_store import SqliteStore  # noqa: E402
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
        doc_ids = _seed_two_docs(db_path)
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
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_ai_bulk_tag.py -v
```

Expected: the four new tests fail with 404/405 (route absent).

- [ ] **Step 3: Register the `ai_bulk_tag` handler**

In `document_search/app.py`, after the existing `@worker.handler("ai_reorganize")` block (which ends at `app.py:391`), append:

```python
    @worker.handler("ai_bulk_tag")
    def _handle_ai_bulk_tag(payload: dict, progress_cb):
        import hashlib

        from document_search.services.acl_service import visible_document_ids_subquery
        from document_search.services.ai_validation import (
            AiValidationError,
            validate_tag_suggestion,
        )

        owner_id = payload["owner_user_id"]
        limit = min(int(payload.get("limit", 20)), 200)
        apply = bool(payload.get("apply", False))

        db = SqliteStore(Path(db_path))
        acl_sql, acl_params = visible_document_ids_subquery(owner_id)
        rows = db.conn.execute(
            f"SELECT id, path, filename, extension FROM documents "
            f"WHERE id IN ({acl_sql}) ORDER BY id LIMIT ?",
            (*acl_params, limit),
        ).fetchall()

        state = {"total": len(rows), "done": 0, "results": []}
        progress_cb(dict(state))

        for doc in rows:
            blocks = db.conn.execute(
                "SELECT text FROM content_blocks WHERE document_id=? LIMIT 6",
                (doc["id"],),
            ).fetchall()
            text = " ".join(b["text"][:500] for b in blocks)
            sug = organizer.suggest(
                file_path=Path(doc["path"]),
                extracted_text=text,
                tags=[],
                metadata={"filename": doc["filename"], "extension": doc["extension"]},
            )
            raw = {"suggested_tags": sug.suggested_tags or []}
            prompt_hash = hashlib.sha256(
                f"{doc['id']}|{doc['path']}|{text[:2000]}".encode("utf-8")
            ).hexdigest()

            try:
                validated = validate_tag_suggestion(raw)
            except AiValidationError as e:
                state["results"].append({
                    "document_id": doc["id"],
                    "filename": doc["filename"],
                    "status": "skipped",
                    "reason": f"validation: {e}",
                    "applied_tags": [],
                })
                state["done"] += 1
                progress_cb({"total": state["total"], "done": state["done"], "results": list(state["results"])})
                continue

            if apply:
                db.set_tags(owner_id, doc["id"], validated.suggested_tags)
            try:
                db.record_ai_decision(
                    kind="bulk_tag",
                    model=sug.model,
                    prompt_sha256=prompt_hash,
                    document_id=doc["id"],
                    output={"suggested_tags": validated.suggested_tags},
                    applied=1 if apply else 0,
                    user_id=owner_id,
                )
            except Exception:  # provenance is secondary to the tag write
                pass

            state["results"].append({
                "document_id": doc["id"],
                "filename": doc["filename"],
                "status": "applied" if apply else "proposed",
                "reason": sug.reason,
                "applied_tags": validated.suggested_tags if apply else [],
                "proposed_tags": validated.suggested_tags,
            })
            state["done"] += 1
            progress_cb({"total": state["total"], "done": state["done"], "results": list(state["results"])})

        return {"total": state["total"], "done": state["done"], "results": state["results"]}
```

- [ ] **Step 4: Add the start route**

In `document_search/app.py`, after the `api_ai_reorganize_start` route (ends `app.py:1501`), add:

```python
    @app.post("/api/ai/bulk-tag/start")
    def api_ai_bulk_tag_start(
        req: dict,
        x_auth_token: str | None = Header(default=None),
    ):
        admin_id = require_admin(x_auth_token)
        try:
            limit = int(req.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
        apply = bool(req.get("apply", False))
        job_id = job_store.enqueue(
            "ai_bulk_tag",
            payload={"owner_user_id": admin_id, "limit": limit, "apply": apply},
            owner_user_id=admin_id,
            max_retries=0,
        )
        return {"job_id": str(job_id)}
```

- [ ] **Step 5: Extend `GET /api/ai/jobs/{id}` to serve `ai_bulk_tag`**

In `document_search/app.py`, in `api_ai_job` (`app.py:1450`), the kind guard currently reads:

```python
            if job and job["kind"] in ("ai_suggest_structure", "ai_reorganize"):
```

Change it to include the new kind:

```python
            if job and job["kind"] in ("ai_suggest_structure", "ai_reorganize", "ai_bulk_tag"):
```

`ai_bulk_tag` shares the `ai_reorganize` response shape (`total`/`done`/`results`), and the existing `else:` branch (`app.py:1475-1479`) already produces exactly that from `result_json`/`progress_json` for any kind that is not `ai_suggest_structure`. No further change to the response shaping is required.

- [ ] **Step 6: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_ai_bulk_tag.py -v
```

Expected: 7 passing (3 provenance/store from Task 1 + 4 job/route).

- [ ] **Step 7: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: previous total + 4, zero failures. In particular `tests/test_app_jobs.py` must still pass unchanged (the `api_ai_job` guard change is additive).

- [ ] **Step 8: Commit**

```powershell
git add document_search/app.py tests/test_ai_bulk_tag.py
git commit -m @'
feat(ai): add ai_bulk_tag job, /api/ai/bulk-tag/start, with validation and provenance

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 6: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Run full suite cleanly**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: green, zero failures, no flaky reruns. If a RAG/bulk-tag test is flaky on timing, increase the `_wait` timeout — never add a blanket `sleep`.

- [ ] **Step 2: Smoke test the RAG endpoint end-to-end (stubbed model)**

```powershell
$env:PYTHONPATH = "."; python -c @'
import json, tempfile, pathlib
from datetime import UTC, datetime
from fastapi.testclient import TestClient
from document_search.app import create_app
from document_search.index.sqlite_store import SqliteStore
from document_search.services.ai_organizer import AiOrganizer

tmp = pathlib.Path(tempfile.mkdtemp())
db_path = tmp / "smoke.db"
app = create_app(str(db_path))

AiOrganizer.is_available = lambda self: True
def fake(self, prompt, *, format=None, options=None, timeout=None):
    return {"response": json.dumps({"summary": "Revenue rose [S1].", "citations": ["S1", "S9"]})}
AiOrganizer._generate = fake

with TestClient(app) as c:
    tok = c.post("/api/login", json={"username":"admin","password":"admin"}).json()["token"]
    db = SqliteStore(db_path); now = datetime.now(tz=UTC).isoformat()
    cur = db.conn.execute("INSERT INTO documents(path,filename,extension,file_size,modified_at,sha256,indexed_at,status) VALUES(?,?,?,?,?,?,?,?)",
                          ("/d/q3.pdf","q3.pdf",".pdf",10,now,"x",now,"ok"))
    did = cur.lastrowid
    bc = db.conn.execute("INSERT INTO content_blocks(document_id,block_type,block_number,text,extractor,text_length,metadata_json) VALUES(?,?,?,?,?,?,?)",
                         (did,"paragraph",0,"Revenue rose twelve percent.","t",10,"{}"))
    db.conn.execute("INSERT INTO content_fts(document_id,block_id,path,filename,extension,block_type,block_number,text) VALUES(?,?,?,?,?,?,?,?)",
                    (did,bc.lastrowid,"/d/q3.pdf","q3.pdf",".pdf","paragraph","0","Revenue rose twelve percent."))
    db.conn.commit(); db._backfill_acl()
    r = c.post("/api/ai/summarize", headers={"X-Auth-Token": tok}, json={"query":"revenue","k":5}).json()
    print("summary =", r["summary"])
    print("citations =", r["citations"])
    print("sources =", [(s["label"], s["document_id"], s["block_number"], s["cited"]) for s in r["sources"]])
    assert r["citations"] == ["S1"], r["citations"]   # S9 hallucination dropped
    assert r["sources"][0]["block_number"] == 0
print("OK")
'@
```

Expected: prints the summary, `citations = ['S1']`, a source tuple with the real block number, then `OK`.

- [ ] **Step 3: Verify provenance rows exist after a bulk-tag run (optional, manual)**

```powershell
$env:PYTHONPATH = "."; python -c @'
import json, tempfile, pathlib, time
from datetime import UTC, datetime
from fastapi.testclient import TestClient
from document_search.app import create_app
from document_search.index.sqlite_store import SqliteStore
from document_search.services.ai_organizer import AiOrganizer, OrganizationSuggestion

tmp = pathlib.Path(tempfile.mkdtemp()); db_path = tmp / "smoke2.db"
app = create_app(str(db_path))
AiOrganizer.suggest = lambda self, *, file_path, extracted_text="", tags, metadata: OrganizationSuggestion(suggested_tags=["invoice"], reason="r", model="llama3.2")
with TestClient(app) as c:
    tok = c.post("/api/login", json={"username":"admin","password":"admin"}).json()["token"]
    db = SqliteStore(db_path); now = datetime.now(tz=UTC).isoformat()
    db.conn.execute("INSERT INTO documents(path,filename,extension,file_size,modified_at,sha256,indexed_at,status) VALUES(?,?,?,?,?,?,?,?)",
                    ("/d/a.pdf","a.pdf",".pdf",10,now,"x",now,"ok")); db.conn.commit(); db._backfill_acl()
    jid = c.post("/api/ai/bulk-tag/start", headers={"X-Auth-Token": tok}, json={"limit":10,"apply":True}).json()["job_id"]
    for _ in range(100):
        b = c.get(f"/api/ai/jobs/{jid}", headers={"X-Auth-Token": tok}).json()
        if b["status"] in ("finished","failed"): break
        time.sleep(0.05)
    print("job =", b["status"], b["results"])
    print("decisions =", [dict(d) for d in SqliteStore(db_path).list_ai_decisions(kind="bulk_tag")])
print("OK")
'@
```

Expected: job `finished`, one decision row with `applied=1` and a `prompt_sha256`, then `OK`.

- [ ] **Step 4: No commit (verification only).**

---

## Definition of Done

- [ ] `pytest -q` is green with zero failures and zero flaky reruns.
- [ ] `ai_decisions` table exists with all 9 columns + 3 indexes; `record_ai_decision()` / `list_ai_decisions()` are the only writers/readers.
- [ ] `ai_validation.py` exists, is pure (no Ollama/DB imports at module scope), and exports `AiValidationError`, `validate_summary`, `validate_tag_suggestion`, `validate_structure`.
- [ ] `validate_summary` drops any citation not in the supplied source set (hallucination guard); a unit test proves a fake `[S9]` is removed.
- [ ] `AiOrganizer.summarize_with_citations()` builds a prompt addressing each source by its real `(document_id, block_number)` and returns a validated `RagSummary`; tests stub `_generate` only — no live model.
- [ ] `POST /api/ai/summarize` (require_user) retrieves top-K via the existing ACL-filtered `search()`, returns `{summary, citations, sources}` where every source carries a real `document_id` + `block_number`; returns `502` when Ollama is unavailable and `422` on invalid AI output.
- [ ] `ai_bulk_tag` job (require_admin to start) iterates ACL/owner-scoped documents, validates every suggestion before writing, applies tags via `set_tags()` only when `apply=True`, and records one `ai_decisions` row per processed document (`applied` reflecting mode).
- [ ] `GET /api/ai/jobs/{id}` serves `ai_bulk_tag` with the same `total/done/results` shape as `ai_reorganize`; non-admin/non-owner users still get 404 (inherited from existing guard).
- [ ] Existing behaviour unchanged: `suggest()`, `suggest_structure()`, `ask()`, `ai_suggest_structure`/`ai_reorganize` handlers, `/api/ai/pull-model`, `/api/system/update`, and all prior tests pass untouched.
- [ ] Both manual smoke tests pass.

---

## Notes for the executing agent

- **Why RAG is synchronous and not a job:** the persistent queue (see `2026-05-17-job-queue.md`) exists for long fan-out work (indexing, reorganize). A single top-K summary is one short `generate` call; routing it through enqueue → poll → GET would add latency and a polling UX for no benefit. If you later want a "summarise the whole corpus" feature, that *would* be a job — but it is out of scope here.
- **Why a `_generate()` seam instead of stubbing `urllib`:** the existing `suggest()`/`suggest_structure()` each inline their own `urllib.request.urlopen` call, which is awkward to monkeypatch cleanly. Adding one private method that the *new* code routes through gives tests a single, stable patch point and keeps the old methods byte-for-byte unchanged (lower risk). Do **not** refactor the old methods onto `_generate` in this plan.
- **Citation integrity is the load-bearing guarantee.** The validator (`validate_summary`) is what makes citations trustworthy: it intersects the model's claimed labels with the labels actually supplied. Never return a citation the model invented. The endpoint additionally marks each source `cited: true/false` so the UI can render which blocks were used — but the authoritative list is `citations`.
- **ACL inheritance is free for RAG.** `summarize` calls `search(db, query, k, ..., user_id)` with the caller's `user_id` and no `bypass_acl`, so it inherits exactly the same visibility filtering as `/api/search` (`app.py:1584`). Do not add a second ACL check — that would risk drift. For bulk-tag, the handler runs in a worker thread (no request `user_id`), so it re-derives the scope from the job payload's `owner_user_id` via `visible_document_ids_subquery` — the same SQL fragment `search_service` uses.
- **Tags are per-user.** `user_tags`/`document_tags` are keyed on `user_id` (`sqlite_store.py:66-82`). Bulk-tag writes under the requesting admin's id. This matches `set_tags(user_id, document_id, tags)` (`sqlite_store.py:385`). Do not try to write "global" tags — that schema does not exist.
- **Provenance never blocks the user-visible effect.** In the bulk-tag handler, the `record_ai_decision` call is wrapped in `try/except: pass` *after* the tag write, so an audit hiccup cannot lose a tag. Validation, by contrast, happens *before* the write and is mandatory — a validation failure means no tag and no `applied` provenance.
- **`req: dict` body, not a Pydantic model:** the two new POST routes accept a raw `dict` body to avoid touching the central Pydantic model block and to keep the diff localised. This matches the lightweight query-param style already used by `api_ai_suggest_structure` and `api_ai_reorganize_start`. If the project later standardises request models, wrap these — but not in this plan.
- **The `_backfill_acl()` calls in the test seeds are required:** tests insert documents directly via SQL, bypassing the normal ingestion path that grants `public` read. Calling `db._backfill_acl()` after seeding grants the new docs to `public` so the ACL-filtered `search()`/bulk-tag selection can see them — without it, retrieval returns nothing and the assertions on real block numbers cannot run.
- **Test counts are intentionally not hard-coded against a global baseline** (the suite total shifts as other branches land). Each task states the count for *its own* new file/slice and asserts "previous total + N" for the full run. If the full run shows fewer than expected, a prior test regressed — investigate with `pytest -q --last-failed -v` before proceeding.
