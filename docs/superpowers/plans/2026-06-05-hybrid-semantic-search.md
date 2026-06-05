# Hybrid Semantic Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional **semantic / hybrid** retrieval path to Seekr's currently pure-FTS5 keyword search (`document_search/index/search_service.py`). Embeddings come from Ollama, are stored per content-block in SQLite, and are fused with BM25 ranks via Reciprocal Rank Fusion (RRF). Keyword-only search stays the default and is the only mode active when `semantic_search_enabled=False`, so low-resource hosts and CI are never forced to embed.

**Architecture:** A new `embedding_service.py` wraps Ollama's `/api/embeddings` endpoint (reusing the exact `urllib.request` + timeout + error-handling pattern of `ai_organizer.py`) and provides a pure-Python cosine similarity helper. A new `block_embeddings` table stores one vector per `content_blocks` row as a packed `float32` BLOB. A new queue handler `embed_index` (built on the existing persistent job queue, kinds registered in `app.py`) backfills embeddings for blocks that don't have one yet. `search_service.search()` gains a `mode` parameter; in `semantic`/`hybrid` mode it embeds the query, ranks blocks by cosine similarity (over the ACL-visible set), and RRF-fuses that ranking with the existing BM25 ranking. Ranking improvements (recency boost + filename/title field weighting) are layered into the fused score. `/api/search` exposes `mode=keyword|semantic|hybrid`, defaulting to `keyword` and silently downgrading to `keyword` when the feature flag is off.

**Tech Stack:** Python 3.11, SQLite, the existing persistent job queue (`JobStore`/`Worker`), Ollama HTTP. Embeddings vectorstore is plain SQLite BLOBs queried with a pure-Python brute-force cosine scan (works everywhere, no native lib). The `sqlite-vec` (`vec0`) virtual table is documented as an **optional** acceleration that the code path probes for and gracefully falls back from — it is NOT a hard dependency and is NOT required for any test. No new mandatory third-party dependency.

---

## Assumptions & design decisions (NOT yet locked — stated explicitly)

These were open questions in the spec; this plan commits to the following and the executor should treat them as the contract:

- **(a) Embedding source.** Embeddings are produced by Ollama's `POST /api/embeddings` endpoint using a configurable embed model, default **`nomic-embed-text`** (768-dim, cheap, CPU-friendly). The model name is read from `DOCUMENT_SEARCH_OLLAMA_EMBED_MODEL`. **Alternative considered:** `sentence-transformers` (e.g. `all-MiniLM-L6-v2`) run in-process — rejected as the *default* because it adds a heavy mandatory dependency (torch) that contradicts the "lightweight, optional LLM" posture of the project, but the `EmbeddingService` interface is deliberately small (`embed(text) -> list[float]`) so a sentence-transformers backend can be dropped in later behind the same flag.
- **(b) Vector storage.** Vectors are stored in a new SQLite table `block_embeddings` as a packed little-endian `float32` BLOB plus a `dim` column. Similarity is computed by a **pure-Python brute-force cosine scan** over the ACL-visible candidate set. The `sqlite-vec` extension (`vec0` virtual table) is supported as an *optional* accelerator: `embedding_service.try_load_sqlite_vec(conn)` attempts `conn.load_extension("vec0")` and returns a bool; when it fails (the common case, and always in CI) the code uses the Python fallback. **All tests use the Python fallback and a stubbed embedding function — they never load the native lib and never call Ollama.**
- **(c) Hybrid ranking.** Hybrid = **Reciprocal Rank Fusion** of the BM25 result list and the vector-similarity result list: `score(doc) = Σ_lists 1 / (k + rank_in_list)` with `k = 60` (the standard RRF constant). RRF is chosen over a raw weighted blend of BM25 distance and cosine because the two scores live on incomparable scales (BM25 is an unbounded negative log-prob; cosine is in `[-1, 1]`); rank fusion needs no per-corpus normalisation and is robust. A tunable per-list weight (`bm25_weight`, `vector_weight`, both default `1.0`) multiplies each list's RRF contribution so operators can lean keyword- or vector-heavy.
- **(d) Keyword-only remains first-class.** A config flag `semantic_search_enabled` (default **`False`**) gates the entire feature. When off: the embed backfill handler is a no-op, `mode=semantic|hybrid` requests are downgraded to `keyword`, and `search()` behaves exactly as today. This guarantees zero behavioural/performance change for hosts that don't opt in, and keeps the full test-suite green without Ollama.

---

## File Structure

**Create:**
- `document_search/services/embedding_service.py` — Ollama `/api/embeddings` client + cosine similarity + sqlite-vec probe + pure-Python fallback ranker.
- `tests/test_embedding_service.py` — unit tests (stubbed HTTP, pure-Python cosine, packing round-trip).
- `tests/test_embedding_store.py` — `block_embeddings` schema + store CRUD.
- `tests/test_hybrid_search.py` — RRF fusion + ACL-preserving hybrid `search()` (stubbed embeddings).
- `tests/test_app_search_modes.py` — `/api/search` `mode` wiring + flag downgrade.

**Modify:**
- `document_search/index/sqlite_store.py` — add `block_embeddings` table to `_init_schema`; add `upsert_block_embedding`, `get_blocks_without_embedding`, `iter_block_embeddings` helpers; store embeddings inside `upsert_document` when enabled.
- `document_search/config.py` — add `semantic_search_enabled`, `embed_model`, `bm25_weight`, `vector_weight` to `AppConfig`.
- `document_search/index/search_service.py` — add `mode`, `embed_fn`, `bm25_weight`, `vector_weight` params; `_rrf_fuse`, `_recency_boost`, `_field_boost`, `_vector_candidates` helpers; hybrid branch.
- `document_search/app.py` — register `embed_index` handler; instantiate `EmbeddingService`; add `mode` to `SearchRequest`; wire `/api/search`; add `POST /api/index/embeddings/start` to enqueue a backfill.

**Untouched:**
- The BM25 keyword path and its existing tests — must remain byte-for-byte behaviourally identical when `mode="keyword"`.
- `ai_organizer.py` — read-only reference for the HTTP pattern.

---

## Key design decisions (locked for this plan)

- **One vector per content block**, not per document. Blocks are the existing FTS granularity; per-block embeddings give snippet-level recall and let document scores aggregate via `MAX` over block similarities.
- **`EmbeddingService.embed` is injectable.** `search()` and the queue handler accept an `embed_fn: Callable[[str], list[float]] | None`. Tests pass a deterministic stub; production passes `EmbeddingService(...).embed`. This is the hinge that keeps tests Ollama-free.
- **ACL first, vectors second.** The vector scan only ever runs over `document_id`s returned by `acl_service.visible_document_ids_subquery(user_id)` (or all docs when `bypass_acl=True`). ACL is never weakened by the semantic path.
- **Pagination preserved.** Hybrid fusion produces a ranked list of `document_id`s; `search()` returns flat block-rows in that document order (same row shape as today: `rank, document_id, filename, path, extension, modified_at, indexed_at, block_type, block_number, snippet`), truncated to `limit`. `api_search`'s existing group-by-document logic is unchanged.
- **Embeddings are best-effort.** A block with no stored embedding simply doesn't participate in the vector list (it can still surface via BM25 in hybrid mode). Missing-embedding never raises.
- **Dimension safety.** Each stored vector records its `dim`; the ranker skips any candidate whose `dim` disagrees with the query vector (e.g. after an embed-model change), rather than crashing. A model change therefore degrades gracefully until a re-backfill runs.

---

## Task 1: Config flags

**Files:**
- Modify: `document_search/config.py`
- Test: `tests/test_embedding_service.py` (new, first test only)

- [ ] **Step 1: Write the failing test**

Create `tests/test_embedding_service.py`:

```python
from document_search.config import AppConfig, load_config


def test_appconfig_has_semantic_defaults():
    cfg = AppConfig()
    assert cfg.semantic_search_enabled is False
    assert cfg.embed_model == "nomic-embed-text"
    assert cfg.bm25_weight == 1.0
    assert cfg.vector_weight == 1.0


def test_load_config_reads_semantic_flags(tmp_path):
    import json
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "semantic_search_enabled": True,
        "embed_model": "mxbai-embed-large",
        "bm25_weight": 2.0,
        "vector_weight": 0.5,
    }), encoding="utf-8")
    cfg = load_config(p)
    assert cfg.semantic_search_enabled is True
    assert cfg.embed_model == "mxbai-embed-large"
    assert cfg.bm25_weight == 2.0
    assert cfg.vector_weight == 0.5
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_embedding_service.py -v
```

Expected: `AttributeError: 'AppConfig' object has no attribute 'semantic_search_enabled'`.

- [ ] **Step 3: Add fields to `AppConfig`**

In `document_search/config.py`, inside the `@dataclass(slots=True) class AppConfig`, add the four fields after `follow_symlinks: bool = False` and before `ocr: OcrConfig = ...`:

```python
    follow_symlinks: bool = False
    semantic_search_enabled: bool = False
    embed_model: str = "nomic-embed-text"
    bm25_weight: float = 1.0
    vector_weight: float = 1.0
    ocr: OcrConfig = field(default_factory=OcrConfig)
```

`load_config` already copies any matching key via the `elif hasattr(cfg, key): setattr(...)` loop, so no loader change is needed.

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_embedding_service.py -v
```

Expected: 2 passing.

- [ ] **Step 5: Commit**

```powershell
git add document_search/config.py tests/test_embedding_service.py
git commit -m "feat(search): add semantic-search config flags to AppConfig"
```

---

## Task 2: `EmbeddingService` (Ollama client + cosine + fallback)

**Files:**
- Create: `document_search/services/embedding_service.py`
- Test: `tests/test_embedding_service.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_embedding_service.py`:

```python
import json
import struct
import urllib.error

import pytest

from document_search.services import embedding_service as es
from document_search.services.embedding_service import (
    EmbeddingService,
    cosine_similarity,
    pack_vector,
    unpack_vector,
    rank_by_cosine,
)


def test_pack_unpack_roundtrip():
    v = [0.1, -0.2, 0.3, 1.0]
    blob = pack_vector(v)
    assert isinstance(blob, (bytes, bytearray))
    out = unpack_vector(blob)
    assert len(out) == 4
    for a, b in zip(v, out):
        assert abs(a - b) < 1e-6


def test_cosine_similarity_basic():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_similarity_zero_vector_is_zero():
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_rank_by_cosine_orders_descending():
    query = [1.0, 0.0]
    # (document_id, vector)
    candidates = [
        (10, [0.0, 1.0]),    # orthogonal -> 0.0
        (20, [1.0, 0.0]),    # identical  -> 1.0
        (30, [0.7, 0.7]),    # ~0.707
    ]
    ranked = rank_by_cosine(query, candidates)
    assert [doc_id for doc_id, _ in ranked] == [20, 30, 10]
    # scores descending
    scores = [s for _, s in ranked]
    assert scores == sorted(scores, reverse=True)


def test_rank_by_cosine_skips_dim_mismatch():
    query = [1.0, 0.0, 0.0]
    candidates = [(1, [1.0, 0.0]), (2, [1.0, 0.0, 0.0])]  # first has wrong dim
    ranked = rank_by_cosine(query, candidates)
    assert [doc_id for doc_id, _ in ranked] == [2]


def test_embed_calls_ollama_and_parses(monkeypatch):
    captured = {}

    class _FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return json.dumps({"embedding": [0.5, 0.25, -0.1]}).encode()

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        captured["timeout"] = timeout
        return _FakeResp()

    monkeypatch.setattr(es.urllib.request, "urlopen", fake_urlopen)
    svc = EmbeddingService(base_url="http://ollama:11434", model="nomic-embed-text")
    vec = svc.embed("hello world")
    assert vec == [0.5, 0.25, -0.1]
    assert captured["url"].endswith("/api/embeddings")
    assert captured["body"]["model"] == "nomic-embed-text"
    assert captured["body"]["prompt"] == "hello world"


def test_embed_returns_none_when_ollama_unreachable(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(es.urllib.request, "urlopen", boom)
    svc = EmbeddingService()
    assert svc.embed("anything") is None


def test_try_load_sqlite_vec_returns_false_without_extension(tmp_path):
    import sqlite3
    conn = sqlite3.connect(tmp_path / "x.db")
    # No vec0 lib in CI -> must return False, never raise.
    assert es.try_load_sqlite_vec(conn) is False
```

- [ ] **Step 2: Run, expect ImportError**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_embedding_service.py -v
```

Expected: ImportError — `embedding_service` doesn't exist yet.

- [ ] **Step 3: Implement `embedding_service.py`**

Create `document_search/services/embedding_service.py`. The Ollama call mirrors `ai_organizer.py` exactly: `urllib.request.Request(..., method="POST")`, `json`-encoded body, `urlopen(req, timeout=...)`, and a broad `except` that logs and returns a safe sentinel.

```python
"""Embedding generation + vector similarity for semantic/hybrid search.

The Ollama call pattern (urllib.request.Request + urlopen + timeout + broad
except returning a sentinel) intentionally mirrors document_search.services.
ai_organizer.AiOrganizer so the two share the same operational behaviour.

Vectors are stored elsewhere as packed little-endian float32 BLOBs; this module
owns the (un)packing and the pure-Python cosine ranker used as the universal
fallback when the optional `sqlite-vec` extension is unavailable.
"""
from __future__ import annotations

import json
import logging
import math
import os
import struct
import urllib.error
import urllib.request
from typing import Callable, Iterable

logger = logging.getLogger(__name__)


def pack_vector(vec: list[float]) -> bytes:
    """Pack a float vector into a little-endian float32 BLOB."""
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack_vector(blob: bytes) -> list[float]:
    """Inverse of pack_vector."""
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity in [-1, 1]. Returns 0.0 for a zero-norm operand."""
    if len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def rank_by_cosine(
    query: list[float],
    candidates: Iterable[tuple[int, list[float]]],
) -> list[tuple[int, float]]:
    """Brute-force rank (document_id, vector) candidates by cosine vs query.

    Candidates whose vector dimension disagrees with the query are skipped
    (e.g. after an embed-model change) rather than raising. Returns a list of
    (document_id, score) sorted by score descending.
    """
    qdim = len(query)
    scored: list[tuple[int, float]] = []
    for doc_id, vec in candidates:
        if len(vec) != qdim:
            continue
        scored.append((doc_id, cosine_similarity(query, vec)))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored


def try_load_sqlite_vec(conn) -> bool:
    """Best-effort load of the optional `sqlite-vec` (vec0) extension.

    Returns True only if the extension loaded. Any failure (extension loading
    disabled, lib not installed, OS error) returns False so callers fall back
    to the pure-Python ranker. Never raises.
    """
    try:
        conn.enable_load_extension(True)
    except Exception:
        return False
    try:
        conn.load_extension("vec0")
        return True
    except Exception:
        return False
    finally:
        try:
            conn.enable_load_extension(False)
        except Exception:
            pass


class EmbeddingService:
    """Ollama-backed text embedder."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        timeout: int = 60,
    ) -> None:
        self.base_url = (
            base_url or os.getenv("DOCUMENT_SEARCH_OLLAMA_URL", "http://ollama:11434")
        ).rstrip("/")
        self.model = model or os.getenv(
            "DOCUMENT_SEARCH_OLLAMA_EMBED_MODEL", "nomic-embed-text"
        )
        self.timeout = timeout

    def embed(self, text: str) -> list[float] | None:
        """Return the embedding vector for `text`, or None on any failure.

        None (not an exception) is returned when Ollama is unreachable or the
        response is malformed, so callers can degrade to keyword-only search.
        """
        cleaned = (text or "").strip()
        if not cleaned:
            return None
        payload = json.dumps({"model": self.model, "prompt": cleaned[:8000]}).encode()
        try:
            req = urllib.request.Request(
                f"{self.base_url}/api/embeddings",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = json.loads(resp.read())
                vec = raw.get("embedding")
                if not isinstance(vec, list) or not vec:
                    logger.warning("Ollama embeddings response had no 'embedding' array")
                    return None
                return [float(x) for x in vec]
        except urllib.error.URLError as e:
            logger.debug("Ollama embeddings not reachable: %s", e)
            return None
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            logger.warning("Ollama embeddings parse error: %s", e)
            return None
        except Exception as e:  # noqa: BLE001 - defensive, mirrors AiOrganizer
            logger.error("Embedding error: %s", e)
            return None
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_embedding_service.py -v
```

Expected: 10 passing (2 from Task 1 + 8 new).

- [ ] **Step 5: Commit**

```powershell
git add document_search/services/embedding_service.py tests/test_embedding_service.py
git commit -m "feat(search): EmbeddingService with Ollama client, cosine, sqlite-vec probe"
```

---

## Task 3: `block_embeddings` schema + store helpers

**Files:**
- Modify: `document_search/index/sqlite_store.py`
- Test: `tests/test_embedding_store.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_embedding_store.py`:

```python
from datetime import UTC, datetime
from pathlib import Path

import pytest

from document_search.index.sqlite_store import SqliteStore
from document_search.services.embedding_service import pack_vector


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "test.db")


def _insert_doc_with_block(store, path="/d/a.txt", text="hello"):
    now = datetime.now(tz=UTC).isoformat()
    cur = store.conn.execute(
        "INSERT INTO documents(path, filename, extension, file_size, modified_at, "
        "sha256, indexed_at, status) VALUES(?,?,?,?,?,?,?,?)",
        (path, Path(path).name, ".txt", len(text), now, "h", now, "ok"),
    )
    doc_id = cur.lastrowid
    bcur = store.conn.execute(
        "INSERT INTO content_blocks(document_id, block_type, block_number, text, "
        "extractor, text_length) VALUES(?,?,?,?,?,?)",
        (doc_id, "paragraph", 1, text, "txt", len(text)),
    )
    store.conn.commit()
    return doc_id, bcur.lastrowid


def test_block_embeddings_table_exists(store):
    cols = {c[1] for c in store.conn.execute("PRAGMA table_info(block_embeddings)").fetchall()}
    assert {"block_id", "document_id", "dim", "vector", "model", "created_at"}.issubset(cols)


def test_upsert_and_iter_block_embedding(store):
    doc_id, block_id = _insert_doc_with_block(store)
    store.upsert_block_embedding(block_id, doc_id, [0.1, 0.2, 0.3], model="nomic-embed-text")
    rows = list(store.iter_block_embeddings([doc_id]))
    assert len(rows) == 1
    got_block_id, got_doc_id, vec = rows[0]
    assert got_block_id == block_id
    assert got_doc_id == doc_id
    assert len(vec) == 3
    assert abs(vec[0] - 0.1) < 1e-6


def test_upsert_block_embedding_is_idempotent(store):
    doc_id, block_id = _insert_doc_with_block(store)
    store.upsert_block_embedding(block_id, doc_id, [1.0, 2.0], model="m")
    store.upsert_block_embedding(block_id, doc_id, [3.0, 4.0], model="m")  # overwrite
    rows = list(store.iter_block_embeddings([doc_id]))
    assert len(rows) == 1
    assert rows[0][2][0] == pytest.approx(3.0)


def test_get_blocks_without_embedding(store):
    doc_id, block_id = _insert_doc_with_block(store, path="/d/a.txt", text="alpha")
    doc2, block2 = _insert_doc_with_block(store, path="/d/b.txt", text="beta")
    store.upsert_block_embedding(block_id, doc_id, [0.0, 1.0], model="m")
    missing = store.get_blocks_without_embedding(limit=10)
    missing_ids = {m["block_id"] for m in missing}
    assert block2 in missing_ids
    assert block_id not in missing_ids


def test_iter_block_embeddings_scoped_to_doc_ids(store):
    doc1, b1 = _insert_doc_with_block(store, path="/d/a.txt")
    doc2, b2 = _insert_doc_with_block(store, path="/d/b.txt")
    store.upsert_block_embedding(b1, doc1, [1.0, 0.0], model="m")
    store.upsert_block_embedding(b2, doc2, [0.0, 1.0], model="m")
    only_doc1 = list(store.iter_block_embeddings([doc1]))
    assert {r[1] for r in only_doc1} == {doc1}
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_embedding_store.py -v
```

Expected: `OperationalError: no such table: block_embeddings`.

- [ ] **Step 3: Add the table + helpers**

In `document_search/index/sqlite_store.py`, inside the `executescript("""...""")` of `_init_schema`, **append** just before the closing `"""` (after the `idx_jobs_owner` index line):

```sql
            CREATE TABLE IF NOT EXISTS block_embeddings (
              block_id INTEGER PRIMARY KEY,
              document_id INTEGER NOT NULL,
              dim INTEGER NOT NULL,
              vector BLOB NOT NULL,
              model TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY (block_id) REFERENCES content_blocks(id) ON DELETE CASCADE,
              FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_block_emb_doc ON block_embeddings(document_id);
```

Then add these methods to `SqliteStore` (place them directly after `move_document`, before `get_user_tags`):

```python
    def upsert_block_embedding(
        self, block_id: int, document_id: int, vector: list[float], model: str
    ) -> None:
        from document_search.services.embedding_service import pack_vector
        now = datetime.now(tz=UTC).isoformat()
        self.conn.execute(
            """
            INSERT INTO block_embeddings(block_id, document_id, dim, vector, model, created_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(block_id) DO UPDATE SET
              document_id=excluded.document_id, dim=excluded.dim,
              vector=excluded.vector, model=excluded.model, created_at=excluded.created_at
            """,
            (block_id, document_id, len(vector), pack_vector(vector), model, now),
        )
        self.conn.commit()

    def get_blocks_without_embedding(self, limit: int = 500) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT cb.id AS block_id, cb.document_id AS document_id, cb.text AS text
            FROM content_blocks cb
            LEFT JOIN block_embeddings be ON be.block_id = cb.id
            WHERE be.block_id IS NULL AND length(cb.text) > 0
            ORDER BY cb.id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def iter_block_embeddings(self, document_ids: list[int]):
        """Yield (block_id, document_id, vector) for the given documents.

        Decodes the packed BLOB lazily; intended for the in-memory cosine scan
        over an ACL-filtered candidate set.
        """
        from document_search.services.embedding_service import unpack_vector
        if not document_ids:
            return
        placeholders = ",".join("?" * len(document_ids))
        rows = self.conn.execute(
            f"SELECT block_id, document_id, vector FROM block_embeddings "
            f"WHERE document_id IN ({placeholders})",
            tuple(document_ids),
        ).fetchall()
        for r in rows:
            yield r["block_id"], r["document_id"], unpack_vector(r["vector"])
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_embedding_store.py -v
```

Expected: 5 passing.

- [ ] **Step 5: Full suite (no regressions)**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: prior baseline + new tests, zero failures. The new table is invisible to existing queries.

- [ ] **Step 6: Commit**

```powershell
git add document_search/index/sqlite_store.py tests/test_embedding_store.py
git commit -m "feat(search): block_embeddings table + store helpers"
```

---

## Task 4: RRF fusion + ranking-boost helpers

**Files:**
- Modify: `document_search/index/search_service.py` (add helpers only; no behaviour change yet)
- Test: `tests/test_hybrid_search.py` (new, fusion-math tests only)

- [ ] **Step 1: Write the failing fusion tests**

Create `tests/test_hybrid_search.py`:

```python
import pytest

from document_search.index.search_service import (
    _rrf_fuse,
    _recency_boost,
    _field_boost,
)


def test_rrf_fuse_simple_two_lists():
    # bm25 ranks doc 1 best; vector ranks doc 2 best.
    bm25 = [1, 2, 3]
    vector = [2, 3, 1]
    fused = _rrf_fuse({"bm25": (bm25, 1.0), "vector": (vector, 1.0)}, k=60)
    # fused is a list of (doc_id, score) sorted desc.
    assert [d for d, _ in fused][0] in (1, 2)
    # doc 3 appears in both at middling rank -> below the top pair
    assert fused[-1][0] == 3
    # every input doc is present exactly once
    assert sorted(d for d, _ in fused) == [1, 2, 3]


def test_rrf_score_math_exact():
    # Single list: doc at rank 1 (index 0) -> 1/(60+1).
    fused = _rrf_fuse({"only": ([7, 8], 1.0)}, k=60)
    scores = dict(fused)
    assert scores[7] == pytest.approx(1.0 / 61)
    assert scores[8] == pytest.approx(1.0 / 62)


def test_rrf_weights_bias_a_list():
    # Same two lists; heavily weight vector. doc 2 is vector's #1.
    fused = _rrf_fuse({"bm25": ([1, 2], 1.0), "vector": ([2, 1], 5.0)}, k=60)
    assert fused[0][0] == 2


def test_recency_boost_newer_scores_higher():
    older = _recency_boost("2020-01-01T00:00:00+00:00", now_iso="2026-01-01T00:00:00+00:00")
    newer = _recency_boost("2025-12-01T00:00:00+00:00", now_iso="2026-01-01T00:00:00+00:00")
    assert 0.0 <= older <= newer <= 1.0


def test_recency_boost_handles_bad_date():
    assert _recency_boost(None) == 0.0
    assert _recency_boost("not-a-date") == 0.0


def test_field_boost_matches_filename():
    # query term appears in the filename -> positive boost
    assert _field_boost("annual report", "annual-report-2025.pdf") > 0.0
    assert _field_boost("annual report", "invoice.pdf") == 0.0
```

- [ ] **Step 2: Run, expect ImportError**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_hybrid_search.py -v
```

Expected: ImportError — helpers don't exist yet.

- [ ] **Step 3: Add the helpers to `search_service.py`**

In `document_search/index/search_service.py`, add these module-level functions **after** `build_match_query` and **before** `_browse_all`:

```python
def _rrf_fuse(
    lists: dict[str, tuple[list[int], float]],
    k: int = 60,
) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion of several ranked document_id lists.

    `lists` maps a label -> (ranked_doc_ids, weight). Each document's fused
    score is the weighted sum over lists of 1/(k + rank), where rank is the
    1-based position in that list. Documents absent from a list contribute 0
    from it. Returns (document_id, score) sorted by score descending, ties
    broken by document_id for determinism.
    """
    scores: dict[int, float] = {}
    for ranked, weight in lists.values():
        for idx, doc_id in enumerate(ranked):
            rank = idx + 1
            scores[doc_id] = scores.get(doc_id, 0.0) + weight * (1.0 / (k + rank))
    return sorted(scores.items(), key=lambda t: (-t[1], t[0]))


def _recency_boost(modified_at: str | None, now_iso: str | None = None) -> float:
    """Map a document's modified_at into a [0, 1] recency score.

    1.0 == modified now, decaying with a ~365-day half-life. Unparseable or
    missing dates score 0.0 (no boost, never an error).
    """
    from datetime import UTC, datetime
    if not modified_at:
        return 0.0
    try:
        ts = datetime.fromisoformat(modified_at)
    except (ValueError, TypeError):
        return 0.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    now = datetime.fromisoformat(now_iso) if now_iso else datetime.now(tz=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
    # Exponential decay, half-life 365 days.
    return 0.5 ** (age_days / 365.0)


def _field_boost(query: str, filename: str) -> float:
    """Boost for query terms appearing in the filename/title.

    Returns the fraction of distinct query terms present in the filename, in
    [0, 1]. A filename match is a strong relevance signal that BM25 over body
    text under-weights.
    """
    import re
    terms = {t for t in re.findall(r"[a-z0-9]+", (query or "").lower()) if len(t) > 2}
    if not terms:
        return 0.0
    name = (filename or "").lower()
    hits = sum(1 for t in terms if t in name)
    return hits / len(terms)
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_hybrid_search.py -v
```

Expected: 6 passing.

- [ ] **Step 5: Commit**

```powershell
git add document_search/index/search_service.py tests/test_hybrid_search.py
git commit -m "feat(search): RRF fusion + recency/field ranking helpers"
```

---

## Task 5: Hybrid/semantic branch in `search()`

**Files:**
- Modify: `document_search/index/search_service.py`
- Test: `tests/test_hybrid_search.py` (extend)

The new `search()` signature adds (appended, keyword-only callers unaffected):

```python
mode: str = "keyword",                       # "keyword" | "semantic" | "hybrid"
embed_fn: Callable[[str], list[float] | None] | None = None,
bm25_weight: float = 1.0,
vector_weight: float = 1.0,
```

Semantics:
- `mode="keyword"` (default): unchanged — return the existing BM25/`_browse_all` rows verbatim.
- `mode="semantic"` or `"hybrid"`: require `embed_fn`; if it's `None` or returns `None` (Ollama down / empty query embedding), **fall back to keyword** — never raise, never return empty due to a missing embedder.
- Vector candidates are gathered ONLY over the ACL-visible document set (same `visible_document_ids_subquery`, or all docs when `bypass_acl`). The vector ranking is doc-level: each document's vector score is the **max** cosine over its blocks, plus `vector_weight`-independent ranking boosts (recency + filename field) applied to the *final fused* score.
- Fusion: `hybrid` RRF-fuses the BM25 doc-order and the vector doc-order; `semantic` uses the vector order alone (still RRF'd as a single weighted list so the boost layer is uniform).

- [ ] **Step 1: Write the failing integration tests**

Append to `tests/test_hybrid_search.py`:

```python
from datetime import UTC, datetime
from pathlib import Path

from document_search.index.sqlite_store import SqliteStore
from document_search.index.search_service import search


def _seed(tmp_path):
    """Two docs about 'soup'; one (cabbage) only an admin-visible doc, both public.
    We give cabbage a query-aligned embedding and tomato an orthogonal one so the
    vector ranker prefers cabbage even though both match BM25 'soup'."""
    store = SqliteStore(tmp_path / "test.db")
    alice = store.create_user("alice", "pw")
    now = datetime.now(tz=UTC).isoformat()
    docs = [
        ("/d/cabbage.txt", "h1", "cabbage soup recipe", [1.0, 0.0, 0.0]),
        ("/d/tomato.txt", "h2", "tomato soup recipe", [0.0, 1.0, 0.0]),
    ]
    block_of = {}
    for path, sha, body, _vec in docs:
        cur = store.conn.execute(
            "INSERT INTO documents(path, filename, extension, file_size, modified_at, "
            "sha256, indexed_at, status) VALUES(?,?,?,?,?,?,?,?)",
            (path, Path(path).name, ".txt", len(body), now, sha, now, "ok"),
        )
        doc_id = cur.lastrowid
        bcur = store.conn.execute(
            "INSERT INTO content_blocks(document_id, block_type, block_number, text, "
            "extractor, text_length) VALUES(?,?,?,?,?,?)",
            (doc_id, "paragraph", 1, body, "txt", len(body)),
        )
        store.conn.execute(
            "INSERT INTO content_fts(document_id, block_id, path, filename, extension, "
            "block_type, block_number, text) VALUES(?,?,?,?,?,?,?,?)",
            (doc_id, bcur.lastrowid, path, Path(path).name, ".txt", "paragraph", "1", body),
        )
        block_of[path] = (doc_id, bcur.lastrowid)
    store.conn.commit()
    store2 = SqliteStore(store.db_path)  # re-open -> backfill public ACL
    for (path, sha, body, vec) in docs:
        doc_id, block_id = block_of[path]
        store2.upsert_block_embedding(block_id, doc_id, vec, model="stub")
    return store2, alice


def test_keyword_mode_unchanged(tmp_path):
    store, alice = _seed(tmp_path)
    rows = search(store, "soup", user_id=alice, mode="keyword")
    paths = {r["path"] for r in rows}
    assert paths == {"/d/cabbage.txt", "/d/tomato.txt"}


def test_semantic_mode_prefers_vector_aligned_doc(tmp_path):
    store, alice = _seed(tmp_path)
    # Query embedding aligned with cabbage's vector [1,0,0].
    stub = lambda text: [1.0, 0.0, 0.0]
    rows = search(store, "soup", user_id=alice, mode="semantic", embed_fn=stub)
    ordered = [r["path"] for r in rows]
    assert ordered[0] == "/d/cabbage.txt"


def test_hybrid_mode_returns_both_and_respects_acl(tmp_path):
    store, alice = _seed(tmp_path)
    stub = lambda text: [1.0, 0.0, 0.0]
    rows = search(store, "soup", user_id=alice, mode="hybrid", embed_fn=stub)
    paths = {r["path"] for r in rows}
    assert paths == {"/d/cabbage.txt", "/d/tomato.txt"}


def test_semantic_falls_back_to_keyword_when_embed_unavailable(tmp_path):
    store, alice = _seed(tmp_path)
    # embed_fn returns None (Ollama down) -> keyword behaviour, no error.
    rows = search(store, "soup", user_id=alice, mode="semantic", embed_fn=lambda t: None)
    paths = {r["path"] for r in rows}
    assert paths == {"/d/cabbage.txt", "/d/tomato.txt"}


def test_hybrid_acl_filters_out_restricted_doc(tmp_path):
    store, alice = _seed(tmp_path)
    bob = store.create_user("bob", "pw")
    # Restrict cabbage.txt to alice only.
    now = datetime.now(tz=UTC).isoformat()
    public_id = store.conn.execute(
        "SELECT id FROM principals WHERE type='group' AND external_id='public'"
    ).fetchone()["id"]
    cab_id = store.conn.execute(
        "SELECT id FROM documents WHERE path='/d/cabbage.txt'"
    ).fetchone()["id"]
    alice_p = store.conn.execute(
        "SELECT principal_id FROM users WHERE username='alice'"
    ).fetchone()["principal_id"]
    store.conn.execute(
        "DELETE FROM document_acl WHERE document_id=? AND principal_id=?", (cab_id, public_id)
    )
    store.conn.execute(
        "INSERT INTO document_acl(document_id, principal_id, permission, granted_at) "
        "VALUES(?,?, 'read', ?)", (cab_id, alice_p, now),
    )
    store.conn.commit()
    stub = lambda text: [1.0, 0.0, 0.0]
    bob_rows = search(store, "soup", user_id=bob, mode="hybrid", embed_fn=stub)
    assert {r["path"] for r in bob_rows} == {"/d/tomato.txt"}
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_hybrid_search.py -v
```

Expected: the five new tests fail — `search()` rejects the `mode`/`embed_fn` kwargs.

- [ ] **Step 3: Implement the hybrid branch**

In `document_search/index/search_service.py`:

First add the import at the top, after the existing `from document_search.index.sqlite_store import SqliteStore`:

```python
from typing import Callable
```

Replace the `search` function signature and add the branch. The full new `search` is:

```python
def search(
    store: SqliteStore,
    query: str,
    limit: int = 20,
    filetype: str | None = None,
    path_filter: str | None = None,
    block_type: str | None = None,
    modified_from: str | None = None,
    modified_to: str | None = None,
    tags: list[str] | None = None,
    user_id: int | None = None,
    bypass_acl: bool = False,
    mode: str = "keyword",
    embed_fn: Callable[[str], list[float] | None] | None = None,
    bm25_weight: float = 1.0,
    vector_weight: float = 1.0,
):
    if user_id is None and not bypass_acl:
        raise ValueError("user_id is required unless bypass_acl=True is set explicitly")

    tags = [t.lower().strip() for t in (tags or [])]

    # Keyword path (unchanged behaviour).
    keyword_rows = _keyword_rows(
        store, query, limit, filetype, path_filter, block_type,
        modified_from, modified_to, tags, user_id, bypass_acl,
    )
    if mode == "keyword":
        return keyword_rows

    # Semantic/hybrid require an embedder; degrade to keyword if absent.
    if embed_fn is None:
        return keyword_rows
    query_vec = embed_fn(query)
    if not query_vec:
        return keyword_rows

    return _hybrid_rows(
        store, keyword_rows, query, query_vec, limit,
        filetype, path_filter, block_type, modified_from, modified_to,
        tags, user_id, bypass_acl, mode, bm25_weight, vector_weight,
    )
```

Now **extract** the existing BM25/browse body into a helper `_keyword_rows` (this is a pure refactor — copy the current `search` body verbatim minus the `user_id`-guard and the `tags` normalisation, which now live in `search`). Add it directly after the `search` function:

```python
def _keyword_rows(
    store: SqliteStore,
    query: str,
    limit: int,
    filetype: str | None,
    path_filter: str | None,
    block_type: str | None,
    modified_from: str | None,
    modified_to: str | None,
    tags: list[str],
    user_id: int | None,
    bypass_acl: bool,
):
    match_query = build_match_query(query, filetype, block_type)

    if match_query is None:
        return _browse_all(
            store, filetype, path_filter, block_type,
            modified_from, modified_to, tags, user_id, limit,
            bypass_acl=bypass_acl,
        )

    sql = """
        SELECT c.rank, d.id as document_id, d.filename, d.path, d.extension,
               d.modified_at, d.indexed_at,
               b.block_type, b.block_number,
               snippet(content_fts, 7, '[', ']', ' … ', 12) AS snippet
        FROM content_fts c
        JOIN documents d ON d.id = c.document_id
        JOIN content_blocks b ON b.id = c.block_id
        WHERE content_fts MATCH ?
    """
    params: list = [match_query]
    if path_filter:
        sql += " AND d.path LIKE ?"
        params.append(path_filter + "%")
    if modified_from:
        sql += " AND d.modified_at >= ?"
        params.append(modified_from)
    if modified_to:
        sql += " AND d.modified_at <= ?"
        params.append(modified_to)
    if tags and user_id is not None:
        tag_ph = ",".join("?" * len(tags))
        sql += f"""
            AND d.id IN (
                SELECT dt.document_id FROM document_tags dt
                JOIN user_tags ut ON ut.id = dt.tag_id
                WHERE dt.user_id = ? AND ut.name IN ({tag_ph})
                GROUP BY dt.document_id
                HAVING COUNT(DISTINCT ut.name) = ?
            )"""
        params.extend([user_id] + tags + [len(tags)])
    if not bypass_acl:
        from document_search.services.acl_service import visible_document_ids_subquery
        acl_sql, acl_params = visible_document_ids_subquery(user_id)
        sql += f" AND d.id IN ({acl_sql})"
        params.extend(acl_params)
    sql += " ORDER BY c.rank LIMIT ?"
    params.append(limit)
    return store.conn.execute(sql, tuple(params)).fetchall()
```

> Delete the now-duplicated body from the old `search` (the part from `match_query = build_match_query(...)` downward) — `search` now ends at the `return _hybrid_rows(...)` call shown above.

Add the hybrid assembler after `_keyword_rows`:

```python
def _visible_doc_ids(
    store: SqliteStore,
    user_id: int | None,
    bypass_acl: bool,
    path_filter: str | None,
    modified_from: str | None,
    modified_to: str | None,
) -> list[int]:
    """All document_ids the user may read, after the same scalar filters the
    keyword path applies (path/date), used as the vector candidate pool."""
    sql = "SELECT d.id FROM documents d WHERE 1=1"
    params: list = []
    if path_filter:
        sql += " AND d.path LIKE ?"
        params.append(path_filter + "%")
    if modified_from:
        sql += " AND d.modified_at >= ?"
        params.append(modified_from)
    if modified_to:
        sql += " AND d.modified_at <= ?"
        params.append(modified_to)
    if not bypass_acl:
        from document_search.services.acl_service import visible_document_ids_subquery
        acl_sql, acl_params = visible_document_ids_subquery(user_id)
        sql += f" AND d.id IN ({acl_sql})"
        params.extend(acl_params)
    return [r[0] for r in store.conn.execute(sql, tuple(params)).fetchall()]


def _vector_doc_order(
    store: SqliteStore,
    query_vec: list[float],
    candidate_doc_ids: list[int],
) -> list[int]:
    """Rank candidate documents by best (max) block cosine vs the query vector."""
    from document_search.services.embedding_service import cosine_similarity
    best: dict[int, float] = {}
    for _block_id, doc_id, vec in store.iter_block_embeddings(candidate_doc_ids):
        if len(vec) != len(query_vec):
            continue
        sim = cosine_similarity(query_vec, vec)
        if doc_id not in best or sim > best[doc_id]:
            best[doc_id] = sim
    return [doc_id for doc_id, _ in sorted(best.items(), key=lambda t: (-t[1], t[0]))]


def _hybrid_rows(
    store: SqliteStore,
    keyword_rows,
    query: str,
    query_vec: list[float],
    limit: int,
    filetype: str | None,
    path_filter: str | None,
    block_type: str | None,
    modified_from: str | None,
    modified_to: str | None,
    tags: list[str],
    user_id: int | None,
    bypass_acl: bool,
    mode: str,
    bm25_weight: float,
    vector_weight: float,
):
    # BM25 doc order (dedup, preserve rank order from keyword_rows).
    bm25_order: list[int] = []
    seen: set[int] = set()
    for r in keyword_rows:
        did = r["document_id"]
        if did not in seen:
            seen.add(did)
            bm25_order.append(did)

    candidates = _visible_doc_ids(
        store, user_id, bypass_acl, path_filter, modified_from, modified_to
    )
    vector_order = _vector_doc_order(store, query_vec, candidates)

    lists: dict[str, tuple[list[int], float]] = {"vector": (vector_order, vector_weight)}
    if mode == "hybrid":
        lists["bm25"] = (bm25_order, bm25_weight)
    fused = _rrf_fuse(lists)

    # Layer ranking boosts (recency + filename field) onto the fused score.
    meta = {}
    fused_ids = [d for d, _ in fused]
    if fused_ids:
        placeholders = ",".join("?" * len(fused_ids))
        for row in store.conn.execute(
            f"SELECT id, filename, modified_at FROM documents WHERE id IN ({placeholders})",
            tuple(fused_ids),
        ).fetchall():
            meta[row["id"]] = (row["filename"], row["modified_at"])

    boosted: list[tuple[int, float]] = []
    for doc_id, score in fused:
        filename, modified_at = meta.get(doc_id, ("", None))
        score += 0.1 * _recency_boost(modified_at)
        score += 0.2 * _field_boost(query, filename)
        boosted.append((doc_id, score))
    boosted.sort(key=lambda t: (-t[1], t[0]))
    ordered_ids = [d for d, _ in boosted][:limit]

    return _rows_for_doc_ids(store, ordered_ids, keyword_rows)


def _rows_for_doc_ids(store: SqliteStore, ordered_ids: list[int], keyword_rows):
    """Materialise output rows (same shape as the keyword path) in the fused
    document order. Reuses any BM25 snippet rows we already have; for docs that
    only surfaced via the vector list, emit a snippet-less representative block."""
    by_doc: dict[int, list] = {}
    for r in keyword_rows:
        by_doc.setdefault(r["document_id"], []).append(r)

    out = []
    for doc_id in ordered_ids:
        if doc_id in by_doc:
            out.extend(by_doc[doc_id])
            continue
        rep = store.conn.execute(
            """
            SELECT NULL AS rank, d.id AS document_id, d.filename, d.path, d.extension,
                   d.modified_at, d.indexed_at, cb.block_type, cb.block_number,
                   NULL AS snippet
            FROM documents d
            JOIN content_blocks cb ON cb.document_id = d.id
            WHERE d.id = ?
            ORDER BY cb.block_number LIMIT 1
            """,
            (doc_id,),
        ).fetchone()
        if rep is not None:
            out.append(rep)
    return out
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_hybrid_search.py -v
```

Expected: 11 passing (6 fusion + 5 integration).

- [ ] **Step 5: Full suite (keyword path must be byte-for-byte unchanged)**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: every prior test still green (the `search()` default `mode="keyword"` routes through `_keyword_rows`, which is a verbatim copy of the old body). If `tests/test_search_acl.py` or `tests/test_app_search.py` fail, the refactor changed behaviour — diff `_keyword_rows` against the original `search` body and fix.

- [ ] **Step 6: Commit**

```powershell
git add document_search/index/search_service.py tests/test_hybrid_search.py
git commit -m "feat(search): hybrid/semantic search branch with RRF + boosts behind embed_fn"
```

---

## Task 6: `embed_index` backfill queue handler + store-on-index

**Files:**
- Modify: `document_search/app.py`, `document_search/index/sqlite_store.py`
- Test: `tests/test_app_search_modes.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_app_search_modes.py`:

```python
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from document_search.app import create_app


def _login(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_embed_index_handler_registered(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    assert "embed_index" in app.state.worker._handlers


def test_embed_index_noop_when_flag_disabled(tmp_path, monkeypatch):
    """With semantic_search_enabled False (default), the backfill returns a
    skipped result and writes no embeddings — no Ollama call."""
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        r = client.post("/api/index/embeddings/start", headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            jr = client.get(f"/api/index/jobs/{job_id}", headers={"X-Auth-Token": token})
            if jr.json()["status"] in ("finished", "failed", "interrupted"):
                break
            time.sleep(0.05)
        # Job completes; nothing embedded.
        from document_search.index.sqlite_store import SqliteStore
        s = SqliteStore(tmp_path / "t.db")
        n = s.conn.execute("SELECT COUNT(*) FROM block_embeddings").fetchone()[0]
        assert n == 0


def test_search_mode_param_downgrades_when_flag_off(tmp_path):
    """mode=hybrid is accepted but behaves as keyword when the flag is off."""
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        r = client.post(
            "/api/search",
            headers={"X-Auth-Token": token},
            json={"query": "anything", "mode": "hybrid"},
        )
        assert r.status_code == 200, r.text
        assert isinstance(r.json(), list)
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_search_modes.py -v
```

Expected: `embed_index` not registered; `/api/index/embeddings/start` 404; `SearchRequest` rejects `mode`.

- [ ] **Step 3a: Store embeddings on index (gated, so default tests stay offline)**

In `document_search/index/sqlite_store.py`, the `upsert_document` method must optionally embed new blocks. Keep it injection-based so it never imports Ollama by default. Add a constructor-time embedder hook:

In `__init__`, after `self._init_schema()` add:

```python
        self.embed_fn = None  # optional Callable[[str], list[float] | None]; set by app when semantic enabled
```

Then, inside `upsert_document`, **after** the `fts_rows.append(...)` loop and **before** `if fts_rows:`, embed each block when an embedder is configured. Replace the block-insert loop so it captures `(block_id, text)`:

```python
        path_str = str(fp.path)
        name_str = fp.path.name
        ext_str = fp.path.suffix.lower()
        fts_rows = []
        new_blocks: list[tuple[int, str]] = []
        for block in ext.blocks:
            cursor = self.conn.execute(
                "INSERT INTO content_blocks(document_id, block_type, block_number, text, extractor, text_length, metadata_json) VALUES(?,?,?,?,?,?,?)",
                (doc_id, block.block_type, block.block_number, block.text, block.extractor, len(block.text), str(block.metadata)),
            )
            block_id = cursor.lastrowid
            fts_rows.append((doc_id, block_id, path_str, name_str, ext_str, block.block_type, str(block.block_number), block.text))
            new_blocks.append((block_id, block.text))
        if fts_rows:
            self.conn.executemany(
                "INSERT INTO content_fts(document_id, block_id, path, filename, extension, block_type, block_number, text) VALUES(?,?,?,?,?,?,?,?)",
                fts_rows,
            )
        if self.embed_fn is not None:
            for block_id, text in new_blocks:
                vec = self.embed_fn(text)
                if vec:
                    self.upsert_block_embedding(block_id, doc_id, vec, model=getattr(self, "embed_model", "unknown"))
        self.conn.commit()
        return doc_id
```

> The existing method already commits at the end; ensure exactly one final `self.conn.commit()` and `return doc_id`. Remove the original duplicated `if fts_rows:` block you replaced.

- [ ] **Step 3b: Register the `embed_index` handler + endpoint + search wiring in `app.py`**

In `document_search/app.py`, after the `organizer = AiOrganizer()` line (~267), add the embedding service:

```python
    from document_search.services.embedding_service import EmbeddingService
    embedder = EmbeddingService(model=load_effective_config().embed_model)
```

> `load_effective_config` is defined later in `create_app`; to avoid a forward-reference, instead read the model lazily inside the handler. Simpler: construct `EmbeddingService()` here with no model override and set `.model` per-call. Use:

```python
    from document_search.services.embedding_service import EmbeddingService
    embedder = EmbeddingService()
```

Register the handler alongside the other `@worker.handler(...)` blocks (after `_handle_ai_reorganize`):

```python
    @worker.handler("embed_index")
    def _handle_embed_index(payload: dict, progress_cb):
        from document_search.config import load_config
        cfg = load_config(config_path) if config_path.exists() else AppConfig()
        if not cfg.semantic_search_enabled:
            return {"skipped": True, "reason": "semantic_search_enabled is false", "embedded": 0}
        embedder.model = cfg.embed_model
        batch = int(payload.get("batch", 500))
        db = SqliteStore(Path(db_path))
        pending = db.get_blocks_without_embedding(limit=batch)
        total = len(pending)
        embedded = 0
        progress_cb({"total": total, "embedded": 0})
        for b in pending:
            vec = embedder.embed(b["text"])
            if vec:
                db.upsert_block_embedding(b["block_id"], b["document_id"], vec, model=cfg.embed_model)
                embedded += 1
            progress_cb({"total": total, "embedded": embedded})
        return {"skipped": False, "embedded": embedded, "total": total}
```

Add the start endpoint near the other `/api/index/*` routes (after `api_index_start`):

```python
    @app.post("/api/index/embeddings/start")
    def api_index_embeddings_start(x_auth_token: str | None = Header(default=None)):
        admin_id = require_admin(x_auth_token)
        job_id = job_store.enqueue(
            "embed_index", payload={"batch": 500}, owner_user_id=admin_id, max_retries=0
        )
        return {"job_id": str(job_id)}
```

Add `mode` to `SearchRequest` (in the model near line 110):

```python
class SearchRequest(BaseModel):
    query: str = ""
    tags: list[str] = Field(default_factory=list)
    limit: int = 20
    filetype: str | None = None
    path: str | None = None
    block_type: str | None = None
    modified_from: str | None = None
    modified_to: str | None = None
    mode: str = "keyword"
```

Wire `/api/search` (replace the `search(...)` call inside `api_search`, ~line 1584):

```python
        cfg = load_effective_config()
        mode = req.mode if cfg.semantic_search_enabled else "keyword"
        embed_fn = embedder.embed if (cfg.semantic_search_enabled and mode != "keyword") else None
        if cfg.semantic_search_enabled and mode != "keyword":
            embedder.model = cfg.embed_model
        try:
            rows = search(
                db, req.query, req.limit, req.filetype, req.path, req.block_type,
                req.modified_from, req.modified_to, req.tags, user_id,
                mode=mode, embed_fn=embed_fn,
                bm25_weight=cfg.bm25_weight, vector_weight=cfg.vector_weight,
            )
        except sqlite3.OperationalError as e:
            raise HTTPException(status_code=400, detail=f"Search query error: {e}")
```

> Keep the rest of `api_search` (the group-by-document block) exactly as-is.

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_search_modes.py -v
```

Expected: 3 passing.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: all green. The default `semantic_search_enabled=False` means no test ever reaches Ollama.

- [ ] **Step 6: Commit**

```powershell
git add document_search/app.py document_search/index/sqlite_store.py tests/test_app_search_modes.py
git commit -m "feat(search): embed_index backfill handler, /api/search mode wiring, store-on-index"
```

---

## Task 7: End-to-end semantic test with the flag ON (stubbed embedder)

**Files:**
- Test: `tests/test_app_search_modes.py` (extend)

This proves the *enabled* path works end-to-end without Ollama by monkeypatching `EmbeddingService.embed`.

- [ ] **Step 1: Write the test**

Append to `tests/test_app_search_modes.py`:

```python
def test_enabled_semantic_search_end_to_end(tmp_path, monkeypatch):
    import json
    from pathlib import Path
    from document_search.config import AppConfig
    import document_search.app as app_mod
    from document_search.services.embedding_service import EmbeddingService

    # Deterministic stub embedder: 'cabbage' -> [1,0,0], 'tomato' -> [0,1,0],
    # anything else (incl. the query) -> aligned with cabbage.
    def fake_embed(self, text):
        t = (text or "").lower()
        if "tomato" in t:
            return [0.0, 1.0, 0.0]
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr(EmbeddingService, "embed", fake_embed)

    # config.json with the flag ON
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"semantic_search_enabled": True, "embed_model": "stub"}), encoding="utf-8")
    monkeypatch.setenv("DOCUMENT_SEARCH_CONFIG_PATH", str(config_path))

    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        # Seed two docs directly + FTS
        from document_search.index.sqlite_store import SqliteStore
        from datetime import UTC, datetime
        s = SqliteStore(tmp_path / "t.db")
        now = datetime.now(tz=UTC).isoformat()
        for path, body in [("/d/cabbage.txt", "cabbage soup"), ("/d/tomato.txt", "tomato soup")]:
            cur = s.conn.execute(
                "INSERT INTO documents(path, filename, extension, file_size, modified_at, sha256, indexed_at, status) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (path, Path(path).name, ".txt", len(body), now, path, now, "ok"),
            )
            doc_id = cur.lastrowid
            bcur = s.conn.execute(
                "INSERT INTO content_blocks(document_id, block_type, block_number, text, extractor, text_length) "
                "VALUES(?,?,?,?,?,?)", (doc_id, "paragraph", 1, body, "txt", len(body)),
            )
            s.conn.execute(
                "INSERT INTO content_fts(document_id, block_id, path, filename, extension, block_type, block_number, text) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (doc_id, bcur.lastrowid, path, Path(path).name, ".txt", "paragraph", "1", body),
            )
        s.conn.commit()
        SqliteStore(tmp_path / "t.db")  # re-open -> backfill public ACL

        # Backfill embeddings via the queue
        jr = client.post("/api/index/embeddings/start", headers={"X-Auth-Token": token}).json()
        import time
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            st = client.get(f"/api/index/jobs/{jr['job_id']}", headers={"X-Auth-Token": token}).json()
            if st["status"] in ("finished", "failed"):
                break
            time.sleep(0.05)
        n = SqliteStore(tmp_path / "t.db").conn.execute("SELECT COUNT(*) FROM block_embeddings").fetchone()[0]
        assert n == 2

        # Semantic search: query -> cabbage-aligned vector -> cabbage first
        r = client.post("/api/search", headers={"X-Auth-Token": token},
                        json={"query": "cabbage", "mode": "semantic"})
        assert r.status_code == 200, r.text
        results = r.json()
        assert results and results[0]["path"] == "/d/cabbage.txt"
```

- [ ] **Step 2: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_search_modes.py::test_enabled_semantic_search_end_to_end -v
```

Expected: 1 passing. If the embed backfill count is 0, confirm `config.json` is being read (the `DOCUMENT_SEARCH_CONFIG_PATH` env var must point at the flag-on file before `create_app`).

- [ ] **Step 3: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: all green.

- [ ] **Step 4: Commit**

```powershell
git add tests/test_app_search_modes.py
git commit -m "test(search): end-to-end semantic search with stubbed embedder"
```

---

## Task 8: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Full suite, clean**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: zero failures, zero errors. No test contacts Ollama or loads a native extension.

- [ ] **Step 2: Confirm keyword default is untouched**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_search_acl.py tests/test_app_search.py -v
```

Expected: all pre-existing search tests pass unchanged — proof the refactor preserved the keyword path.

- [ ] **Step 3: Manual smoke (flag OFF — the shipping default)**

```powershell
$env:PYTHONPATH = "."; python -c "
from fastapi.testclient import TestClient
from document_search.app import create_app
import tempfile, pathlib
tmp = pathlib.Path(tempfile.mkdtemp())
app = create_app(str(tmp / 'smoke.db'))
with TestClient(app) as c:
    tok = c.post('/api/login', json={'username':'admin','password':'admin'}).json()['token']
    r = c.post('/api/search', headers={'X-Auth-Token': tok}, json={'query':'the','mode':'hybrid','limit':5})
    print('status', r.status_code, 'rows', len(r.json()))
    assert r.status_code == 200
print('OK')
"
```

Expected: `status 200 rows ...` then `OK`. `mode=hybrid` is silently downgraded to keyword because the flag is off.

- [ ] **Step 4: No commit (verification only)**

---

## Definition of Done

- [ ] `pytest -q` green; no test requires a live Ollama or the `sqlite-vec` native lib.
- [ ] `AppConfig` exposes `semantic_search_enabled` (default `False`), `embed_model` (default `nomic-embed-text`), `bm25_weight`, `vector_weight`.
- [ ] `EmbeddingService.embed` hits Ollama `/api/embeddings` and returns `None` (never raises) on any failure; `cosine_similarity`, `pack_vector`/`unpack_vector`, `rank_by_cosine`, and `try_load_sqlite_vec` are covered by unit tests.
- [ ] `block_embeddings` table exists; `upsert_block_embedding`/`get_blocks_without_embedding`/`iter_block_embeddings` work and are idempotent.
- [ ] `search()` supports `mode=keyword|semantic|hybrid`; `keyword` is byte-for-byte the old behaviour; `semantic`/`hybrid` fall back to keyword when no embedder/embedding is available; ACL filtering is preserved in every mode; results are truncated to `limit`.
- [ ] Hybrid ranking uses RRF (`k=60`) with per-list weights, plus recency and filename field boosts on the fused score.
- [ ] `embed_index` queue handler backfills missing embeddings and is a no-op when the flag is off.
- [ ] `/api/search` accepts `mode`, downgrades to keyword when disabled; `POST /api/index/embeddings/start` enqueues a backfill (admin-only).
- [ ] New embeddings are written on index when an embedder is configured on the store.

---

## Notes for the executing agent

- **sqlite-vec native-lib caveat.** `try_load_sqlite_vec` is the ONLY place that touches the extension, and it must never raise — on most dev/CI machines `conn.enable_load_extension` is either disabled at compile time or the `vec0` lib isn't installed, so it returns `False` and everything runs through the pure-Python `rank_by_cosine`/`iter_block_embeddings` path. Do NOT add `sqlite-vec` to `requirements.txt` in this plan; wiring the accelerated `vec0` MATCH query is a follow-up that must keep the Python fallback as the default. The brute-force scan is O(N·dim) per query over the ACL-visible block set — fine for tens of thousands of blocks; if a profile later shows it's hot, that's when sqlite-vec earns its keep.
- **Embedding cost / throughput.** Each block embed is one Ollama HTTP round-trip; `nomic-embed-text` on CPU is roughly low-tens-of-ms per short block but can be far slower for large blocks or a cold model load. The `embed_index` handler intentionally processes a bounded `batch` (default 500) per job so a backfill of a large corpus is many small, restart-safe queue jobs rather than one multi-hour job — re-enqueue until `get_blocks_without_embedding` returns empty. Store-on-index (`SqliteStore.embed_fn`) adds one round-trip per block to indexing latency; that's why it's gated behind an explicitly-set `embed_fn` and only the app sets it when the flag is on. Keep `EmbeddingService.timeout` generous (60 s) to survive cold model loads.
- **Why RRF, not a weighted cosine+BM25 blend.** BM25 `rank` from FTS5 is an unbounded negative number; cosine is `[-1,1]`. Blending them directly needs per-query normalisation that's brittle. RRF only needs the *order* of each list, so it's scale-free and stable. The per-list weights still let an operator bias toward keyword or vector.
- **Dimension drift.** If `embed_model` changes, old vectors have the wrong `dim`; `rank_by_cosine`/`_vector_doc_order` skip mismatched dims rather than crash, so search keeps working (keyword + whatever vectors still match) until a re-backfill. There is no automatic re-embed on model change — document that operationally (clear `block_embeddings` and re-run `embed_index` after changing the model).
- **Forward-reference gotcha in `app.py`.** `load_effective_config` is defined *inside* `create_app` below where the handlers are registered, but handlers and routes only *call* it at request/job time, so referencing it inside a closure is fine. Constructing `EmbeddingService()` with no model and setting `.model` per-call avoids needing the config at construction time.
- **Keyword refactor is the riskiest step.** Task 5 extracts the existing `search` body verbatim into `_keyword_rows`. The full suite (especially `tests/test_search_acl.py`) is the guardrail — if anything there goes red, the copy diverged from the original; diff and reconcile before proceeding.
