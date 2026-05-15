# Design: Search Fixes + Smart Autocomplete + Grouped Results

**Date:** 2026-05-15  
**Status:** Approved

---

## Overview

Fixes four bugs in search and indexing, adds two backend endpoints, introduces a reusable chip-input component, upgrades the tag filter to multi-tag (AND logic), and redesigns search results to group all matching snippets per document with full hit highlighting.

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

**Fix (frontend):** Replace the free-text paths input with a `<select multiple>` dropdown populated from `GET /api/source-folders` (see Section 2.3). Each option is either a configured source root or a direct subdirectory of one. The user selects one or more entries; the selected paths are sent as the `paths` array to `/api/index/start`.

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

### 2.3 `GET /api/source-folders`

Returns the configured source paths and their immediate subdirectories, for use in the ingest path selector. Requires session token.

**Response:**
```json
[
  { "path": "/mnt/windows-docs", "label": "Windows Share", "is_root": true },
  { "path": "/mnt/windows-docs/invoices", "label": "invoices", "is_root": false },
  { "path": "/mnt/windows-docs/reports", "label": "reports", "is_root": false },
  { "path": "/documents", "label": "Documents", "is_root": true }
]
```

**Implementation:**
1. Read `source_paths` from config.
2. For each configured path that exists on disk, yield it as a root entry (using its configured `label` or path basename).
3. List its immediate subdirectories (`os.scandir`, one level deep) and yield each as a non-root entry.
4. Skip paths that do not exist on disk (mount not active). Returns `[]` if no source paths are configured.

**Frontend rendering:** `<select multiple>` with `<optgroup label="…">` per source root, containing its subdirectories as `<option>` entries. The root itself is always selectable (as the first option inside its optgroup). Height is capped at ~8 visible rows.

---

## Section 3: Multi-Tag Search (AND Logic)

### 3.1 Backend Changes

`SearchRequest.tag: str | None` → `SearchRequest.tags: list[str]` (default `[]`).

`search()` in `search_service.py`: when `tags` is non-empty, filter documents to those that carry **all** specified tags (AND semantics):

```sql
AND d.id IN (
    SELECT dt.document_id
    FROM document_tags dt
    JOIN user_tags ut ON ut.id = dt.tag_id
    WHERE dt.user_id = ? AND ut.name IN (?, ?, ...)
    GROUP BY dt.document_id
    HAVING COUNT(DISTINCT ut.name) = <len(tags)>
)
```

Parameters: `user_id` followed by each tag name (lowercased/stripped), then `len(tags)` for the HAVING clause.

`api_search` passes `req.tags` (list) instead of `req.tag`.

### 3.2 Frontend

Tag filter in search becomes a **ChipInput** (multi-value). Each chip is an individual tag. `runSearch()` sends `tags: chipInputTagFilter.values()` (array). Empty array = no tag filter.

---

## Section 4: Grouped Search Results with Full Hit Highlighting

### 4.1 Backend — Grouped Response Shape

`api_search` groups the flat per-block rows by `document_id` before returning. The response changes from a flat list to a list of document objects, each containing all matching hits:

```json
[
  {
    "document_id": 42,
    "filename": "report.pdf",
    "path": "/mnt/windows-docs/report.pdf",
    "extension": ".pdf",
    "modified_at": "2026-03-01T10:00:00",
    "is_marked": false,
    "tags": ["invoice"],
    "open_url": "/api/files/open?document_id=42",
    "hit_count": 3,
    "hits": [
      { "block_type": "page", "block_number": 1, "snippet_html": "...highlighted..." },
      { "block_type": "page", "block_number": 5, "snippet_html": "...highlighted..." },
      { "block_type": "table", "block_number": 2, "snippet_html": "...highlighted..." }
    ]
  }
]
```

Documents are ordered by best FTS5 rank across their hits (lowest rank = best match first). The existing `highlight_terms()` function is applied to each snippet individually.

### 4.2 Backend — Fallback (browse-all) Shape

When query is empty/`*`, the fallback plain SQL path returns rows without FTS snippets. Each hit gets `snippet_html: null` and `block_type`/`block_number` from `content_blocks`. Results are ordered by `d.modified_at DESC`.

### 4.3 Frontend — Result Card Redesign

One card per document. Each card shows:

```
┌─ report.pdf ────────────────────────────────── ⭐? ─┐
│ /mnt/windows-docs/report.pdf    .pdf  3 hits         │
│ [invoice] [customer-a]                               │
│                                                      │
│ [page 1]   …the quick brown fox jumped over…         │
│ [page 5]   …as brown as the earth beneath…           │
│ [table 2]  …a brown-colored material was found…      │
│                                                      │
│ [Save tags ▾]  [Mark]  [Reindex]  [Open file]        │
└──────────────────────────────────────────────────────┘
```

- Filename is a link (same as Open file).
- Hit snippets are shown in a compact list, each prefixed with a `[block_type N]` label.
- If `snippet_html` is null (browse-all mode), show a placeholder "—" for that hit.
- The tag editor (currently an inline `<input>`) stays in the card footer, but becomes a ChipInput using the same component as the upload form — so existing tags are pre-filled as chips and saving replaces them.

### 4.4 Snippet Limit per Document

Show at most **5 hits** per document card by default. If `hit_count > 5`, show a "Show N more hits" expander that reveals the rest inline (no network call needed — all hits are already in the response).

---

## Section 5: Frontend — Chip Input Component + Wiring

### 5.1 ChipInput Component (`app.js`)

A reusable component managing a set of string values rendered as dismissible chips.

**Behaviour:**
- Wraps an existing `<input>` element and an optional `<datalist>` for autocomplete suggestions.
- Pressing Enter or `,` while focused on the input adds the current value as a chip (if non-empty and not a duplicate).
- Each chip renders as `value ×`; clicking × removes it.
- `ChipInput.values()` returns the current array of strings for use in API payloads.
- `ChipInput.setOptions(arr)` replaces the datalist options.
- `ChipInput.setValues(arr)` pre-populates chips (used to pre-fill tags in the result card editor).

**CSS additions to `styles.css`:**
- `.chip-wrap` — flex container with wrap, gap, border, and padding matching existing `.f-col` inputs.
- `.chip` — pill shape using existing `--accent` / `--bg-2` tokens, with a small × button.
- `.chip-input` — borderless input inside the wrap that grows to fill remaining space.

### 5.2 Wiring

| Page | Field | Component | Data source |
|---|---|---|---|
| Search | File type filter | ChipInput (multi) | `GET /api/index/extensions` on filter open |
| Search | Tag filter | ChipInput (multi) | `GET /api/tags` on filter open |
| Search results | Tag editor per card | ChipInput (multi, pre-filled) | existing tags from result data |
| Upload (Ingest) | Tags | ChipInput (multi) | `GET /api/tags` after login |
| Upload (Ingest) | Target folder | plain `<input>` + `<datalist>` | `GET /api/folders` after login |
| Ingest | Paths to index | `<select multiple>` with optgroups | `GET /api/source-folders` after login |

The folder field is a single-value text input with browser-native datalist autocomplete (free text accepted; new folders are created by the existing upload handler).

### 5.3 API Payload Changes

- `runSearch()`: `filetype` → `chipInputFiletype.values().join(',') || null`; `tags` → `chipInputTagFilter.values()` (array, may be empty).
- `saveTags(documentId)`: reads from ChipInput in the result card instead of a plain text input.
- `uploadDocument()`: tags → `chipInputUploadTags.values().join(',')`.
- `startIndex()`: paths read from all selected `<option>` values in the `<select multiple>` element.

---

## Out of Scope

- Persistent filter state across page reloads.
- Server-side folder creation endpoint (handled implicitly by the existing upload endpoint).
