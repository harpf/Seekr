# Design: Search Fixes + Smart Autocomplete

**Date:** 2026-05-15  
**Status:** Approved

---

## Overview

Fixes four bugs in search and indexing, adds two backend endpoints, and introduces a reusable chip-input component wired across search filters, upload form, and the ingest page.

---

## Section 1: Bug Fixes

### 1.1 Wildcard `*` → 500 Internal Server Error

**Root cause:** FTS5 treats a bare `*` as invalid syntax. The exception is unhandled, causing a 500.

**Fix:**
- In `api_search` (app.py), catch `sqlite3.OperationalError` and return HTTP 400 with a descriptive message.
- In `build_match_query` (search_service.py), if `query` is `*` or blank, skip the FTS `MATCH` clause entirely and fall back to a plain SQL `SELECT` from `documents JOIN content_blocks`, applying date/tag/path filters via regular WHERE conditions. This gives a useful "browse all" mode.
- Remove `min_length=1` from `SearchRequest.query` so empty-query searches are valid when other filters are present.

### 1.2 `[object Object]` Error Display

**Root cause:** FastAPI 422 validation errors return `detail` as an array of objects. The frontend does `String(array)` which yields `"[object Object]"`.

**Fix:** In the `api()` function in `app.js`:
```js
if (Array.isArray(detail)) {
  msg = detail.map(e => e.msg || String(e)).join('; ');
} else {
  msg = String(detail ?? text);
}
```

### 1.3 Extension Filter — Comma-Separated Values Not Handled

**Root cause:** If the user types `pdf,docx`, the whole string becomes one `extension:` clause in FTS5, which fails or returns nothing.

**Fix:** In `build_match_query`, split `filetype` on commas and build `(extension:pdf OR extension:docx)`. Strip leading dots from user input since FTS5 tokenizes `.pdf` as `pdf` anyway.

### 1.4 Indexing with `/` Silently Stalls

**Root cause:** `os.walk("/")` traverses the full Linux filesystem including `/proc`, `/sys`, `/dev`. The exclude_dirs list does not cover system paths, so the walker stalls or processes millions of irrelevant entries.

**Fix (backend):** In `api_index_start`, reject any path that is `/` or starts with `/proc`, `/sys`, `/dev` with HTTP 400 and a clear message.

**Fix (frontend):** On ingest page load, call `GET /api/config` and pre-populate the paths input with the configured `source_paths[].path` values (joined by `, `). User can still edit before submitting.

---

## Section 2: New Backend Endpoints

### 2.1 `GET /api/index/extensions`

Returns distinct file extensions present in the `documents` table, ordered by document count descending. Requires session token.

**Response:**
```json
[".pdf", ".docx", ".txt"]
```

**Implementation:** `SELECT extension, COUNT(*) FROM documents GROUP BY extension ORDER BY COUNT(*) DESC`

### 2.2 `GET /api/folders`

Returns existing subdirectories inside the upload root (`DOCUMENT_SEARCH_UPLOAD_ROOT`, default `/data/uploads`), as relative paths. Requires session token.

**Response:**
```json
["incoming/2026", "customers/acme", "archive"]
```

**Implementation:** `os.walk(upload_root)` limited to depth 2, yielding relative directory paths. Returns `[]` gracefully if upload root does not exist.

---

## Section 3: Frontend — Chip Input Component + Wiring

### 3.1 ChipInput Component (`app.js`)

A reusable component managing a set of string values rendered as dismissible chips.

**Behaviour:**
- Wraps an existing `<input>` element and an optional `<datalist>` for autocomplete suggestions.
- Pressing Enter or `,` while focused on the input adds the current value as a chip (if non-empty and not a duplicate).
- Each chip renders as `value ×`; clicking × removes it.
- `ChipInput.values()` returns the current array of strings for use in API payloads.
- `ChipInput.setOptions(arr)` replaces the datalist options.

**CSS additions to `styles.css`:**
- `.chip-wrap` — flex container with wrap, gap, border, and padding matching existing `.f-col` inputs.
- `.chip` — pill shape using existing `--accent` / `--bg-2` tokens, with a small × button.
- `.chip-input` — borderless input inside the wrap that grows to fill remaining space.

### 3.2 Wiring

| Page | Field | Component | Data source |
|---|---|---|---|
| Search | File type filter | ChipInput (multi) | `GET /api/index/extensions` on filter open |
| Search | Tag filter | plain `<input>` + `<datalist>` (single value) | `GET /api/tags` on filter open |
| Upload (Ingest) | Tags | ChipInput (multi) | `GET /api/tags` after login |
| Upload (Ingest) | Target folder | plain `<input>` + `<datalist>` | `GET /api/folders` after login |
| Ingest | Paths to index | plain `<input>` pre-populated | `GET /api/config` source_paths on load |

The tag filter in search is a single-value field (backend supports one tag at a time), so it uses a plain datalist rather than ChipInput to avoid implying multiple tags work. The folder field is also a single-value text input with browser-native datalist autocomplete (free text accepted; new folders are created by the existing upload handler).

### 3.3 API Payload Changes

- `runSearch()`: `filetype` becomes `chipInputFiletype.values().join(',') || null`; tag filter stays as a single plain input value.
- `uploadDocument()`: tags become `chipInputUploadTags.values().join(',')`.
- `startIndex()`: paths value read from pre-populated input as before.

---

## Out of Scope

- Multi-tag search filtering (backend currently supports one tag at a time).
- Persistent filter state across page reloads.
- Server-side folder creation endpoint (already handled implicitly by the upload endpoint).
