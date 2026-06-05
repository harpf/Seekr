# External Connectors (WebDAV/Nextcloud, S3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `document_search/connectors/` package so Seekr can ingest documents from remote stores (Nextcloud/WebDAV first, then S3-compatible) through the *same* extractor pipeline used for local filesystem crawling. Each ingested document gets its `owner_principal_id` set so the ACL layer (`acl_service.visible_document_ids_subquery`) governs visibility from the moment a connector imports a file. Ingestion runs through the persistent job queue under a new job kind `connector_index`.

**Architecture:** A small `Connector` interface exposes two operations — `list_files()` (iterate remote file descriptors, mirroring `crawler.iter_documents`' filtering contract) and `fetch(remote, dest)` (download one remote file's bytes to a local temp path). Connectors are pure I/O adapters: they do **not** extract, fingerprint, or touch the DB. A new `connector_index` worker handler (in `app.py`) instantiates the configured connector, lists remote files, fetches each to a `tempfile`, then runs the *existing* `fingerprint → extractor_for → upsert_document` pipeline — exactly what `index_paths` does for local paths — but passes `owner_principal_id` resolved from the connector's configured owner mapping. Connector definitions come from `config.json` under `connectors: [...]` (each with `type`, auth, and an `owner` mapping). The HTTP plumbing reuses the stdlib `urllib.request` pattern already established in `services/ai_organizer.py` (explicit `Request`, `urlopen(timeout=...)`, `URLError` handling) so WebDAV needs no new dependency; S3 uses `boto3` against any S3-compatible endpoint.

**Tech Stack:** Python 3.11, stdlib `urllib.request` + `xml.etree.ElementTree` for WebDAV PROPFIND/GET (no new dep), `boto3` for S3 (new dep, optional import), `tempfile`, pytest with `monkeypatch` to simulate remote responses (tests never hit a real server). Windows/PowerShell test invocation.

**Scope boundaries:**

In scope:
- `document_search/connectors/` package: `base.py` (`RemoteFile` dataclass + `Connector` Protocol/ABC + a shared `should_include` filter), `webdav.py` (`WebDavConnector`), `s3.py` (`S3Connector`), `factory.py` (`build_connector(cfg)` dispatch).
- New job kind `connector_index` + handler registered in `create_app` next to `index_paths`.
- `SqliteStore.get_or_create_principal()` helper and an `owner_principal_id` keyword on `upsert_document` so connector-ingested docs are owner-stamped.
- `AppConfig.connectors` config field + README documentation of the `connectors` shape.
- `/api/connectors/index` admin endpoint that enqueues a `connector_index` job for a named connector.
- requirements.txt: add `boto3` (rationale below). WebDAV adds nothing.
- Tests for the filter, the WebDAV connector (mocked `urlopen`), the S3 connector (mocked boto3 client), the factory, and an end-to-end `connector_index` handler test asserting docs are ingested with `owner_principal_id` set.

Out of scope (deferred to later plans):
- SMB/CIFS connector — **already partially handled** by the existing *mount* feature (`SourcePath.mount_point` + `type` in `app.py:122-126`): an admin mounts an SMB share at a local path and indexes it as a normal `source_paths` entry via `index_paths`. A native SMB connector (pysmb/smbprotocol) that sets per-file ACLs from the share is a follow-up, noted here for completeness.
- Incremental / delta sync (ETag-based change detection beyond sha256 skip). The first cut re-lists and re-fingerprints; the existing sha256+mtime skip in the pipeline avoids re-extracting unchanged files, but `fetch` still downloads. ETag-gated fetch is a follow-up.
- Secret management. Connector auth secrets live in `config.json` **in plaintext** today — same caveat as `ollama_url` and `ha_api_keys`. See **Notes for the executing agent** and the security plan reference.
- A connector-management UI. Connectors are defined by editing `config.json`; only the enqueue endpoint is added.
- Writing back to the remote (read-only ingestion only).

---

## Assumptions (design decisions — read before implementing)

1. **Common interface, shared pipeline.** Every connector implements `list_files()` (yields `RemoteFile`) and `fetch(remote, dest)` (writes bytes to a local `Path`). The `connector_index` handler is the *only* place that fingerprints/extracts/upserts — connectors never touch the DB or extractors. This guarantees WebDAV and S3 ingest produce byte-identical index rows to a local crawl of the same files.

2. **Extension/size filtering mirrors `crawler.iter_documents`.** A shared `should_include(name, size, cfg)` helper in `connectors/base.py` applies the same rules the local crawler applies (`supported_extensions`, `exclude_patterns`, `max_file_size_mb`, temp-office `~$` skip, hidden-file skip). Connectors call it so remote listings respect the same config the local crawler respects. (The local crawler keeps its own `os.walk` loop; we extract the per-file predicate, not the traversal.)

3. **Auth secrets come from config (plaintext caveat).** Each connector entry in `config.json` carries its credentials inline (WebDAV: `username`/`password`; S3: `access_key`/`secret_key`/`endpoint_url`/`bucket`). This matches how `ollama_url` and `ha_api_keys` are already stored. It is **not** encrypted at rest. The forthcoming security/secrets plan (`docs/superpowers/plans/` security track) will move these behind an env-var-or-keyring indirection; until then, document the caveat in README and code comments and never log secret values.

4. **Each connector sets `owner_principal_id`.** Every connector entry has an `owner` mapping. The simplest, default form is `{"type": "group", "external_id": "public"}` → docs are owned by the public group (visible to all, preserving today's single-tenant behaviour). A connector that knows the remote owner (e.g. a Nextcloud share owner) can set `{"type": "user", "external_id": "alice@nextcloud"}`; the handler resolves it to a `principals.id` via `SqliteStore.get_or_create_principal(...)` and stamps `documents.owner_principal_id`. If `owner` is omitted, the handler leaves `owner_principal_id` NULL (ACL falls back to the `public` read row that `upsert_document` already grants — see `sqlite_store.py:268-278`).

5. **Start with WebDAV (most concrete), then S3.** WebDAV/Nextcloud is the reference implementation (PROPFIND to list, GET to fetch, stdlib only). S3 follows with the same interface, behind an optional `boto3` import so the dependency is only required when an S3 connector is actually configured.

6. **Tests never hit the network.** WebDAV tests monkeypatch `document_search.connectors.webdav.urllib.request.urlopen` to return canned PROPFIND XML / file bytes. S3 tests inject a fake boto3-style client. The end-to-end handler test uses a `FakeConnector` registered through the factory's seam.

---

## File Structure

**Create:**
- `document_search/connectors/__init__.py` — package marker + public exports.
- `document_search/connectors/base.py` — `RemoteFile`, `Connector` ABC, `should_include`.
- `document_search/connectors/webdav.py` — `WebDavConnector`.
- `document_search/connectors/s3.py` — `S3Connector`.
- `document_search/connectors/factory.py` — `build_connector(entry, cfg)`.
- `tests/test_connectors_base.py` — unit tests for `should_include`.
- `tests/test_connector_webdav.py` — WebDAV connector tests (mocked `urlopen`).
- `tests/test_connector_s3.py` — S3 connector tests (fake boto3 client).
- `tests/test_connector_factory.py` — factory dispatch tests.
- `tests/test_app_connector_index.py` — end-to-end `connector_index` handler + endpoint test.

**Modify:**
- `document_search/index/sqlite_store.py` — add `get_or_create_principal()`; add `owner_principal_id` kwarg to `upsert_document`.
- `document_search/config.py` — add `connectors: list[dict]` field to `AppConfig`.
- `document_search/app.py` — register `connector_index` handler; add `POST /api/connectors/index` endpoint.
- `requirements.txt` — add `boto3`.
- `README.md` — document the `connectors` config block + plaintext-secret caveat.

**Untouched (must remain as-is):**
- `document_search/crawler.py` — keep its `os.walk` traversal; we only mirror its per-file predicate logic in `should_include`, we do not import from it.
- The `index_paths` handler and its endpoints.

---

## Key design decisions (locked)

- **`RemoteFile` is the connector's currency.** It carries `name` (basename), `path` (remote path/key, used as a stable identity and for logging), `size` (bytes, or `None` if unknown until fetch), and `modified` (ISO string or `None`). The handler turns the local temp file into the canonical `FileFingerprint` after fetch, so the index `path` column reflects a synthetic but stable connector URI (`webdav://host/remote/path` or `s3://bucket/key`) — **not** the throwaway temp path.
- **Synthetic index path.** Because the temp file is deleted after extraction, we must not store the temp path in `documents.path` (it would collide/duplicate across runs). The handler builds a stable `index_path = connector.uri_for(remote)` and overrides the fingerprint's `path` with it before `upsert_document`. This keeps re-runs idempotent (same URI → `ON CONFLICT(path) DO UPDATE`).
- **`owner_principal_id` is resolved once per job**, not per file (all files from one connector share one owner mapping in this first cut).
- **S3 dependency is lazy.** `import boto3` happens inside `S3Connector.__init__`, so the package imports cleanly without boto3 installed; only configuring an S3 connector requires it.
- **The factory has a test seam.** `build_connector` reads a module-level `_REGISTRY` dict (`{"webdav": WebDavConnector, "s3": S3Connector}`); tests register a `FakeConnector` under a test type to drive the handler without real I/O.

---

## Task 1: `get_or_create_principal` + `owner_principal_id` on `upsert_document`

**Files:**
- Modify: `document_search/index/sqlite_store.py`
- Test: `tests/test_connectors_base.py` (new — start the file here, extend in Task 2)

- [ ] **Step 1: Write the failing test**

Create `tests/test_connectors_base.py`:

```python
from pathlib import Path
from datetime import UTC, datetime
import pytest

from document_search.index.sqlite_store import SqliteStore
from document_search.models import FileFingerprint, ExtractionResult, ContentBlock


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "test.db")


def test_get_or_create_principal_is_idempotent(store):
    pid1 = store.get_or_create_principal("user", "alice@nextcloud", "Alice")
    pid2 = store.get_or_create_principal("user", "alice@nextcloud", "Alice (again)")
    assert isinstance(pid1, int)
    assert pid1 == pid2
    row = store.conn.execute(
        "SELECT type, external_id FROM principals WHERE id=?", (pid1,)
    ).fetchone()
    assert row["type"] == "user"
    assert row["external_id"] == "alice@nextcloud"


def test_get_or_create_principal_returns_existing_public_group(store):
    # _backfill_acl already created the 'public' group at startup
    pid = store.get_or_create_principal("group", "public", "Everyone")
    existing = store.conn.execute(
        "SELECT id FROM principals WHERE type='group' AND external_id='public'"
    ).fetchone()["id"]
    assert pid == existing


def _fp(path: Path) -> FileFingerprint:
    return FileFingerprint(
        path=path,
        file_size=10,
        modified_at=datetime.now(tz=UTC),
        sha256="deadbeef",
    )


def _result(path: Path) -> ExtractionResult:
    return ExtractionResult(
        file_path=path,
        status="ok",
        blocks=[ContentBlock(block_type="paragraph", block_number=1,
                             text="hello world", extractor="txt")],
    )


def test_upsert_document_sets_owner_principal_id(store):
    owner = store.get_or_create_principal("user", "bob@dav", "Bob")
    p = Path("webdav://host/remote/file.txt")
    doc_id = store.upsert_document(_fp(p), _result(p), owner_principal_id=owner)
    row = store.conn.execute(
        "SELECT owner_principal_id FROM documents WHERE id=?", (doc_id,)
    ).fetchone()
    assert row["owner_principal_id"] == owner


def test_upsert_document_owner_defaults_to_null(store):
    p = Path("/local/file.txt")
    doc_id = store.upsert_document(_fp(p), _result(p))
    row = store.conn.execute(
        "SELECT owner_principal_id FROM documents WHERE id=?", (doc_id,)
    ).fetchone()
    assert row["owner_principal_id"] is None
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_connectors_base.py -v
```

Expected: `AttributeError: 'SqliteStore' object has no attribute 'get_or_create_principal'` and the owner test fails (kwarg not accepted).

- [ ] **Step 3: Add `get_or_create_principal` to `SqliteStore`**

In `document_search/index/sqlite_store.py`, add this method directly **after** `get_document_by_id` (around line 246):

```python
    def get_or_create_principal(
        self, type_: str, external_id: str, display_name: str | None = None
    ) -> int:
        """Look up a principal by (type, external_id), creating it if absent.

        `type_` is 'user' or 'group'. Returns the principal id. Idempotent —
        the UNIQUE(type, external_id) constraint plus INSERT OR IGNORE make
        repeated calls safe.
        """
        from datetime import UTC, datetime
        now = datetime.now(tz=UTC).isoformat()
        self.conn.execute(
            "INSERT OR IGNORE INTO principals(type, external_id, display_name, created_at) "
            "VALUES(?,?,?,?)",
            (type_, external_id, display_name or external_id, now),
        )
        row = self.conn.execute(
            "SELECT id FROM principals WHERE type=? AND external_id=?",
            (type_, external_id),
        ).fetchone()
        self.conn.commit()
        return int(row["id"])
```

- [ ] **Step 4: Add `owner_principal_id` kwarg to `upsert_document`**

In `document_search/index/sqlite_store.py`, change the signature of `upsert_document` (line 248) from:

```python
    def upsert_document(self, fp: FileFingerprint, ext: ExtractionResult) -> int:
```

to:

```python
    def upsert_document(
        self, fp: FileFingerprint, ext: ExtractionResult,
        owner_principal_id: int | None = None,
    ) -> int:
```

Then, in the same method, change the `INSERT INTO documents(...)` statement to include the `owner_principal_id` column. Replace the existing INSERT block (lines 253-266) with:

```python
        self.conn.execute(
            """
            INSERT INTO documents(path, filename, extension, file_size, modified_at, sha256, indexed_at, status, error_message, page_count, slide_count, metadata_json, owner_principal_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
              filename=excluded.filename, extension=excluded.extension, file_size=excluded.file_size, modified_at=excluded.modified_at,
              sha256=excluded.sha256, indexed_at=excluded.indexed_at, status=excluded.status, error_message=excluded.error_message,
              page_count=excluded.page_count, slide_count=excluded.slide_count, metadata_json=excluded.metadata_json,
              owner_principal_id=COALESCE(excluded.owner_principal_id, documents.owner_principal_id)
            """,
            (
                str(fp.path), fp.path.name, fp.path.suffix.lower(), fp.file_size, fp.modified_at.isoformat(), fp.sha256, now,
                ext.status, ext.error_message, meta.get("page_count"), meta.get("slide_count"), str(meta), owner_principal_id,
            ),
        )
```

(`COALESCE(excluded.owner_principal_id, documents.owner_principal_id)` means a later local re-index that passes `None` won't wipe an owner a connector previously set. The existing `index_paths` handler calls `upsert_document(fp, result)` with no owner, so it is unaffected.)

- [ ] **Step 5: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_connectors_base.py -v
```

Expected: 5 passing.

- [ ] **Step 6: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: prior baseline + 5 new, zero regressions. (The `upsert_document` signature change is backward-compatible — `owner_principal_id` defaults to `None`.)

- [ ] **Step 7: Commit**

```powershell
git add document_search/index/sqlite_store.py tests/test_connectors_base.py
git commit -m @'
feat(connectors): owner-aware upsert_document + get_or_create_principal

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 2: `connectors/base.py` — `RemoteFile`, `Connector` ABC, `should_include`

**Files:**
- Create: `document_search/connectors/__init__.py`
- Create: `document_search/connectors/base.py`
- Test: `tests/test_connectors_base.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_connectors_base.py`:

```python
from document_search.config import AppConfig
from document_search.connectors.base import RemoteFile, Connector, should_include


def _cfg(**over) -> AppConfig:
    cfg = AppConfig()
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def test_should_include_accepts_supported_extension():
    cfg = _cfg()
    assert should_include("report.pdf", 1000, cfg) is True
    assert should_include("notes.md", 1000, cfg) is True


def test_should_include_rejects_unsupported_extension():
    cfg = _cfg()
    assert should_include("photo.jpg", 1000, cfg) is False
    assert should_include("archive.zip", 1000, cfg) is False


def test_should_include_rejects_temp_office_file():
    cfg = _cfg()
    assert should_include("~$budget.docx", 1000, cfg) is False


def test_should_include_rejects_exclude_pattern():
    cfg = _cfg(exclude_patterns=["*.tmp", "draft-*"])
    assert should_include("draft-plan.pdf", 1000, cfg) is False
    assert should_include("scratch.tmp", 1000, cfg) is False


def test_should_include_rejects_hidden_when_configured():
    cfg = _cfg(ignore_hidden=True)
    assert should_include(".secret.pdf", 1000, cfg) is False
    cfg2 = _cfg(ignore_hidden=False)
    assert should_include(".secret.pdf", 1000, cfg2) is True


def test_should_include_rejects_oversize():
    cfg = _cfg(max_file_size_mb=1)
    assert should_include("big.pdf", 2 * 1024 * 1024, cfg) is False
    # size None means 'unknown' — accept (we'll size-check after fetch)
    assert should_include("maybe.pdf", None, cfg) is True


def test_connector_is_abstract():
    with pytest.raises(TypeError):
        Connector()  # abstract — cannot instantiate


def test_remotefile_fields():
    rf = RemoteFile(name="a.pdf", path="/remote/a.pdf", size=12, modified="2026-01-01T00:00:00+00:00")
    assert rf.name == "a.pdf"
    assert rf.path == "/remote/a.pdf"
    assert rf.size == 12
```

- [ ] **Step 2: Run, expect FAIL (ImportError)**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_connectors_base.py -v
```

Expected: `ModuleNotFoundError: No module named 'document_search.connectors'`.

- [ ] **Step 3: Create the package marker**

Create `document_search/connectors/__init__.py`:

```python
"""Remote-source connectors.

Each connector lists remote files and fetches their bytes to a local temp file,
feeding the *same* extractor pipeline used for local filesystem crawling. The
`connector_index` job handler (in app.py) owns fingerprinting / extraction /
upsert; connectors are pure I/O adapters and never touch the database.
"""
from document_search.connectors.base import RemoteFile, Connector, should_include

__all__ = ["RemoteFile", "Connector", "should_include"]
```

- [ ] **Step 4: Implement `base.py`**

Create `document_search/connectors/base.py`:

```python
"""Connector interface and the shared inclusion predicate.

`should_include` mirrors the per-file filtering that
`document_search.crawler.iter_documents` applies during a local crawl, so a
remote listing respects the same config (supported extensions, exclude
patterns, max size, temp-office and hidden-file skips).
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterator

from document_search.config import AppConfig


@dataclass(slots=True)
class RemoteFile:
    """A file discovered on a remote source.

    `path` is the remote-native identity (WebDAV href / S3 key). `size` and
    `modified` may be None when the remote does not report them in a listing;
    the handler size-checks again after fetch.
    """
    name: str
    path: str
    size: int | None = None
    modified: str | None = None


def should_include(name: str, size: int | None, config: AppConfig) -> bool:
    """Return True if a remote file with this basename/size should be ingested.

    Mirrors the predicate inside `crawler.iter_documents` (which keeps its own
    os.walk traversal). Kept in one place so local and remote ingestion agree.
    """
    p = Path(name)
    if config.ignore_hidden and name.startswith("."):
        return False
    if config.ignore_temp_office_files and name.startswith("~$"):
        return False
    if any(fnmatch(name, pat) for pat in config.exclude_patterns):
        return False
    suffix = p.suffix.lower()
    if suffix not in {e.lower() for e in config.supported_extensions}:
        return False
    if size is not None and size > config.max_file_size_mb * 1024 * 1024:
        return False
    return True


class Connector(abc.ABC):
    """Common interface for all remote-source connectors.

    Implementations are constructed from a single `connectors[]` entry in
    config.json. They must be safe to use from a worker thread and must not
    touch the database.
    """

    #: short type identifier matching the config `type` field ('webdav', 's3', ...)
    type: str = "base"

    @abc.abstractmethod
    def list_files(self, config: AppConfig) -> Iterator[RemoteFile]:
        """Yield every RemoteFile that passes `should_include`."""
        raise NotImplementedError

    @abc.abstractmethod
    def fetch(self, remote: RemoteFile, dest: Path) -> None:
        """Download `remote`'s bytes to the local path `dest`."""
        raise NotImplementedError

    @abc.abstractmethod
    def uri_for(self, remote: RemoteFile) -> str:
        """Return a stable synthetic URI used as the index `documents.path`.

        Must be deterministic across runs so re-indexing updates the same row
        (ON CONFLICT(path)) rather than creating duplicates.
        """
        raise NotImplementedError
```

- [ ] **Step 5: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_connectors_base.py -v
```

Expected: 13 passing (5 from Task 1 + 8 new).

- [ ] **Step 6: Commit**

```powershell
git add document_search/connectors/__init__.py document_search/connectors/base.py tests/test_connectors_base.py
git commit -m @'
feat(connectors): base Connector interface + shared should_include filter

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 3: `WebDavConnector` (Nextcloud/WebDAV via PROPFIND + GET)

**Files:**
- Create: `document_search/connectors/webdav.py`
- Test: `tests/test_connector_webdav.py` (new)

WebDAV listing uses an HTTP `PROPFIND` with `Depth: 1` (recursing per-collection) returning a `multistatus` XML body. We parse `<d:response>` entries: each has a `<d:href>` and a `<d:propstat>/<d:prop>` with `<d:getcontentlength>`, `<d:getlastmodified>`, and `<d:resourcetype>` (a `<d:collection/>` marks a directory). Fetch is a plain authenticated `GET`. We use stdlib `urllib.request` exactly like `services/ai_organizer.py`.

- [ ] **Step 1: Write the failing tests (mock `urlopen`)**

Create `tests/test_connector_webdav.py`:

```python
from pathlib import Path
import io
import pytest

from document_search.config import AppConfig
from document_search.connectors.webdav import WebDavConnector
import document_search.connectors.webdav as webdav_mod


# A minimal Nextcloud-style multistatus body: one collection (the root, depth 0
# self-entry), one PDF, one JPG (filtered out), one sub-collection.
_PROPFIND_XML = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:">
  <d:response>
    <d:href>/remote.php/dav/files/alice/Docs/</d:href>
    <d:propstat><d:prop>
      <d:resourcetype><d:collection/></d:resourcetype>
    </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/files/alice/Docs/report.pdf</d:href>
    <d:propstat><d:prop>
      <d:getcontentlength>2048</d:getcontentlength>
      <d:getlastmodified>Mon, 05 Jan 2026 10:00:00 GMT</d:getlastmodified>
      <d:resourcetype/>
    </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/files/alice/Docs/photo.jpg</d:href>
    <d:propstat><d:prop>
      <d:getcontentlength>5000</d:getcontentlength>
      <d:resourcetype/>
    </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
</d:multistatus>
"""


class _FakeResponse:
    def __init__(self, body: bytes, status: int = 207):
        self._body = body
        self.status = status
    def read(self):
        return self._body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def _connector():
    return WebDavConnector(
        base_url="https://cloud.example.com/remote.php/dav/files/alice",
        username="alice",
        password="app-token-xyz",
        remote_root="/Docs",
    )


def test_list_files_parses_propfind_and_filters(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["method"] = req.get_method()
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        captured["depth"] = req.get_header("Depth")
        return _FakeResponse(_PROPFIND_XML.encode())

    monkeypatch.setattr(webdav_mod.urllib.request, "urlopen", fake_urlopen)

    cfg = AppConfig()
    files = list(_connector().list_files(cfg))

    # report.pdf included; photo.jpg filtered by should_include; collection skipped
    names = {f.name for f in files}
    assert names == {"report.pdf"}
    rf = files[0]
    assert rf.size == 2048
    assert rf.path.endswith("/Docs/report.pdf")
    # PROPFIND with Depth:1 and Basic auth header
    assert captured["method"] == "PROPFIND"
    assert captured["depth"] == "1"
    assert captured["auth"].startswith("Basic ")


def test_fetch_writes_bytes(monkeypatch, tmp_path):
    def fake_urlopen(req, timeout=None):
        assert req.get_method() == "GET"
        return _FakeResponse(b"%PDF-1.4 fake bytes", status=200)

    monkeypatch.setattr(webdav_mod.urllib.request, "urlopen", fake_urlopen)

    rf = webdav_mod.RemoteFile(name="report.pdf",
                               path="/remote.php/dav/files/alice/Docs/report.pdf",
                               size=2048)
    dest = tmp_path / "report.pdf"
    _connector().fetch(rf, dest)
    assert dest.read_bytes() == b"%PDF-1.4 fake bytes"


def test_uri_for_is_stable():
    c = _connector()
    rf = webdav_mod.RemoteFile(name="report.pdf",
                               path="/remote.php/dav/files/alice/Docs/report.pdf")
    uri = c.uri_for(rf)
    assert uri.startswith("webdav://cloud.example.com/")
    assert uri == c.uri_for(rf)  # deterministic


def test_list_files_raises_friendly_on_http_error(monkeypatch):
    import urllib.error
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))
    monkeypatch.setattr(webdav_mod.urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError, match="WebDAV"):
        list(_connector().list_files(AppConfig()))
```

- [ ] **Step 2: Run, expect FAIL (ImportError)**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_connector_webdav.py -v
```

- [ ] **Step 3: Implement `webdav.py`**

Create `document_search/connectors/webdav.py`:

```python
"""WebDAV / Nextcloud connector.

Lists remote files with PROPFIND (Depth: 1, recursing into sub-collections)
and fetches each with an authenticated GET. Uses stdlib urllib.request — same
HTTP pattern as services/ai_organizer.py — so no third-party dependency.

Auth: HTTP Basic with the configured username/password. For Nextcloud, the
'password' should be an app token (Settings -> Security -> App passwords),
never the account password. The token lives in config.json in PLAINTEXT — see
the plan's secret caveat.
"""
from __future__ import annotations

import base64
import logging
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterator

from document_search.config import AppConfig
from document_search.connectors.base import Connector, RemoteFile, should_include

log = logging.getLogger(__name__)

_DAV_NS = "{DAV:}"
_PROPFIND_BODY = (
    '<?xml version="1.0"?>'
    '<d:propfind xmlns:d="DAV:"><d:prop>'
    "<d:getcontentlength/><d:getlastmodified/><d:resourcetype/>"
    "</d:prop></d:propfind>"
)


class WebDavConnector(Connector):
    type = "webdav"

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        remote_root: str = "/",
        timeout: int = 60,
        max_depth: int = 25,
    ) -> None:
        # base_url is the WebDAV files root, e.g.
        # https://cloud.example.com/remote.php/dav/files/alice
        self.base_url = base_url.rstrip("/")
        parsed = urllib.parse.urlsplit(self.base_url)
        self.scheme = parsed.scheme
        self.host = parsed.netloc
        self.base_path = parsed.path.rstrip("/")
        self.username = username
        self.password = password
        self.remote_root = "/" + remote_root.strip("/")
        self.timeout = timeout
        self.max_depth = max_depth

    # ── auth / request helpers ────────────────────────────────────────────

    def _auth_header(self) -> str:
        raw = f"{self.username}:{self.password}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def _url_for(self, remote_path: str) -> str:
        # remote_path is a server-absolute path (href). Quote spaces etc.
        safe = urllib.parse.quote(remote_path, safe="/%:")
        return f"{self.scheme}://{self.host}{safe}"

    def _propfind(self, collection_path: str) -> bytes:
        url = self._url_for(collection_path)
        req = urllib.request.Request(
            url,
            data=_PROPFIND_BODY.encode(),
            method="PROPFIND",
            headers={
                "Authorization": self._auth_header(),
                "Depth": "1",
                "Content-Type": 'application/xml; charset="utf-8"',
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"WebDAV PROPFIND failed ({e.code}) for {collection_path}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"WebDAV not reachable: {e.reason}") from e

    # ── Connector interface ───────────────────────────────────────────────

    def list_files(self, config: AppConfig) -> Iterator[RemoteFile]:
        start = self.base_path + self.remote_root
        stack = [(start, 0)]
        seen: set[str] = set()
        while stack:
            collection, depth = stack.pop()
            if depth > self.max_depth or collection in seen:
                continue
            seen.add(collection)
            body = self._propfind(collection)
            root = ET.fromstring(body)
            for resp in root.findall(f"{_DAV_NS}response"):
                href_el = resp.find(f"{_DAV_NS}href")
                if href_el is None or not href_el.text:
                    continue
                href = urllib.parse.unquote(href_el.text)
                href_norm = href.rstrip("/")
                # The collection's own self-entry shows up at depth 1 — skip it.
                if href_norm == collection.rstrip("/"):
                    continue
                prop = resp.find(f"{_DAV_NS}propstat/{_DAV_NS}prop")
                is_dir = (
                    prop is not None
                    and prop.find(f"{_DAV_NS}resourcetype/{_DAV_NS}collection") is not None
                )
                if is_dir:
                    stack.append((href_norm, depth + 1))
                    continue
                size = None
                length_el = prop.find(f"{_DAV_NS}getcontentlength") if prop is not None else None
                if length_el is not None and length_el.text and length_el.text.isdigit():
                    size = int(length_el.text)
                modified = None
                mod_el = prop.find(f"{_DAV_NS}getlastmodified") if prop is not None else None
                if mod_el is not None and mod_el.text:
                    modified = mod_el.text
                name = Path(href_norm).name
                if not should_include(name, size, config):
                    continue
                yield RemoteFile(name=name, path=href_norm, size=size, modified=modified)

    def fetch(self, remote: RemoteFile, dest: Path) -> None:
        url = self._url_for(remote.path)
        req = urllib.request.Request(
            url, method="GET", headers={"Authorization": self._auth_header()}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                dest.write_bytes(resp.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"WebDAV GET failed ({e.code}) for {remote.path}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"WebDAV not reachable: {e.reason}") from e

    def uri_for(self, remote: RemoteFile) -> str:
        return f"webdav://{self.host}{remote.path}"
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_connector_webdav.py -v
```

Expected: 4 passing.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: prior total + 4 new, zero regressions.

- [ ] **Step 6: Commit**

```powershell
git add document_search/connectors/webdav.py tests/test_connector_webdav.py
git commit -m @'
feat(connectors): WebDavConnector (Nextcloud PROPFIND list + GET fetch)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 4: `S3Connector` (S3-compatible via boto3)

**Files:**
- Create: `document_search/connectors/s3.py`
- Modify: `requirements.txt` (add `boto3`)
- Test: `tests/test_connector_s3.py` (new)

`boto3` is the canonical SDK for S3-compatible stores (AWS S3, MinIO, Ceph, Wasabi, Backblaze B2 via the S3 API). It is widely deployed, well-maintained, and supports a custom `endpoint_url` so it works against any S3-compatible server — which `minio` (a thinner alternative) also does, but `boto3` is the more common dependency teams already have. We import it lazily so the package loads without it; only configuring an S3 connector requires the install.

- [ ] **Step 1: Write the failing tests (fake boto3 client)**

Create `tests/test_connector_s3.py`:

```python
from pathlib import Path
import pytest

from document_search.config import AppConfig
from document_search.connectors.s3 import S3Connector


class _FakePaginator:
    def __init__(self, pages):
        self._pages = pages
    def paginate(self, **kwargs):
        yield from self._pages


class _FakeS3Client:
    """Mimics the slice of boto3's S3 client the connector uses."""
    def __init__(self, pages, objects):
        self._pages = pages
        self._objects = objects  # key -> bytes
        self.downloaded = []
    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return _FakePaginator(self._pages)
    def download_file(self, Bucket, Key, Filename):
        self.downloaded.append((Bucket, Key))
        Path(Filename).write_bytes(self._objects[Key])


def _connector_with_client(client):
    c = S3Connector(
        bucket="docs",
        access_key="AK",
        secret_key="SK",
        endpoint_url="https://minio.example.com",
        prefix="reports/",
        client=client,  # injected, bypasses boto3
    )
    return c


def test_list_files_filters_and_maps():
    pages = [{
        "Contents": [
            {"Key": "reports/q1.pdf", "Size": 1234, "LastModified": "2026-01-01"},
            {"Key": "reports/cover.jpg", "Size": 999, "LastModified": "2026-01-02"},
            {"Key": "reports/sub/", "Size": 0, "LastModified": "2026-01-02"},  # dir marker
            {"Key": "reports/big.pdf", "Size": 999_999_999, "LastModified": "2026-01-03"},
        ]
    }]
    client = _FakeS3Client(pages, {})
    cfg = AppConfig(max_file_size_mb=100)
    files = list(_connector_with_client(client).list_files(cfg))
    names = {f.name for f in files}
    assert names == {"q1.pdf"}  # jpg filtered, dir marker skipped, big.pdf oversize
    assert files[0].path == "reports/q1.pdf"
    assert files[0].size == 1234


def test_fetch_downloads_to_dest(tmp_path):
    client = _FakeS3Client([], {"reports/q1.pdf": b"PDFBYTES"})
    rf = _connector_with_client(client)  # reuse to get type
    from document_search.connectors.base import RemoteFile
    remote = RemoteFile(name="q1.pdf", path="reports/q1.pdf", size=8)
    dest = tmp_path / "q1.pdf"
    rf.fetch(remote, dest)
    assert dest.read_bytes() == b"PDFBYTES"
    assert client.downloaded == [("docs", "reports/q1.pdf")]


def test_uri_for_is_stable():
    client = _FakeS3Client([], {})
    c = _connector_with_client(client)
    from document_search.connectors.base import RemoteFile
    rf = RemoteFile(name="q1.pdf", path="reports/q1.pdf")
    assert c.uri_for(rf) == "s3://docs/reports/q1.pdf"


def test_init_without_boto3_and_without_client_raises(monkeypatch):
    """If boto3 isn't installed and no client is injected, construction fails
    with a clear message rather than an obscure ImportError at use time."""
    import document_search.connectors.s3 as s3_mod
    monkeypatch.setattr(s3_mod, "_import_boto3", lambda: (_ for _ in ()).throw(
        ImportError("No module named 'boto3'")))
    with pytest.raises(RuntimeError, match="boto3"):
        S3Connector(bucket="docs", access_key="AK", secret_key="SK")
```

- [ ] **Step 2: Run, expect FAIL (ImportError)**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_connector_s3.py -v
```

- [ ] **Step 3: Implement `s3.py`**

Create `document_search/connectors/s3.py`:

```python
"""S3-compatible connector (AWS S3, MinIO, Ceph, Wasabi, B2-S3, ...).

Lists objects with the list_objects_v2 paginator and downloads each with
download_file. boto3 is imported lazily so this module loads even when boto3
isn't installed — only *constructing* an S3Connector (without an injected
client) requires it.

Auth: access_key / secret_key from config.json (PLAINTEXT — see plan caveat).
`endpoint_url` lets it target any S3-compatible server.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator

from document_search.config import AppConfig
from document_search.connectors.base import Connector, RemoteFile, should_include

log = logging.getLogger(__name__)


def _import_boto3():
    import boto3  # local import; raises ImportError if missing
    return boto3


class S3Connector(Connector):
    type = "s3"

    def __init__(
        self,
        bucket: str,
        access_key: str,
        secret_key: str,
        endpoint_url: str | None = None,
        region: str | None = None,
        prefix: str = "",
        client=None,  # injectable for tests
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix.lstrip("/")
        if client is not None:
            self._client = client
        else:
            try:
                boto3 = _import_boto3()
            except ImportError as e:
                raise RuntimeError(
                    "boto3 is required for S3 connectors. Install it with "
                    "`pip install boto3`."
                ) from e
            self._client = boto3.client(
                "s3",
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                endpoint_url=endpoint_url,
                region_name=region,
            )

    def list_files(self, config: AppConfig) -> Iterator[RemoteFile]:
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/"):  # directory placeholder
                    continue
                size = obj.get("Size")
                name = Path(key).name
                if not should_include(name, size, config):
                    continue
                modified = obj.get("LastModified")
                yield RemoteFile(
                    name=name,
                    path=key,
                    size=size,
                    modified=str(modified) if modified is not None else None,
                )

    def fetch(self, remote: RemoteFile, dest: Path) -> None:
        self._client.download_file(
            Bucket=self.bucket, Key=remote.path, Filename=str(dest)
        )

    def uri_for(self, remote: RemoteFile) -> str:
        return f"s3://{self.bucket}/{remote.path}"
```

- [ ] **Step 4: Add boto3 to requirements.txt**

In `requirements.txt`, append after the last line:

```
boto3>=1.34.0
```

- [ ] **Step 5: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_connector_s3.py -v
```

Expected: 4 passing. (Note: tests inject a fake client, so boto3 need not be installed to pass — but install it now so real S3 use works: `pip install boto3`.)

- [ ] **Step 6: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: prior total + 4 new, zero regressions.

- [ ] **Step 7: Commit**

```powershell
git add document_search/connectors/s3.py requirements.txt tests/test_connector_s3.py
git commit -m @'
feat(connectors): S3Connector (boto3, lazy import) + boto3 requirement

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 5: `factory.build_connector` + `AppConfig.connectors`

**Files:**
- Create: `document_search/connectors/factory.py`
- Modify: `document_search/config.py` (add `connectors` field)
- Test: `tests/test_connector_factory.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_connector_factory.py`:

```python
import pytest

from document_search.config import AppConfig, load_config
from document_search.connectors.factory import build_connector, register_connector_type
from document_search.connectors.webdav import WebDavConnector
from document_search.connectors.base import Connector, RemoteFile


def test_build_webdav_connector():
    entry = {
        "name": "nextcloud",
        "type": "webdav",
        "base_url": "https://cloud.example.com/remote.php/dav/files/alice",
        "username": "alice",
        "password": "tok",
        "remote_root": "/Docs",
    }
    c = build_connector(entry)
    assert isinstance(c, WebDavConnector)
    assert c.host == "cloud.example.com"
    assert c.remote_root == "/Docs"


def test_build_s3_connector_injectable(monkeypatch):
    # S3 needs boto3 at construction; register a fake type instead to prove dispatch.
    entry = {"name": "minio", "type": "s3", "bucket": "docs",
             "access_key": "AK", "secret_key": "SK"}
    import document_search.connectors.s3 as s3_mod
    monkeypatch.setattr(s3_mod, "_import_boto3",
                        lambda: type("B", (), {"client": staticmethod(lambda *a, **k: object())}))
    c = build_connector(entry)
    assert c.type == "s3"


def test_unknown_type_raises():
    with pytest.raises(ValueError, match="unknown connector type"):
        build_connector({"name": "x", "type": "ftp"})


def test_register_custom_type_seam():
    class FakeConnector(Connector):
        type = "fake"
        def __init__(self, **kw): self.kw = kw
        def list_files(self, config): return iter(())
        def fetch(self, remote, dest): pass
        def uri_for(self, remote): return f"fake://{remote.path}"
    register_connector_type("fake", FakeConnector)
    c = build_connector({"name": "t", "type": "fake", "foo": 1})
    assert isinstance(c, FakeConnector)
    assert c.kw["foo"] == 1


def test_appconfig_has_connectors_field():
    cfg = AppConfig()
    assert cfg.connectors == []


def test_load_config_reads_connectors(tmp_path):
    import json
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "connectors": [
            {"name": "nc", "type": "webdav",
             "base_url": "https://h/dav", "username": "u", "password": "p"}
        ]
    }), encoding="utf-8")
    cfg = load_config(p)
    assert len(cfg.connectors) == 1
    assert cfg.connectors[0]["type"] == "webdav"
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_connector_factory.py -v
```

Expected: ImportError on `factory`, plus `AppConfig` has no `connectors`.

- [ ] **Step 3: Add `connectors` to `AppConfig`**

In `document_search/config.py`, inside the `AppConfig` dataclass (after the `ocr` field, line 30), add:

```python
    connectors: list[dict] = field(default_factory=list)
```

The existing generic `for key, value in raw.items(): ... elif hasattr(cfg, key): setattr(...)` loop in `load_config` already copies any matching top-level key, so `connectors` is read automatically — no loader change needed.

- [ ] **Step 4: Implement `factory.py`**

Create `document_search/connectors/factory.py`:

```python
"""Construct a Connector from a config.json `connectors[]` entry.

A module-level registry maps the entry's `type` to a connector class. Tests
register a fake type via `register_connector_type` to exercise the
connector_index handler without real network/cloud I/O.
"""
from __future__ import annotations

from typing import Type

from document_search.connectors.base import Connector
from document_search.connectors.webdav import WebDavConnector
from document_search.connectors.s3 import S3Connector

_REGISTRY: dict[str, Type[Connector]] = {
    "webdav": WebDavConnector,
    "s3": S3Connector,
}

# Keys that are connector metadata, not constructor arguments.
_META_KEYS = {"name", "type", "owner", "enabled"}


def register_connector_type(type_name: str, cls: Type[Connector]) -> None:
    _REGISTRY[type_name] = cls


def build_connector(entry: dict) -> Connector:
    """Instantiate the connector described by a single config entry.

    Strips metadata keys (`name`, `type`, `owner`, `enabled`) and forwards the
    rest as keyword arguments to the connector constructor.
    """
    type_name = entry.get("type")
    cls = _REGISTRY.get(type_name)
    if cls is None:
        raise ValueError(f"unknown connector type: {type_name!r}")
    kwargs = {k: v for k, v in entry.items() if k not in _META_KEYS}
    return cls(**kwargs)
```

- [ ] **Step 5: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_connector_factory.py -v
```

Expected: 6 passing.

- [ ] **Step 6: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: prior total + 6 new, zero regressions.

- [ ] **Step 7: Commit**

```powershell
git add document_search/connectors/factory.py document_search/config.py tests/test_connector_factory.py
git commit -m @'
feat(connectors): build_connector factory + AppConfig.connectors field

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 6: `connector_index` job handler + `POST /api/connectors/index`

**Files:**
- Modify: `document_search/app.py` — register a `connector_index` worker handler next to `index_paths`; add an admin endpoint that enqueues it.
- Test: `tests/test_app_connector_index.py` (new)

The handler:
1. Loads effective config, finds the connector entry by `name`.
2. Resolves the `owner` mapping → `owner_principal_id` (via `get_or_create_principal`), or `None`.
3. Builds the connector (`build_connector`).
4. For each `RemoteFile` from `list_files`: fetch to a `tempfile`, fingerprint, override the fingerprint's `path` with `connector.uri_for(remote)` so the index row is stable, extract, `upsert_document(..., owner_principal_id=owner_id)`, delete the temp file, publish progress.
5. Returns the final counts (same `{found,indexed,skipped,updated,errors,done}` shape as `index_paths`).

- [ ] **Step 1: Write the failing test (FakeConnector via registry seam)**

Create `tests/test_app_connector_index.py`:

```python
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from document_search.app import create_app
from document_search.connectors.base import Connector, RemoteFile
from document_search.connectors import factory as connector_factory


def _login(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


class FakeConnector(Connector):
    """In-memory connector: serves two .txt files, one filtered .jpg."""
    type = "fake"

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self._files = {
            "fake://store/a.txt": b"alpha document about budgets",
            "fake://store/b.txt": b"beta document about invoices",
        }

    def list_files(self, config):
        yield RemoteFile(name="a.txt", path="store/a.txt", size=28)
        yield RemoteFile(name="b.txt", path="store/b.txt", size=29)

    def fetch(self, remote, dest):
        dest.write_bytes(self._files[self.uri_for(remote)])

    def uri_for(self, remote):
        return f"fake://store/{remote.name}"


@pytest.fixture(autouse=True)
def _register_fake():
    connector_factory.register_connector_type("fake", FakeConnector)
    yield
    connector_factory._REGISTRY.pop("fake", None)


def _write_config(tmp_path, owner=None):
    import json, os
    entry = {"name": "myfake", "type": "fake"}
    if owner is not None:
        entry["owner"] = owner
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"connectors": [entry]}), encoding="utf-8")
    os.environ["DOCUMENT_SEARCH_CONFIG_PATH"] = str(cfg_path)
    return cfg_path


def test_connector_index_ingests_files_with_owner(tmp_path, monkeypatch):
    _write_config(tmp_path, owner={"type": "group", "external_id": "team-a"})
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        r = client.post("/api/connectors/index",
                        headers={"X-Auth-Token": token},
                        json={"name": "myfake"})
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        assert job_id.isdigit()

        import time
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            s = client.get(f"/api/index/jobs/{job_id}",
                           headers={"X-Auth-Token": token}).json()
            if s["status"] in ("finished", "failed", "interrupted"):
                break
            time.sleep(0.05)
        assert s["status"] == "finished", s
        assert s["found"] == 2
        assert s["indexed"] == 2

    # Verify the docs were upserted with the synthetic URI path and an owner.
    from document_search.index.sqlite_store import SqliteStore
    db = SqliteStore(tmp_path / "t.db")
    rows = db.conn.execute(
        "SELECT path, owner_principal_id FROM documents ORDER BY path"
    ).fetchall()
    paths = [r["path"] for r in rows]
    assert paths == ["fake://store/a.txt", "fake://store/b.txt"]
    team_a = db.conn.execute(
        "SELECT id FROM principals WHERE type='group' AND external_id='team-a'"
    ).fetchone()["id"]
    assert all(r["owner_principal_id"] == team_a for r in rows)


def test_connector_index_unknown_name_returns_404(tmp_path):
    _write_config(tmp_path, owner=None)
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        r = client.post("/api/connectors/index",
                        headers={"X-Auth-Token": token},
                        json={"name": "does-not-exist"})
        assert r.status_code == 404


def test_connector_index_requires_admin(tmp_path):
    _write_config(tmp_path, owner=None)
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        # No token → unauthorized
        r = client.post("/api/connectors/index", json={"name": "myfake"})
        assert r.status_code == 401
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_connector_index.py -v
```

Expected: 404/405 on the missing `/api/connectors/index` route.

- [ ] **Step 3: Register the `connector_index` handler in `app.py`**

In `document_search/app.py`, **after** the `_handle_index_paths` handler block (ends at line 328, before `@worker.handler("ai_suggest_structure")` at line 330), insert:

```python
    @worker.handler("connector_index")
    def _handle_connector_index(payload: dict, progress_cb):
        import logging
        import tempfile
        from document_search.connectors.factory import build_connector

        _clog = logging.getLogger("document_search.connectors")
        name = payload["name"]
        if config_path.exists():
            cfg = load_config(config_path)
        else:
            cfg = AppConfig()
        entry = next(
            (c for c in cfg.connectors if c.get("name") == name), None
        )
        if entry is None:
            raise RuntimeError(f"connector '{name}' not found in config")

        db = SqliteStore(Path(db_path))

        # Resolve owner mapping -> principal id (or None for public-default).
        owner_id = None
        owner = entry.get("owner")
        if isinstance(owner, dict) and owner.get("type") and owner.get("external_id"):
            owner_id = db.get_or_create_principal(
                owner["type"], owner["external_id"], owner.get("display_name")
            )

        connector = build_connector(entry)
        counts = {"found": 0, "indexed": 0, "skipped": 0, "updated": 0, "errors": 0, "done": 0}

        for remote in connector.list_files(cfg):
            counts["found"] += 1
            index_path = Path(connector.uri_for(remote))
            suffix = Path(remote.name).suffix
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            tmp_path = Path(tmp.name)
            tmp.close()
            try:
                connector.fetch(remote, tmp_path)
                fp = fingerprint(tmp_path)
                # Override the throwaway temp path with the stable connector URI
                # so re-runs update the same row (ON CONFLICT(path)).
                fp.path = index_path
                existing = db.get_document(str(index_path))
                extr = extractor_for(suffix.lower())
                if extr is None:
                    counts["done"] += 1
                    progress_cb(dict(counts))
                    continue
                result = extr.extract(tmp_path)
                db.upsert_document(fp, result, owner_principal_id=owner_id)
                if result.status == "error":
                    counts["errors"] += 1
                elif existing:
                    counts["updated"] += 1
                else:
                    counts["indexed"] += 1
            except Exception as exc:
                _clog.warning("connector_index: failed on %s: %s", remote.path, exc)
                counts["errors"] += 1
            finally:
                tmp_path.unlink(missing_ok=True)
            counts["done"] += 1
            progress_cb(dict(counts))
        return counts
```

> **Note on `fp.path = index_path`:** `FileFingerprint` is a `@dataclass(slots=True)` (models.py:27) with a mutable `path` field — assignment works. `extr.extract(tmp_path)` reads the *real* temp file (so extraction sees actual bytes); only the index identity uses the synthetic URI.

> **Note on logging:** `app.py` has no module-level logger today, so the handler creates a local one (`_clog = logging.getLogger("document_search.connectors")`) inside the function. Do not assume a module `logger` exists at the top of `app.py`.

- [ ] **Step 4: Add the `POST /api/connectors/index` endpoint**

Add a request model near the other Pydantic models (e.g. after `IndexRequest`, line 108):

```python
class ConnectorIndexRequest(BaseModel):
    name: str
```

Then add the endpoint. Place it next to `api_index_start` (the `/api/index/start` route). New route:

```python
    @app.post("/api/connectors/index", tags=["index"])
    def api_connectors_index(
        req: ConnectorIndexRequest,
        x_auth_token: str | None = Header(default=None),
    ):
        admin_id = require_admin(x_auth_token)
        cfg = load_effective_config()
        if not any(c.get("name") == req.name for c in cfg.connectors):
            raise HTTPException(status_code=404, detail=f"Connector '{req.name}' not configured")
        job_id = job_store.enqueue(
            "connector_index",
            payload={"name": req.name},
            owner_user_id=admin_id,
            max_retries=0,
        )
        return {"job_id": str(job_id)}
```

> The job's progress shape matches `index_paths`, so the existing `GET /api/index/jobs/{job_id}` already renders it — **but** that endpoint filters on `job["kind"] != "index_paths"` and 404s otherwise. Update that guard to accept `connector_index` too. Find in `api_index_job` (the `/api/index/jobs/{job_id}` route) the line:

```python
        if not job or job["kind"] != "index_paths":
```

and change it to:

```python
        if not job or job["kind"] not in ("index_paths", "connector_index"):
```

- [ ] **Step 5: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_connector_index.py -v
```

Expected: 3 passing.

- [ ] **Step 6: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: prior total + 3 new, zero regressions. If `test_app_connector_index.py` leaks the `DOCUMENT_SEARCH_CONFIG_PATH` env var into later tests, ensure the fixture/test that sets it does so in `tmp_path` and that no other test reads a real config — the existing suite uses temp DBs and default config, so this is isolated, but if a flake appears, convert the `os.environ[...] =` to `monkeypatch.setenv` (already passed into the test signatures).

- [ ] **Step 7: Commit**

```powershell
git add document_search/app.py tests/test_app_connector_index.py
git commit -m @'
feat(connectors): connector_index job kind + POST /api/connectors/index

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 7: README documentation for the `connectors` config block

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Document the `connectors` shape**

In `README.md`, in the configuration section (after the `source_paths` row in the config table, around line 218), add a row:

```
| `connectors` | list | Remote-source connectors (WebDAV/Nextcloud, S3) for owner-aware ingestion |
```

Then, after the config table (after line 220), add a subsection:

````markdown
### External connectors

Connectors ingest documents from remote stores through the same extractor
pipeline as local indexing, stamping each document with an `owner_principal_id`
so the ACL layer governs visibility. Define them in `config.json` under
`connectors` and trigger a run with `POST /api/connectors/index` (admin only):

```json
{
  "connectors": [
    {
      "name": "nextcloud-shared",
      "type": "webdav",
      "base_url": "https://cloud.example.com/remote.php/dav/files/alice",
      "username": "alice",
      "password": "APP_TOKEN_HERE",
      "remote_root": "/Shared/Docs",
      "owner": { "type": "group", "external_id": "public" }
    },
    {
      "name": "minio-reports",
      "type": "s3",
      "bucket": "reports",
      "access_key": "AK...",
      "secret_key": "SK...",
      "endpoint_url": "https://minio.example.com",
      "prefix": "2026/",
      "owner": { "type": "user", "external_id": "ops@example.com" }
    }
  ]
}
```

| Field | Applies to | Description |
|---|---|---|
| `name` | all | Unique identifier; passed to `POST /api/connectors/index`. |
| `type` | all | `webdav` or `s3`. |
| `owner` | all | `{type, external_id}` mapped to a principal that owns ingested docs. Omit for public-default visibility. |
| `base_url` | webdav | WebDAV files root (Nextcloud: `.../remote.php/dav/files/<user>`). |
| `username` / `password` | webdav | Basic-auth credentials. **Use a Nextcloud app token, not the account password.** |
| `remote_root` | webdav | Subpath under the files root to crawl. |
| `bucket` / `access_key` / `secret_key` | s3 | S3 bucket and credentials. |
| `endpoint_url` | s3 | Custom endpoint for S3-compatible stores (MinIO, Ceph, ...). Omit for AWS. |
| `prefix` | s3 | Key prefix to limit the listing. |

> **Security caveat:** connector credentials are stored in `config.json` in
> **plaintext**, exactly like `ollama_url` and `ha_api_keys`. Protect the config
> file with filesystem permissions and prefer scoped, revocable tokens
> (Nextcloud app passwords, least-privilege S3 keys). Encrypted-at-rest secret
> storage is tracked separately in the security plan.

> **SMB/CIFS:** there is no native SMB connector yet. Mount the share at a local
> path (Config → Paths supports a mount point) and index it as a normal
> `source_paths` entry.
````

- [ ] **Step 2: Verify markdown renders (no test)**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: full suite unchanged (docs-only change).

- [ ] **Step 3: Commit**

```powershell
git add README.md
git commit -m @'
docs(connectors): document connectors config block + plaintext-secret caveat

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 8: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Full suite, clean**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: green, zero failures, zero flaky reruns. New tests: 5 (base/owner) + 8 (should_include) + 4 (webdav) + 4 (s3) + 6 (factory) + 3 (handler/endpoint) = 30 new.

- [ ] **Step 2: Import smoke test (package loads without boto3 present)**

```powershell
$env:PYTHONPATH = "."; python -c "import document_search.connectors as c; from document_search.connectors.factory import build_connector; print('connectors import OK', c.__all__)"
```

Expected: prints `connectors import OK ['RemoteFile', 'Connector', 'should_include']` even if boto3 is not installed (the import is lazy).

- [ ] **Step 3: End-to-end smoke via TestClient (fake connector)**

```powershell
$env:PYTHONPATH = "."; python -c "
import json, os, tempfile, time, pathlib
from fastapi.testclient import TestClient
from document_search.connectors.base import Connector, RemoteFile
from document_search.connectors import factory

class Fake(Connector):
    type='fake'
    def __init__(self, **k): pass
    def list_files(self, cfg): yield RemoteFile(name='x.txt', path='s/x.txt', size=5)
    def fetch(self, r, d): d.write_bytes(b'hello world doc')
    def uri_for(self, r): return 'fake://s/x.txt'
factory.register_connector_type('fake', Fake)

tmp = pathlib.Path(tempfile.mkdtemp())
cfg = tmp/'config.json'
cfg.write_text(json.dumps({'connectors':[{'name':'f','type':'fake','owner':{'type':'group','external_id':'public'}}]}))
os.environ['DOCUMENT_SEARCH_CONFIG_PATH'] = str(cfg)
from document_search.app import create_app
app = create_app(str(tmp/'smoke.db'))
with TestClient(app) as c:
    tok = c.post('/api/login', json={'username':'admin','password':'admin'}).json()['token']
    j = c.post('/api/connectors/index', headers={'X-Auth-Token':tok}, json={'name':'f'}).json()
    print('job_id =', j['job_id'])
    for _ in range(60):
        s = c.get(f\"/api/index/jobs/{j['job_id']}\", headers={'X-Auth-Token':tok}).json()
        if s['status'] in ('finished','failed','interrupted'): break
        time.sleep(0.05)
    print('final =', s)
    assert s['status']=='finished' and s['found']==1 and s['indexed']==1
print('OK')
"
```

Expected: prints `job_id = <number>`, `final = {'status':'finished','found':1,'indexed':1,...}`, then `OK`.

- [ ] **Step 4: No commit (verification only)**

---

## Definition of Done

- [ ] `pytest -q` green: ~30 new tests passing, zero regressions.
- [ ] `document_search/connectors/` package exists with `base.py` (`RemoteFile`, `Connector` ABC, `should_include`), `webdav.py`, `s3.py`, `factory.py`, `__init__.py`.
- [ ] `WebDavConnector` lists via PROPFIND (Depth 1, recursing) and fetches via GET, stdlib-only, Basic auth.
- [ ] `S3Connector` lists via `list_objects_v2` paginator and fetches via `download_file`, with lazy boto3 import and an injectable client for tests.
- [ ] `should_include` mirrors `crawler.iter_documents`' per-file filtering (extensions, exclude patterns, max size, temp-office, hidden).
- [ ] `SqliteStore.get_or_create_principal` is idempotent; `upsert_document(..., owner_principal_id=...)` stamps the owner and `COALESCE`s on conflict so local re-index doesn't clear it.
- [ ] `connector_index` job kind: lists remote files, fetches to a temp file, extracts via the existing pipeline, upserts with the stable `connector.uri_for` path and resolved `owner_principal_id`, reports `{found,indexed,skipped,updated,errors,done}` progress, and cleans up temp files.
- [ ] `POST /api/connectors/index` (admin-only) enqueues the job; `GET /api/index/jobs/{id}` renders `connector_index` progress.
- [ ] `AppConfig.connectors` config field + README docs with the plaintext-secret caveat and SMB-via-mount note.
- [ ] `requirements.txt` includes `boto3`.
- [ ] Tests never contact a real server (mocked `urlopen` / fake boto3 client / FakeConnector registry seam).

---

## Notes for the executing agent

- **Plaintext secrets (security caveat).** Connector credentials (`password`, `secret_key`) live in `config.json` unencrypted — identical to the existing `ollama_url`/`ha_api_keys` posture. Do **not** log secret values anywhere (the connectors only log paths/keys, never auth). The forthcoming security/secrets plan will move these behind an env-var-or-keyring indirection; until then this matches the project's current threat model. Reference the ACL plan's own note (`2026-05-17-acl-foundation.md`, "Notes for the executing agent") which anticipated owner-aware ingestion as the connector's job.
- **Why stdlib for WebDAV, boto3 for S3.** WebDAV is plain HTTP with an XML body — `urllib.request` + `xml.etree` (both stdlib, and `urllib.request` is already the established HTTP pattern in `ai_organizer.py`) avoid a `webdavclient3` dependency entirely. S3's signing (SigV4), pagination, and multipart make a hand-rolled client a liability, so `boto3` earns its place; it is imported lazily so the package and the WebDAV path don't require it.
- **Why a synthetic `uri_for` path, not the temp path.** The temp file is deleted after extraction. Storing the temp path in `documents.path` would (a) break the `ON CONFLICT(path)` idempotency (a new temp name every run → duplicate rows) and (b) leak meaningless paths into search results. The stable `webdav://host/...` / `s3://bucket/key` URI keeps re-indexing a true upsert and gives users a recognisable source.
- **`owner_principal_id` COALESCE.** The conflict clause uses `COALESCE(excluded.owner_principal_id, documents.owner_principal_id)` so a later *local* re-index (which passes `None`) does not erase an owner a connector set. Conversely, a connector with an explicit owner *will* overwrite a prior owner — intentional, since the connector is the authority for files it manages.
- **Filtering parity, not traversal sharing.** We deliberately did **not** import from `crawler.py`; we re-implemented only its per-file predicate in `should_include`. The local crawler keeps its `os.walk` traversal. If the local filter rules change later, update both — they are intentionally small and co-located so the drift is obvious. (A future refactor could have `crawler.iter_documents` call `should_include` too; out of scope here to keep the diff focused.)
- **Worker-thread DB.** Like `index_paths`, the handler creates its own `SqliteStore(Path(db_path))` rather than calling the request-thread-local `store()` — the worker is not a request thread. This is the established pattern (see `_handle_index_paths`).
- **S3 `download_file` does its own streaming.** We pass a destination filename and let boto3 stream to disk (memory-safe for large objects). WebDAV's `fetch` currently does `resp.read()` into memory then `write_bytes` — fine for the project's `max_file_size_mb` cap (default 100 MB), but if you raise that cap substantially, switch WebDAV `fetch` to `shutil.copyfileobj(resp, f)` streaming.
- **ETag/delta sync is deferred.** The first cut always fetches every listed file; the pipeline's sha256+mtime check then avoids re-*extracting* unchanged content, but the bytes are still downloaded. ETag-gated fetch (skip download when the remote ETag matches the stored sha256/etag) is the obvious next optimisation and is noted in Scope.
- **Test isolation for `DOCUMENT_SEARCH_CONFIG_PATH`.** `tests/test_app_connector_index.py` sets this env var to a temp config. Prefer `monkeypatch.setenv` (the signatures already receive `monkeypatch`) so it is auto-restored; if you used `os.environ[...] =`, ensure a teardown clears it to avoid leaking into other tests.
