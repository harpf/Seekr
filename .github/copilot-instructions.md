# Copilot Instructions

## Product & Architecture Context
This repository builds a **document and file indexing/search tool** with a local-first approach.
The target architecture supports ingestion, extraction, indexing, and modern web-based discovery, with optional **local LLM integration** for structured organization and metadata enrichment.

Primary structure:
- `document_search/crawler.py` → source traversal/ingestion
- `document_search/extractors/` → file-type extraction
- `document_search/services/` → reusable domain services (OCR, hashing, AI organizer, file service)
- `document_search/index/` → indexing and query behavior
- `document_search/app.py` + `document_search/web/` → API/web UI layer

## Coding Expectations
- Prefer maintainable, production-ready changes over shortcuts.
- Keep changes small and focused unless a refactor is clearly beneficial.
- Add type hints for new or modified Python code.
- Use robust error handling and meaningful logging.
- Do not introduce secrets, hardcoded credentials, or machine-specific paths.
- Preserve clear module boundaries between ingestion, extraction, indexing, and UI.

## Local LLM Integration Guidelines
- Keep LLM calls behind service abstractions.
- Validate model outputs before persistence.
- Design for degraded mode (timeouts/unavailable model/fallback behavior).
- Keep processing local by default unless explicitly configured otherwise.

## UI/UX Guidelines (Modern & Clean)
- Keep templates componentized and readable.
- Support clear UI states: loading, empty results, success, and error.
- Present results with readable snippets, highlights, and source metadata.
- Prefer responsive and accessible patterns.
- Keep JS logic maintainable and CSS naming consistent.

## Testing & Validation
When behavior changes, update or add tests in `tests/` and run:
- `pytest -q`
- if needed for local import context: `PYTHONPATH=. pytest -q`
