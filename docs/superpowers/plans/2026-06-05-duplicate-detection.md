# Duplicate Detection & Dedup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface exact-duplicate and near-duplicate documents to admins, and let them keep one document per group while removing the rest. Seekr already stores a per-document `sha256` (see `document_search/services/file_service.py:fingerprint` and the `documents.sha256` column in `sqlite_store.py`). This plan adds (1) a `content_hash` column for near-duplicate detection that does **not** depend on embeddings, (2) two `SqliteStore` query helpers that return duplicate groups, (3) an admin `GET /api/documents/duplicates` endpoint plus a `POST /api/documents/duplicates/remove` action, and (4) a Duplicates panel in Ingest → Maintenance.

**Architecture:** All hashing and grouping live in already-existing layers. `content_hash` is a normalized-content fingerprint computed from the indexed text blocks (lowercased, whitespace-collapsed, first 8 KB of concatenated block text), stored as a new nullable column on `documents`. The column is filled at `upsert_document` time and backfilled idempotently at startup. Grouping is pure SQL (`GROUP BY <hash> HAVING COUNT(*) > 1`). The remove action reuses the exact deletion pattern already proven in `SqliteStore.remove_missing` (delete from `content_blocks`, `content_fts`, then `documents`; ACL/marks/tags cascade via existing `ON DELETE CASCADE` foreign keys). The endpoint is ACL-aware: it lists/acts only on documents the calling admin can read, via `acl_service.visible_document_ids_subquery`.

**Tech Stack:** Python 3.11, SQLite, pytest. Frontend is vanilla JS (`document_search/web/static/app.js`) + a Jinja template (`document_search/web/templates/ingest.html`). No new third-party dependencies.

**Chosen near-duplicate approach (locked):** A **normalized-content hash**. For each document we take the concatenated text of its `content_blocks` (in `block_number` order), lowercase it, collapse all runs of whitespace to a single space, strip, truncate to the first 8192 characters, and `sha256` the result. Two files that differ only in byte-level encoding, trailing whitespace, casing, or re-export (e.g. the same report saved twice with a different PDF producer) will share a `content_hash` even when their raw-bytes `sha256` differs. This is cheap, deterministic, embedding-free, and reuses text Seekr already extracts. It is intentionally a *heuristic*: it catches "very likely the same content" not "semantically similar". Shingling/MinHash and embedding-based similarity are explicitly out of scope (a future plan may layer them on if the semantic-search work lands).

**Scope boundaries (out of scope for this plan):**
- No embeddings, no MinHash/SimHash, no fuzzy similarity scoring — only exact `sha256` groups and exact `content_hash` groups.
- No automatic merge of tags/marks across kept vs removed docs — "remove" deletes the duplicate's index rows; the kept document is untouched. (Cascades drop the removed doc's own marks/tags/ACL.)
- No deletion of the file on disk — the action only removes the document from the index. The path stays on disk and would re-appear on the next crawl unless excluded (file-level exclusion is a separate feature).
- No background job — grouping and removal are synchronous admin requests (groups are small relative to the corpus; this is not a crawl).
- No per-user duplicate view — duplicates is an admin tool gated by `require_admin`.

---

## File Structure

**Create:**
- `tests/test_duplicate_detection.py` — unit tests for the content-hash helper + the two `SqliteStore` grouping queries.
- `tests/test_app_duplicates.py` — `TestClient` integration tests for the two endpoints.

**Modify:**
- `document_search/services/file_service.py` — add `normalized_content_hash(blocks_text: str) -> str` pure helper.
- `document_search/index/sqlite_store.py` — add `content_hash` column to `documents` (in `_init_schema` create-table + an additive `ALTER TABLE` migration), compute and store it in `upsert_document`, add `_backfill_content_hash()` (called at end of `_init_schema`), and add `find_exact_duplicate_groups()` + `find_content_duplicate_groups()` + `delete_documents(ids)`.
- `document_search/app.py` — add `GET /api/documents/duplicates` and `POST /api/documents/duplicates/remove`.
- `document_search/web/templates/ingest.html` — add a "Duplicate Documents" card in the Maintenance area.
- `document_search/web/static/app.js` — add `loadDuplicates()`, `renderDuplicateGroups()`, `removeDuplicates()`.

**Read-only references (study, do not edit):**
- `document_search/services/acl_service.py:visible_document_ids_subquery` — ACL filter, reused by the endpoint.
- `document_search/index/sqlite_store.py:remove_missing` (lines ~455–465) — the exact deletion pattern to mirror in `delete_documents`.
- `document_search/app.py:504-513` (`require_admin`) and `app.py:640-645` (`/api/index/cleanup`) — admin endpoint + response shape conventions.

---

## Key design decisions (locked)

- **`content_hash` is nullable.** Documents with no extracted text (e.g. extraction errors, image-only PDFs) get `NULL` and are excluded from content-duplicate grouping. Exact `sha256` grouping still covers them.
- **Grouping queries return groups, not pairs.** Each group is `{hash, count, documents: [{id, path, filename, file_size, modified_at, indexed_at}]}`, ordered so the operator can pick which to keep.
- **The endpoint is ACL-filtered.** A group is only shown if *every* member is visible to the calling admin; members the admin can't read are filtered out, and a group that collapses to a single visible member is dropped. This prevents an admin from acting on documents outside their ACL scope. (Admins go through the same ACL as everyone else — consistent with the ACL-foundation plan.)
- **Remove validates membership.** `POST .../remove` accepts a `keep_id` and `remove_ids`. The server re-derives the group of `keep_id`, confirms every `remove_id` is in that same group (same `sha256` OR same `content_hash`) AND visible to the admin, then deletes. This stops a malformed/malicious request from deleting arbitrary documents.
- **`delete_documents` mirrors `remove_missing`.** Same three deletes (`content_blocks`, `content_fts`, `documents`) in the same order. FK cascades handle `document_acl`, `document_tags`, `user_document_marks`.

---

## Task 1: `normalized_content_hash` helper

**Files:**
- Modify: `document_search/services/file_service.py`
- Test: `tests/test_duplicate_detection.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_duplicate_detection.py`:

```python
from pathlib import Path

import pytest

from document_search.services.file_service import normalized_content_hash


def test_identical_text_same_hash():
    a = normalized_content_hash("Hello World")
    b = normalized_content_hash("Hello World")
    assert a == b


def test_whitespace_and_case_insensitive():
    a = normalized_content_hash("Hello   World\n\tFoo")
    b = normalized_content_hash("  hello world foo  ")
    assert a == b


def test_different_text_different_hash():
    a = normalized_content_hash("the quick brown fox")
    b = normalized_content_hash("a slow green turtle")
    assert a != b


def test_empty_text_returns_none():
    assert normalized_content_hash("") is None
    assert normalized_content_hash("   \n\t  ") is None


def test_only_first_8kb_considered():
    base = "x" * 8192
    a = normalized_content_hash(base + "AAAA")
    b = normalized_content_hash(base + "BBBB")
    # Tails beyond 8 KB are truncated, so the hashes collide.
    assert a == b


def test_returns_hex_string():
    h = normalized_content_hash("some content here")
    assert isinstance(h, str)
    assert len(h) == 64  # sha256 hex digest
    int(h, 16)  # valid hex
```

- [ ] **Step 2: Run, expect FAIL (ImportError)**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_duplicate_detection.py -v
```

Expected: ImportError — `normalized_content_hash` doesn't exist yet.

- [ ] **Step 3: Implement the helper**

In `document_search/services/file_service.py`, add the import and function. The full file becomes:

```python
from __future__ import annotations

import re
from datetime import datetime, UTC
from hashlib import sha256
from pathlib import Path

from document_search.models import FileFingerprint
from document_search.services.hash_service import sha256_file

_WS_RE = re.compile(r"\s+")
_CONTENT_HASH_PREFIX_BYTES = 8192


def fingerprint(path: Path) -> FileFingerprint:
    stat = path.stat()
    return FileFingerprint(
        path=path.resolve(),
        file_size=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        sha256=sha256_file(path),
    )


def normalized_content_hash(text: str) -> str | None:
    """Embedding-free near-duplicate fingerprint.

    Lowercases the text, collapses every run of whitespace to a single space,
    strips, truncates to the first 8 KB, and returns its sha256 hex digest.
    Two documents whose textual content matches after this normalization share
    a hash even when their raw bytes (and therefore file `sha256`) differ —
    e.g. the same report re-exported by a different PDF producer.

    Returns None when the normalized text is empty (no meaningful content to
    fingerprint), so such documents are excluded from content-duplicate groups.
    """
    if not text:
        return None
    normalized = _WS_RE.sub(" ", text).strip().lower()
    if not normalized:
        return None
    truncated = normalized[:_CONTENT_HASH_PREFIX_BYTES]
    return sha256(truncated.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_duplicate_detection.py -v
```

Expected: 6 passing.

- [ ] **Step 5: Commit**

```powershell
git add document_search/services/file_service.py tests/test_duplicate_detection.py
git commit -m "feat(dedup): normalized_content_hash helper for near-duplicate detection"
```

---

## Task 2: `content_hash` column + migration + backfill

**Files:**
- Modify: `document_search/index/sqlite_store.py`
- Test: `tests/test_duplicate_detection.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_duplicate_detection.py`:

```python
from document_search.index.sqlite_store import SqliteStore


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "test.db")


def _insert_doc(store, path, sha, *, content_hash=None, status="ok"):
    from datetime import UTC, datetime
    now = datetime.now(tz=UTC).isoformat()
    cur = store.conn.execute(
        "INSERT INTO documents(path, filename, extension, file_size, modified_at, "
        "sha256, content_hash, indexed_at, status) VALUES(?,?,?,?,?,?,?,?,?)",
        (path, Path(path).name, Path(path).suffix, 100, now, sha, content_hash, now, status),
    )
    store.conn.commit()
    return cur.lastrowid


def _insert_block(store, doc_id, text, block_number=1):
    cur = store.conn.execute(
        "INSERT INTO content_blocks(document_id, block_type, block_number, text, "
        "extractor, text_length) VALUES(?,?,?,?,?,?)",
        (doc_id, "paragraph", block_number, text, "txt", len(text)),
    )
    store.conn.commit()
    return cur.lastrowid


def test_documents_has_content_hash_column(store):
    cols = {c[1] for c in store.conn.execute("PRAGMA table_info(documents)").fetchall()}
    assert "content_hash" in cols


def test_backfill_fills_content_hash_from_blocks(store):
    doc_id = _insert_doc(store, "/d/a.txt", "sha-a", content_hash=None)
    _insert_block(store, doc_id, "The Quick   Brown Fox")
    # Re-open to trigger the idempotent backfill at startup.
    store2 = SqliteStore(store.db_path)
    row = store2.conn.execute(
        "SELECT content_hash FROM documents WHERE id=?", (doc_id,)
    ).fetchone()
    from document_search.services.file_service import normalized_content_hash
    assert row["content_hash"] == normalized_content_hash("The Quick   Brown Fox")


def test_backfill_leaves_null_for_documents_without_text(store):
    doc_id = _insert_doc(store, "/d/empty.pdf", "sha-empty", content_hash=None)
    # No content_blocks for this document.
    store2 = SqliteStore(store.db_path)
    row = store2.conn.execute(
        "SELECT content_hash FROM documents WHERE id=?", (doc_id,)
    ).fetchone()
    assert row["content_hash"] is None


def test_backfill_is_idempotent_and_does_not_overwrite(store):
    doc_id = _insert_doc(store, "/d/a.txt", "sha-a", content_hash="preexisting")
    _insert_block(store, doc_id, "ignored because hash already set")
    store2 = SqliteStore(store.db_path)
    row = store2.conn.execute(
        "SELECT content_hash FROM documents WHERE id=?", (doc_id,)
    ).fetchone()
    assert row["content_hash"] == "preexisting"
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_duplicate_detection.py -v
```

Expected: `test_documents_has_content_hash_column` fails (no column), and the backfill tests fail / error.

- [ ] **Step 3: Add the column, migration, and backfill**

In `document_search/index/sqlite_store.py`:

(a) Add `content_hash TEXT` to the `documents` create-table block. Change the `sha256 TEXT NOT NULL,` line (currently line 41) so the column appears right after it:

```sql
              sha256 TEXT NOT NULL,
              content_hash TEXT,
```

(b) Add an index next to `idx_docs_sha256` (after line 105 `CREATE INDEX IF NOT EXISTS idx_docs_sha256 ...`):

```sql
            CREATE INDEX IF NOT EXISTS idx_docs_content_hash ON documents(content_hash);
```

(c) Add an additive `ALTER TABLE` migration so existing databases gain the column. Insert it directly **after** the `principal_id` migration block (after the block that ends at line 178, before the admin-ensure block):

```python
        # Migration: add content_hash column for near-duplicate detection
        try:
            self.conn.execute("ALTER TABLE documents ADD COLUMN content_hash TEXT")
            self.conn.commit()
        except Exception:
            pass
```

(d) At the very end of `_init_schema`, **after** `self._backfill_acl()` (line 187), append:

```python
        self._backfill_content_hash()
```

(e) Add the new `_backfill_content_hash` method directly below `_backfill_acl` (after line 240):

```python
    def _backfill_content_hash(self) -> None:
        """Idempotent: compute content_hash for any document that has indexed text
        but no content_hash yet. Safe to run on every startup. Documents without
        text blocks stay NULL and are excluded from content-duplicate grouping.
        """
        from document_search.services.file_service import normalized_content_hash

        rows = self.conn.execute(
            "SELECT id FROM documents WHERE content_hash IS NULL"
        ).fetchall()
        for row in rows:
            doc_id = row["id"]
            blocks = self.conn.execute(
                "SELECT text FROM content_blocks WHERE document_id=? ORDER BY block_number",
                (doc_id,),
            ).fetchall()
            if not blocks:
                continue
            combined = " ".join(b["text"] for b in blocks)
            ch = normalized_content_hash(combined)
            if ch is not None:
                self.conn.execute(
                    "UPDATE documents SET content_hash=? WHERE id=?", (ch, doc_id)
                )
        self.conn.commit()
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_duplicate_detection.py -v
```

Expected: all Task-1 + Task-2 tests pass.

- [ ] **Step 5: Full suite (catch regressions in upsert/migration)**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: previously-passing tests still pass.

- [ ] **Step 6: Commit**

```powershell
git add document_search/index/sqlite_store.py tests/test_duplicate_detection.py
git commit -m "feat(dedup): add content_hash column with migration and idempotent backfill"
```

---

## Task 3: Compute `content_hash` during `upsert_document`

**Files:**
- Modify: `document_search/index/sqlite_store.py` (`upsert_document`)
- Test: `tests/test_duplicate_detection.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_duplicate_detection.py`:

```python
def test_upsert_document_sets_content_hash(tmp_path):
    from datetime import UTC, datetime
    from document_search.models import (
        ContentBlock,
        ExtractionResult,
        FileFingerprint,
    )
    from document_search.services.file_service import normalized_content_hash

    store = SqliteStore(tmp_path / "test.db")
    p = tmp_path / "report.txt"
    p.write_text("hello body text", encoding="utf-8")
    fp = FileFingerprint(
        path=p,
        file_size=15,
        modified_at=datetime.now(tz=UTC),
        sha256="abc123",
    )
    block = ContentBlock(
        block_type="paragraph",
        block_number=1,
        text="Hello   Body  TEXT",
        extractor="txt",
        metadata={},
    )
    result = ExtractionResult(
        file_path=p,
        status="ok",
        document_metadata={},
        blocks=[block],
        error_message=None,
    )
    doc_id = store.upsert_document(fp, result)
    row = store.conn.execute(
        "SELECT content_hash FROM documents WHERE id=?", (doc_id,)
    ).fetchone()
    assert row["content_hash"] == normalized_content_hash("Hello   Body  TEXT")
```

> If the constructors above don't match `document_search/models.py`, read that file and adjust the kwargs. The point of the test is: after `upsert_document`, `content_hash` equals `normalized_content_hash` of the concatenated block text.

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_duplicate_detection.py::test_upsert_document_sets_content_hash -v
```

Expected: FAIL — `content_hash` is NULL because `upsert_document` doesn't compute it yet.

- [ ] **Step 3: Compute and store content_hash in `upsert_document`**

In `document_search/index/sqlite_store.py`, modify `upsert_document` (lines 248–295).

(a) Add the import at the top of the method body (right after `now = datetime.now(tz=UTC).isoformat()`, line 249):

```python
        from document_search.services.file_service import normalized_content_hash
        combined_text = " ".join(b.text for b in sorted(ext.blocks, key=lambda b: b.block_number))
        content_hash = normalized_content_hash(combined_text)
```

(b) Update the INSERT to include the `content_hash` column. Replace the INSERT statement (lines 253–266) with:

```python
        self.conn.execute(
            """
            INSERT INTO documents(path, filename, extension, file_size, modified_at, sha256, content_hash, indexed_at, status, error_message, page_count, slide_count, metadata_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
              filename=excluded.filename, extension=excluded.extension, file_size=excluded.file_size, modified_at=excluded.modified_at,
              sha256=excluded.sha256, content_hash=excluded.content_hash, indexed_at=excluded.indexed_at, status=excluded.status, error_message=excluded.error_message,
              page_count=excluded.page_count, slide_count=excluded.slide_count, metadata_json=excluded.metadata_json
            """,
            (
                str(fp.path), fp.path.name, fp.path.suffix.lower(), fp.file_size, fp.modified_at.isoformat(), fp.sha256, content_hash, now,
                ext.status, ext.error_message, meta.get("page_count"), meta.get("slide_count"), str(meta),
            ),
        )
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_duplicate_detection.py -v
```

Expected: all duplicate-detection tests pass.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: green. (Note: existing tests that insert documents via raw SQL with a fixed column list still work — they simply leave `content_hash` NULL, which the migration/backfill tolerate.)

- [ ] **Step 6: Commit**

```powershell
git add document_search/index/sqlite_store.py tests/test_duplicate_detection.py
git commit -m "feat(dedup): compute content_hash on upsert_document"
```

---

## Task 4: Grouping queries + `delete_documents` on `SqliteStore`

**Files:**
- Modify: `document_search/index/sqlite_store.py`
- Test: `tests/test_duplicate_detection.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_duplicate_detection.py`:

```python
def test_find_exact_duplicate_groups(store):
    a = _insert_doc(store, "/d/a.pdf", "SHARED")
    b = _insert_doc(store, "/d/b.pdf", "SHARED")
    _insert_doc(store, "/d/c.pdf", "UNIQUE")

    groups = store.find_exact_duplicate_groups()
    assert len(groups) == 1
    g = groups[0]
    assert g["hash"] == "SHARED"
    assert g["count"] == 2
    ids = {d["id"] for d in g["documents"]}
    assert ids == {a, b}
    # Each member exposes path/filename for the UI
    assert all("path" in d and "filename" in d for d in g["documents"])


def test_find_exact_duplicate_groups_empty_when_no_dupes(store):
    _insert_doc(store, "/d/a.pdf", "H1")
    _insert_doc(store, "/d/b.pdf", "H2")
    assert store.find_exact_duplicate_groups() == []


def test_find_content_duplicate_groups(store):
    a = _insert_doc(store, "/d/a.pdf", "BYTES-A", content_hash="CONTENT")
    b = _insert_doc(store, "/d/b.pdf", "BYTES-B", content_hash="CONTENT")
    _insert_doc(store, "/d/c.pdf", "BYTES-C", content_hash="OTHER")

    groups = store.find_content_duplicate_groups()
    assert len(groups) == 1
    g = groups[0]
    assert g["hash"] == "CONTENT"
    assert {d["id"] for d in g["documents"]} == {a, b}


def test_find_content_duplicate_groups_ignores_null_hash(store):
    _insert_doc(store, "/d/a.pdf", "S1", content_hash=None)
    _insert_doc(store, "/d/b.pdf", "S2", content_hash=None)
    # Two NULL content hashes must NOT be grouped together.
    assert store.find_content_duplicate_groups() == []


def test_find_content_duplicate_groups_excludes_exact_sha256_dupes(store):
    # If two docs are already byte-identical (same sha256) we don't want them
    # surfaced a second time as a content-duplicate.
    _insert_doc(store, "/d/a.pdf", "SAME", content_hash="C")
    _insert_doc(store, "/d/b.pdf", "SAME", content_hash="C")
    # Same content_hash but ALSO same sha256 -> belongs to the exact group only.
    assert store.find_content_duplicate_groups() == []


def test_delete_documents_removes_index_rows(store):
    a = _insert_doc(store, "/d/a.pdf", "H")
    _insert_block(store, a, "some text")
    store.conn.execute(
        "INSERT INTO content_fts(document_id, block_id, path, filename, extension, "
        "block_type, block_number, text) VALUES(?,?,?,?,?,?,?,?)",
        (a, 1, "/d/a.pdf", "a.pdf", ".pdf", "paragraph", "1", "some text"),
    )
    store.conn.commit()

    n = store.delete_documents([a])
    assert n == 1
    assert store.conn.execute("SELECT COUNT(*) FROM documents WHERE id=?", (a,)).fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM content_blocks WHERE document_id=?", (a,)).fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM content_fts WHERE document_id=?", (a,)).fetchone()[0] == 0


def test_delete_documents_empty_list_is_noop(store):
    assert store.delete_documents([]) == 0
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_duplicate_detection.py -v
```

Expected: AttributeError — the three methods don't exist.

- [ ] **Step 3: Implement the three methods**

In `document_search/index/sqlite_store.py`, add these methods (place them after `remove_missing`, at the end of the class, after line 465):

```python
    def _group_documents_by(self, column: str, extra_where: str = "") -> list[dict]:
        """Return duplicate groups for the given documents column.

        `column` is a trusted literal (only ever 'sha256' or 'content_hash' from
        this module — never user input). Groups have COUNT(*) > 1.
        """
        assert column in ("sha256", "content_hash")  # guard against injection
        where = f"WHERE {column} IS NOT NULL"
        if extra_where:
            where += f" AND {extra_where}"
        hash_rows = self.conn.execute(
            f"""
            SELECT {column} AS h, COUNT(*) AS cnt
            FROM documents
            {where}
            GROUP BY {column}
            HAVING COUNT(*) > 1
            ORDER BY cnt DESC, {column}
            """
        ).fetchall()
        groups: list[dict] = []
        for hr in hash_rows:
            members = self.conn.execute(
                f"""
                SELECT id, path, filename, extension, file_size, modified_at, indexed_at,
                       sha256, content_hash
                FROM documents
                WHERE {column} = ?
                ORDER BY indexed_at ASC, id ASC
                """,
                (hr["h"],),
            ).fetchall()
            groups.append({
                "hash": hr["h"],
                "count": hr["cnt"],
                "documents": [dict(m) for m in members],
            })
        return groups

    def find_exact_duplicate_groups(self) -> list[dict]:
        """Groups of documents that share an identical raw-bytes sha256."""
        return self._group_documents_by("sha256")

    def find_content_duplicate_groups(self) -> list[dict]:
        """Groups of documents that share a normalized content_hash but are NOT
        byte-identical (those are already covered by the exact-sha256 groups)."""
        # Exclude content_hash values whose every member shares one sha256 — those
        # are pure exact duplicates and surface in find_exact_duplicate_groups().
        groups = self._group_documents_by("content_hash")
        result = []
        for g in groups:
            distinct_sha = {d["sha256"] for d in g["documents"]}
            if len(distinct_sha) > 1:
                result.append(g)
        return result

    def delete_documents(self, document_ids: list[int]) -> int:
        """Delete documents and their index rows. Mirrors remove_missing's
        deletion order. FK cascades drop document_acl/document_tags/marks.
        Returns the number of documents deleted."""
        deleted = 0
        for doc_id in document_ids:
            row = self.conn.execute(
                "SELECT id FROM documents WHERE id=?", (doc_id,)
            ).fetchone()
            if not row:
                continue
            self.conn.execute("DELETE FROM content_blocks WHERE document_id = ?", (doc_id,))
            self.conn.execute("DELETE FROM content_fts WHERE document_id = ?", (doc_id,))
            self.conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
            deleted += 1
        self.conn.commit()
        return deleted
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_duplicate_detection.py -v
```

Expected: all duplicate-detection tests pass.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: green.

- [ ] **Step 6: Commit**

```powershell
git add document_search/index/sqlite_store.py tests/test_duplicate_detection.py
git commit -m "feat(dedup): duplicate-group queries and delete_documents helper"
```

---

## Task 5: `GET /api/documents/duplicates` (admin, ACL-aware)

**Files:**
- Modify: `document_search/app.py`
- Test: `tests/test_app_duplicates.py` (new)

The response shape:

```json
{
  "exact":   [{"hash": "...", "count": 2, "documents": [{"id":1,"path":"...","filename":"...","file_size":100,"modified_at":"...","indexed_at":"..."}]}],
  "content": [{"hash": "...", "count": 2, "documents": [...]}]
}
```

Each group's `documents` are filtered to those the calling admin can read (via `visible_document_ids_subquery`); a group that collapses to ≤1 visible member is dropped.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_app_duplicates.py`:

```python
import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from document_search.app import create_app


def _login(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _insert_doc(db, path, sha, content_hash=None):
    from datetime import UTC, datetime
    from pathlib import Path
    now = datetime.now(tz=UTC).isoformat()
    cur = db.conn.execute(
        "INSERT INTO documents(path, filename, extension, file_size, modified_at, "
        "sha256, content_hash, indexed_at, status) VALUES(?,?,?,?,?,?,?,?,?)",
        (path, Path(path).name, Path(path).suffix, 100, now, sha, content_hash, now, "ok"),
    )
    db.conn.commit()
    return cur.lastrowid


def test_duplicates_requires_admin(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        r = client.get("/api/documents/duplicates")
        assert r.status_code == 401


def test_duplicates_returns_exact_and_content_groups(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        from document_search.index.sqlite_store import SqliteStore
        db = SqliteStore(tmp_path / "t.db")
        # Exact duplicates: same sha256
        a = _insert_doc(db, "/d/a.pdf", "SHA-X", content_hash="CH-A")
        b = _insert_doc(db, "/d/b.pdf", "SHA-X", content_hash="CH-A")
        # Content duplicates: different sha256, same content_hash
        c = _insert_doc(db, "/d/c.pdf", "SHA-Y", content_hash="CH-Z")
        d = _insert_doc(db, "/d/d.pdf", "SHA-W", content_hash="CH-Z")
        # Re-open the app's per-thread store sees these (same DB file) — grant public ACL
        db2 = SqliteStore(tmp_path / "t.db")  # triggers backfill -> public read on all

        r = client.get("/api/documents/duplicates", headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        body = r.json()
        exact_hashes = {g["hash"] for g in body["exact"]}
        content_hashes = {g["hash"] for g in body["content"]}
        assert "SHA-X" in exact_hashes
        assert "CH-Z" in content_hashes
        # The exact pair must NOT also appear as a content group
        assert "CH-A" not in content_hashes


def test_duplicates_empty_when_none(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        r = client.get("/api/documents/duplicates", headers={"X-Auth-Token": token})
        assert r.status_code == 200
        body = r.json()
        assert body == {"exact": [], "content": []}
```

> The fixture grants public read by re-opening the store (`_backfill_acl` runs at startup and on every new `SqliteStore`), mirroring how `tests/test_search_acl.py` triggers backfill.

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_duplicates.py -v
```

Expected: 404 / route-not-found for the duplicates endpoint.

- [ ] **Step 3: Add the endpoint**

In `document_search/app.py`, add this route **immediately after** `api_index_cleanup` (after line 645). It reuses `store()`, `require_admin`, and the ACL subquery:

```python
    @app.get("/api/documents/duplicates")
    def api_documents_duplicates(x_auth_token: str | None = Header(default=None)):
        admin_id = require_admin(x_auth_token)
        db = store()
        from document_search.services.acl_service import visible_document_ids_subquery

        acl_sql, acl_params = visible_document_ids_subquery(admin_id)
        visible_ids = {
            r["document_id"]
            for r in db.conn.execute(f"SELECT document_id FROM ({acl_sql})", acl_params).fetchall()
        }

        def _filter_groups(groups: list[dict]) -> list[dict]:
            out = []
            for g in groups:
                members = [d for d in g["documents"] if d["id"] in visible_ids]
                if len(members) > 1:
                    out.append({"hash": g["hash"], "count": len(members), "documents": members})
            return out

        return {
            "exact": _filter_groups(db.find_exact_duplicate_groups()),
            "content": _filter_groups(db.find_content_duplicate_groups()),
        }
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_duplicates.py -v
```

Expected: 3 passing.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: green.

- [ ] **Step 6: Commit**

```powershell
git add document_search/app.py tests/test_app_duplicates.py
git commit -m "feat(dedup): GET /api/documents/duplicates admin endpoint (ACL-aware)"
```

---

## Task 6: `POST /api/documents/duplicates/remove` action

**Files:**
- Modify: `document_search/app.py` (add a Pydantic model + the route)
- Test: `tests/test_app_duplicates.py` (extend)

Request body: `{"keep_id": 1, "remove_ids": [2, 3]}`. The server:
1. Loads `keep_id`'s document; 404 if missing or not visible to the admin.
2. Derives the keep document's group members: any doc sharing its `sha256` OR (if `content_hash` is non-NULL) its `content_hash`.
3. Confirms every `remove_id` is in that member set AND visible AND `!= keep_id`.
4. Deletes the validated `remove_ids`.

Response: `{"removed": N, "kept": keep_id}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_app_duplicates.py`:

```python
def test_remove_deletes_only_group_members(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        from document_search.index.sqlite_store import SqliteStore
        db = SqliteStore(tmp_path / "t.db")
        a = _insert_doc(db, "/d/a.pdf", "SHA-X", content_hash="CH")
        b = _insert_doc(db, "/d/b.pdf", "SHA-X", content_hash="CH")
        outsider = _insert_doc(db, "/d/z.pdf", "SHA-OTHER", content_hash="CH-OTHER")
        SqliteStore(tmp_path / "t.db")  # backfill -> public ACL

        r = client.post(
            "/api/documents/duplicates/remove",
            headers={"X-Auth-Token": token},
            json={"keep_id": a, "remove_ids": [b]},
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"removed": 1, "kept": a}

        db2 = SqliteStore(tmp_path / "t.db")
        assert db2.get_document_by_id(a) is not None       # kept
        assert db2.get_document_by_id(b) is None            # removed
        assert db2.get_document_by_id(outsider) is not None # untouched


def test_remove_rejects_non_group_member(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        from document_search.index.sqlite_store import SqliteStore
        db = SqliteStore(tmp_path / "t.db")
        a = _insert_doc(db, "/d/a.pdf", "SHA-X", content_hash="CH-A")
        unrelated = _insert_doc(db, "/d/u.pdf", "SHA-Y", content_hash="CH-B")
        SqliteStore(tmp_path / "t.db")

        r = client.post(
            "/api/documents/duplicates/remove",
            headers={"X-Auth-Token": token},
            json={"keep_id": a, "remove_ids": [unrelated]},
        )
        assert r.status_code == 400, r.text
        # Nothing deleted
        db2 = SqliteStore(tmp_path / "t.db")
        assert db2.get_document_by_id(unrelated) is not None


def test_remove_unknown_keep_id_404(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        r = client.post(
            "/api/documents/duplicates/remove",
            headers={"X-Auth-Token": token},
            json={"keep_id": 999999, "remove_ids": [1]},
        )
        assert r.status_code == 404


def test_remove_requires_admin(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        r = client.post(
            "/api/documents/duplicates/remove",
            json={"keep_id": 1, "remove_ids": [2]},
        )
        assert r.status_code == 401
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_duplicates.py -v
```

Expected: the four new tests fail (route missing).

- [ ] **Step 3: Add the request model and route**

In `document_search/app.py`, locate where the other Pydantic request models are declared (e.g. `MarkRequest`, `TagsRequest` — search for `class MarkRequest`). Add alongside them:

```python
class RemoveDuplicatesRequest(BaseModel):
    keep_id: int
    remove_ids: list[int]
```

Then add the route directly after `api_documents_duplicates` (the endpoint from Task 5):

```python
    @app.post("/api/documents/duplicates/remove")
    def api_documents_duplicates_remove(
        req: RemoveDuplicatesRequest,
        x_auth_token: str | None = Header(default=None),
    ):
        admin_id = require_admin(x_auth_token)
        db = store()
        from document_search.services.acl_service import visible_document_ids_subquery

        acl_sql, acl_params = visible_document_ids_subquery(admin_id)
        visible_ids = {
            r["document_id"]
            for r in db.conn.execute(f"SELECT document_id FROM ({acl_sql})", acl_params).fetchall()
        }

        keep = db.get_document_by_id(req.keep_id)
        if not keep or req.keep_id not in visible_ids:
            raise HTTPException(status_code=404, detail="Keep document not found")

        # Derive the keep document's duplicate group: same sha256, or same
        # (non-NULL) content_hash.
        member_rows = db.conn.execute(
            """
            SELECT id FROM documents
            WHERE sha256 = ?
               OR (content_hash IS NOT NULL AND content_hash = ?)
            """,
            (keep["sha256"], keep["content_hash"]),
        ).fetchall()
        group_ids = {r["id"] for r in member_rows}

        to_remove = []
        for rid in req.remove_ids:
            if rid == req.keep_id:
                raise HTTPException(status_code=400, detail="keep_id cannot be in remove_ids")
            if rid not in group_ids:
                raise HTTPException(
                    status_code=400,
                    detail=f"Document {rid} is not in the same duplicate group as {req.keep_id}",
                )
            if rid not in visible_ids:
                raise HTTPException(status_code=403, detail=f"Document {rid} is not visible")
            to_remove.append(rid)

        removed = db.delete_documents(to_remove)
        return {"removed": removed, "kept": req.keep_id}
```

> `BaseModel` is already imported in `app.py` (used by the other request models). If a linter flags it, confirm the existing import near the top.

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_duplicates.py -v
```

Expected: 7 passing.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: green.

- [ ] **Step 6: Commit**

```powershell
git add document_search/app.py tests/test_app_duplicates.py
git commit -m "feat(dedup): POST /api/documents/duplicates/remove action (validated, ACL-aware)"
```

---

## Task 7: Frontend — Duplicates panel in Ingest → Maintenance

**Files:**
- Modify: `document_search/web/templates/ingest.html`
- Modify: `document_search/web/static/app.js`

There is no JS test harness in this repo, so this task is verified by manual smoke test (Task 8). Keep the markup consistent with the existing Maintenance card (`.card`, `.card-head`, `.btn`, `.feedback`, toast helpers).

- [ ] **Step 1: Add the Duplicates card to `ingest.html`**

In `document_search/web/templates/ingest.html`, **after** the closing `</div>` of the "Index Maintenance" card (the card that ends at line 313, just before the `<!-- AI: Folder Structure Suggestions ... -->` comment at line 315), insert a new admin-only card:

```html
        <!-- Duplicate Documents (admin only) -->
        <div class="card admin-only hidden" id="duplicatesCard">
          <div class="card-head">
            <div class="card-ico">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
            </div>
            <div class="card-titles">
              <h2>Duplicate Documents</h2>
              <p>Find identical (same bytes) and likely-same (same content) documents, keep one, remove the rest</p>
            </div>
          </div>
          <div class="card-body">
            <div class="btn-row" style="margin-bottom:.75rem;">
              <button id="scanDuplicatesBtn" class="btn btn-p" onclick="loadDuplicates()">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
                Scan for duplicates
              </button>
            </div>
            <p id="duplicatesSummary" class="feedback"></p>
            <div id="duplicatesList" class="gap-top"></div>
          </div>
        </div>
```

- [ ] **Step 2: Add the JS handlers to `app.js`**

In `document_search/web/static/app.js`, add these functions directly after `runIndexCleanup` (after line 1887):

```javascript
// ── Duplicate documents ────────────────────────────────────────────
async function loadDuplicates() {
  const btn = document.getElementById('scanDuplicatesBtn');
  const listEl = document.getElementById('duplicatesList');
  if (btn) btn.classList.add('loading');
  setText('duplicatesSummary', 'Scanning…', 'info');
  if (listEl) listEl.innerHTML = '';
  try {
    const data = await api('/api/documents/duplicates');
    const exact = data.exact || [];
    const content = data.content || [];
    const total = exact.length + content.length;
    if (total === 0) {
      setText('duplicatesSummary', 'No duplicates found — your index is clean.', 'ok');
      return;
    }
    setText('duplicatesSummary',
      `${exact.length} exact group${exact.length !== 1 ? 's' : ''} · ${content.length} near-duplicate group${content.length !== 1 ? 's' : ''}`,
      'info');
    if (listEl) {
      listEl.innerHTML =
        renderDuplicateGroups(exact, 'exact', 'Identical files (same bytes)') +
        renderDuplicateGroups(content, 'content', 'Same content, different bytes');
    }
  } catch (e) {
    setText('duplicatesSummary', e.message, 'err');
    showToast(e.message, 'err');
  } finally {
    if (btn) btn.classList.remove('loading');
  }
}

function renderDuplicateGroups(groups, kind, heading) {
  if (!groups.length) return '';
  const sections = groups.map((g, gi) => {
    const groupKey = `${kind}-${gi}`;
    const rows = g.documents.map((d, di) => `
      <label class="dup-row" style="display:flex;align-items:center;gap:.5rem;padding:.25rem 0;">
        <input type="radio" name="keep-${groupKey}" value="${d.id}" ${di === 0 ? 'checked' : ''} />
        <code style="font-size:.8rem;flex:1;overflow:hidden;text-overflow:ellipsis;">${escHtml(d.path)}</code>
        <span class="muted" style="font-size:.75rem;white-space:nowrap;">${formatBytes(d.file_size)} · #${d.id}</span>
      </label>`).join('');
    return `
      <div class="dup-group" data-group-key="${groupKey}" style="border:1px solid var(--border,#2a2a2a);border-radius:8px;padding:.75rem;margin-bottom:.5rem;">
        <div class="muted" style="font-size:.78rem;margin-bottom:.4rem;">${g.count} copies · pick one to keep</div>
        ${rows}
        <div class="btn-row" style="margin-top:.5rem;">
          <button class="btn btn-g" onclick="removeDuplicates('${groupKey}')">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/></svg>
            Keep selected, remove others
          </button>
        </div>
      </div>`;
  }).join('');
  return `<div class="dup-section gap-top"><h3 style="font-size:.9rem;margin:.5rem 0;">${escHtml(heading)}</h3>${sections}</div>`;
}

async function removeDuplicates(groupKey) {
  const group = document.querySelector(`.dup-group[data-group-key="${groupKey}"]`);
  if (!group) return;
  const keepInput = group.querySelector(`input[name="keep-${groupKey}"]:checked`);
  if (!keepInput) { showToast('Select a document to keep', 'err'); return; }
  const keepId = Number(keepInput.value);
  const removeIds = Array.from(group.querySelectorAll(`input[name="keep-${groupKey}"]`))
    .map(i => Number(i.value))
    .filter(id => id !== keepId);
  if (!removeIds.length) { showToast('Nothing to remove', 'info'); return; }
  if (!confirm(`Remove ${removeIds.length} duplicate document${removeIds.length !== 1 ? 's' : ''} from the index? The kept document (#${keepId}) stays. Files on disk are not deleted.`)) return;
  try {
    const r = await api('/api/documents/duplicates/remove', 'POST', { keep_id: keepId, remove_ids: removeIds });
    showToast(`Removed ${r.removed} duplicate${r.removed !== 1 ? 's' : ''}`, 'ok');
    await loadDuplicates();  // refresh
  } catch (e) {
    showToast(e.message, 'err');
  }
}
```

- [ ] **Step 3: Confirm the card unhides for admins**

The card uses `class="card admin-only hidden"`. The existing admin gating (`app.js:861`) does `document.querySelectorAll('.admin-only').forEach(el => el.classList.remove('hidden'))` on login as admin — so no extra wiring is needed. Verify that line still runs on the ingest page (it's the shared post-login routine).

- [ ] **Step 4: Manual smoke (deferred to Task 8)**

No automated test. Proceed to commit; functional verification happens in Task 8.

- [ ] **Step 5: Commit**

```powershell
git add document_search/web/templates/ingest.html document_search/web/static/app.js
git commit -m "feat(dedup): Duplicates panel in Ingest maintenance with keep/remove UI"
```

---

## Task 8: Full verification + manual smoke test

**Files:** none (verification only)

- [ ] **Step 1: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: every test passes, including the new `tests/test_duplicate_detection.py` and `tests/test_app_duplicates.py`.

- [ ] **Step 2: End-to-end smoke via TestClient**

```powershell
$env:PYTHONPATH = "."; python -c "
from fastapi.testclient import TestClient
from document_search.app import create_app
from document_search.index.sqlite_store import SqliteStore
import tempfile, pathlib
tmp = pathlib.Path(tempfile.mkdtemp())
dbp = tmp / 'smoke.db'
app = create_app(str(dbp))
with TestClient(app) as c:
    tok = c.post('/api/login', json={'username':'admin','password':'admin'}).json()['token']
    db = SqliteStore(dbp)
    from datetime import UTC, datetime
    now = datetime.now(tz=UTC).isoformat()
    for path, sha, ch in [('/d/a.pdf','S1','C1'), ('/d/b.pdf','S1','C1'), ('/d/c.pdf','S2','CX'), ('/d/d.pdf','S3','CX')]:
        db.conn.execute('INSERT INTO documents(path,filename,extension,file_size,modified_at,sha256,content_hash,indexed_at,status) VALUES(?,?,?,?,?,?,?,?,?)', (path, path.split('/')[-1], '.pdf', 100, now, sha, ch, now, 'ok'))
    db.conn.commit()
    SqliteStore(dbp)  # backfill public ACL
    dup = c.get('/api/documents/duplicates', headers={'X-Auth-Token': tok}).json()
    print('exact groups:', [(g['hash'], g['count']) for g in dup['exact']])
    print('content groups:', [(g['hash'], g['count']) for g in dup['content']])
    keep = dup['exact'][0]['documents'][0]['id']
    others = [d['id'] for d in dup['exact'][0]['documents'] if d['id'] != keep]
    rem = c.post('/api/documents/duplicates/remove', headers={'X-Auth-Token': tok}, json={'keep_id': keep, 'remove_ids': others}).json()
    print('remove result:', rem)
    after = c.get('/api/documents/duplicates', headers={'X-Auth-Token': tok}).json()
    print('exact after remove:', [(g['hash'], g['count']) for g in after['exact']])
    assert rem['removed'] == len(others)
print('OK')
"
```

Expected: prints one exact group (`S1`, 2), one content group (`CX`, 2), a remove result, then an empty exact list after removal, then `OK`.

- [ ] **Step 3: Browser smoke (optional but recommended)**

```powershell
uvicorn document_search.app:app --port 8080
```

Log in as admin, open the Ingest page, scroll to "Duplicate Documents", click **Scan for duplicates**, pick a doc to keep in a group, click **Keep selected, remove others**, confirm the dialog, and verify a success toast plus the group disappearing on refresh.

- [ ] **Step 4: No final commit (verification only)**

---

## Definition of Done

- [ ] `$env:PYTHONPATH = "."; pytest -q` is green on a clean clone.
- [ ] `documents.content_hash` column exists (create-table + additive `ALTER TABLE` migration); index `idx_docs_content_hash` exists.
- [ ] `normalized_content_hash` is case/whitespace-insensitive, truncates at 8 KB, returns `None` for empty content, and is the single source of the content fingerprint (used by both `upsert_document` and `_backfill_content_hash`).
- [ ] `upsert_document` writes `content_hash`; `_backfill_content_hash` fills it idempotently at startup without overwriting existing values.
- [ ] `find_exact_duplicate_groups` groups by `sha256`; `find_content_duplicate_groups` groups by `content_hash` and excludes groups already covered by exact-sha256 duplicates; both ignore `NULL` and singletons.
- [ ] `delete_documents` mirrors `remove_missing`'s deletion order and returns the count.
- [ ] `GET /api/documents/duplicates` is admin-only, ACL-filters every group's members, and returns `{"exact":[...],"content":[...]}`.
- [ ] `POST /api/documents/duplicates/remove` validates that every `remove_id` is a real, visible member of `keep_id`'s group before deleting; rejects non-members (400), invisible docs (403), unknown keep (404), and non-admins (401).
- [ ] The Duplicates card appears in Ingest → Maintenance for admins, lists groups, lets the operator keep one and remove others with a confirm dialog and toast feedback.
- [ ] Manual smoke test (Step 2) passes.

---

## Notes for the executing agent

- **Why content_hash, not embeddings:** the brief requires an embedding-free near-duplicate heuristic so this plan stands alone. The normalized-content hash reuses already-extracted text and is exact-match on normalized content — it catches re-exports/re-encodes without any model. If the semantic-search plan lands, a *fuzzy* similarity tier can be added on top later; it is deliberately out of scope here.
- **Why the content group excludes exact-sha256 dupes:** a pair that is byte-identical already appears in the exact list. Surfacing it again as a "content duplicate" would double-count and confuse the operator. `find_content_duplicate_groups` drops any content group whose members all share a single `sha256`.
- **Why remove re-derives the group server-side:** never trust the client's notion of "the group". The client sends `keep_id` + `remove_ids`; the server independently computes `keep_id`'s group (same `sha256` OR same non-NULL `content_hash`) and refuses any `remove_id` outside it. This is the security boundary that prevents arbitrary document deletion.
- **ACL consistency:** the endpoint filters through `visible_document_ids_subquery(admin_id)` exactly like search. An admin still goes through the ACL (per the ACL-foundation plan's decision that admins are not a bypass). If a group has members the admin can't read, those members are hidden and the group is shown only if ≥2 visible members remain.
- **Files on disk are not deleted.** "Remove" only deletes index rows. The path remains on disk and will be re-indexed on the next crawl. Surfacing a per-file exclude list is a separate feature; the UI copy in the confirm dialog states this explicitly so operators aren't surprised.
- **Cascade coverage:** `delete_documents` relies on `ON DELETE CASCADE` for `document_acl`, `document_tags`, and `user_document_marks` (all defined in `_init_schema`). `PRAGMA foreign_keys=ON` is set in `_configure_connection`, so cascades fire. `content_blocks` and `content_fts` are deleted explicitly (FTS5 virtual tables have no FK cascade), mirroring `remove_missing`.
- **Backfill performance:** `_backfill_content_hash` only touches rows where `content_hash IS NULL`, so it's a no-op after the first run. For a large legacy corpus the first startup does one pass over text-bearing documents; this is comparable to a one-time reindex and is acceptable. If it ever becomes a startup bottleneck, move it behind an explicit admin "recompute hashes" action — but don't pre-optimize here.
- **Model constructor caveat (Task 3 Step 1):** the test constructs `ContentBlock`/`ExtractionResult`/`DocumentMetadata`/`FileFingerprint`. If your `document_search/models.py` uses different field names or required args, read that file and adjust the test kwargs — the assertion (content_hash equals the helper output) is what matters, not the exact constructor call.
