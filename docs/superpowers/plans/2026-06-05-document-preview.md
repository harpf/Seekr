# In-Browser Document Preview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the "force a download" experience in Seekr with an in-browser preview. Add two ACL-gated backend endpoints — one that serves the raw file *inline* with the correct `Content-Type` (PDFs, images, plain text render natively in the browser), and one that returns the already-extracted block text as JSON for non-renderable types (docx/pptx/legacy office). Add a frontend preview modal that renders PDFs via a **vendored PDF.js**, shows images inline, renders text/markdown, and falls back to a download link for anything unsupported. The existing `GET /api/files/open` stays (for explicit downloads) but gains the same ACL check the preview path uses.

**Architecture:** Two new FastAPI routes in `document_search/app.py`:
- `GET /api/files/preview?document_id=N` — resolves the document, runs the *same* ACL filter the search uses (`visible_document_ids_subquery`), guesses a safe MIME type, and returns a `FileResponse` with `Content-Disposition: inline`. A small allow-list maps file extensions to a `preview_kind` (`pdf` | `image` | `text` | `unsupported`); unsupported types still stream inline but the frontend chooses to offer a download instead.
- `GET /api/files/preview-text?document_id=N` — ACL-checked; returns `{filename, extension, blocks: [{block_type, block_number, text}], truncated}` assembled from `content_blocks`. This powers the text/markdown pane for types the browser can't render natively (docx/pptx/legacy office) and is the universal fallback.

The frontend (`document_search/web/static/app.js` + `search.html`) gets a single reusable modal. Each result card's existing "Open file" anchor stays as a real download link; a new "Preview" button opens the modal. The modal dispatches on `preview_kind`: PDF → PDF.js canvas viewer, image → `<img>`, text/markdown → fetched-and-rendered `<pre>`/markdown, unsupported → message + download button. The modal reuses the existing toast styling tokens, is dismissable with `Esc`/backdrop-click/close-button, and is wired for accessibility (`role="dialog"`, `aria-modal`, focus trap, focus restore).

**Tech Stack:** Python 3.11, FastAPI + Starlette `FileResponse`, SQLite, pytest + `fastapi.testclient`. Frontend: vanilla JS, Jinja2 templates, vendored PDF.js (no new Python deps).

**Decision — PDF.js vendored, not CDN (LOCKED):** Seekr is a self-hosted, offline-capable document system (CLAUDE.md: "lokal betriebenes … System"). A CDN dependency would break in air-gapped deployments and leak which documents are viewed to a third party. We **vendor** PDF.js under `document_search/web/static/vendor/pdfjs/` (served by the existing `StaticFiles` mount at `/static`). Version: **pdf.js 4.x legacy build** (`pdf.min.mjs` + `pdf.worker.min.mjs`), Apache-2.0 licensed. The Apache-2.0 `LICENSE` text ships alongside the vendored files. See Task 1 for the exact fetch/placement steps and the license note.

**Scope boundaries:**

In scope:
- `GET /api/files/preview` (inline, ACL-gated, correct content-type) + extension→kind allow-list.
- `GET /api/files/preview-text` (block text JSON, ACL-gated).
- An ACL helper `_user_can_read_document(db, user_id, document_id)` reused by preview, preview-text, AND the existing `/api/files/open` (which today only checks the session, not document visibility — this plan closes that gap).
- Vendored PDF.js under `static/vendor/pdfjs/` + license file.
- A preview modal in `search.html` + `app.js`: PDF.js viewer, image, text/markdown, download fallback; keyboard/backdrop dismiss; aria attributes; focus management.
- Backend tests via TestClient: inline disposition, correct content-type, ACL 404 for non-owner, preview-text returns block text, unsupported-extension classification.

Out of scope (deferred):
- Pagination/zoom/search-within-PDF beyond simple page-by-page rendering.
- Office→PDF server-side conversion (LibreOffice headless) — docx/pptx use the text pane instead.
- Thumbnail generation / preview caching.
- Streaming range requests for huge PDFs (FastAPI `FileResponse` already supports `Range`; no extra work needed).
- Preview on pages other than `/search` (dashboard, wiki) — the modal code is page-agnostic but only wired into `search.html` here.

---

## File Structure

**Create:**
- `document_search/web/static/vendor/pdfjs/pdf.min.mjs` — vendored PDF.js library (downloaded).
- `document_search/web/static/vendor/pdfjs/pdf.worker.min.mjs` — vendored PDF.js worker (downloaded).
- `document_search/web/static/vendor/pdfjs/LICENSE` — Apache-2.0 license text for PDF.js.
- `tests/test_app_preview.py` — TestClient tests for both preview endpoints + ACL.

**Modify:**
- `document_search/app.py` — add `_PREVIEW_KINDS` map, `_user_can_read_document` helper, `GET /api/files/preview`, `GET /api/files/preview-text`; add ACL check to existing `GET /api/files/open`; add `preview_url` / `preview_text_url` / `preview_kind` to the search-result dicts.
- `document_search/web/templates/search.html` — add the preview modal markup + a `<script type="module">` shim that imports the vendored PDF.js, plus load `app.js` (unchanged include).
- `document_search/web/static/app.js` — add a "Preview" button to each result card; implement `openPreview()`, the modal lifecycle, and the four render paths.
- `document_search/web/static/styles.css` — modal styles (backdrop, panel, viewer, controls).

**Untouched:**
- `GET /api/files/open` keeps its download behaviour (now ACL-checked); existing "Open file" anchor in the card is unchanged.
- All job-queue / AI / indexing code.

---

## Key design decisions (locked)

- **Vendored PDF.js, not CDN** (see header). Files live under `static/vendor/pdfjs/`. The worker is configured via `GlobalWorkerOptions.workerSrc = '/static/vendor/pdfjs/pdf.worker.min.mjs'`.
- **Inline vs download:** the new endpoint sets `Content-Disposition: inline; filename="..."`. The legacy `/api/files/open` keeps the Starlette default (attachment-style for unknown types) and is the explicit "download" action.
- **MIME type:** derived from the file extension via Python's stdlib `mimetypes.guess_type`, with a small override table for types `mimetypes` gets wrong/uncertain on Windows (`.md` → `text/markdown`, `.txt` → `text/plain; charset=utf-8`). Never trust the on-disk content; classification is extension-driven and bounded by `_PREVIEW_KINDS`.
- **ACL is mandatory and shared.** `_user_can_read_document` runs the *exact* `visible_document_ids_subquery(user_id)` fragment used by search, so preview visibility can never diverge from search visibility. Non-owner / no-ACL → **404** (not 403 — we don't leak existence, matching how search simply omits invisible docs).
- **preview-text is the universal fallback.** Even for PDFs/images the frontend can fall back to it if the inline render fails. It returns at most `MAX_PREVIEW_BLOCKS = 200` blocks (`truncated: true` when clipped) to bound payload size.
- **No server-side HTML rendering of markdown.** Markdown is returned as raw text; the frontend renders it with a tiny, dependency-free renderer (headings/bold/italic/code/links escaped). This keeps the backend output trustworthy and XSS-safe (the frontend escapes before applying inline formatting).
- **Search-result payload gains three fields** (`preview_url`, `preview_text_url`, `preview_kind`) so the frontend never has to reconstruct URLs or re-derive the kind.

---

## Task 1: Vendor PDF.js + license

**Files:**
- Create: `document_search/web/static/vendor/pdfjs/pdf.min.mjs`
- Create: `document_search/web/static/vendor/pdfjs/pdf.worker.min.mjs`
- Create: `document_search/web/static/vendor/pdfjs/LICENSE`

- [ ] **Step 1: Create the vendor directory and download the pinned PDF.js build**

PDF.js ships a prebuilt distribution. Pin to `4.10.38` (legacy build = widest browser support, plain ES module). Run from the repo root:

```powershell
$ver = "4.10.38"
$dir = "document_search/web/static/vendor/pdfjs"
New-Item -ItemType Directory -Force $dir | Out-Null
$base = "https://cdn.jsdelivr.net/npm/pdfjs-dist@$ver/legacy/build"
Invoke-WebRequest -Uri "$base/pdf.min.mjs"        -OutFile "$dir/pdf.min.mjs"
Invoke-WebRequest -Uri "$base/pdf.worker.min.mjs" -OutFile "$dir/pdf.worker.min.mjs"
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/mozilla/pdf.js/v$ver/LICENSE" -OutFile "$dir/LICENSE"
```

The CDN is used **only at build/vendor time** to fetch the files into the repo. At runtime nothing is fetched from the network — the files are served from the local `/static` mount. This preserves the offline/self-hosted ethos while keeping the vendoring step reproducible.

If the environment is fully air-gapped (no network even at vendor time), obtain the three files from a machine that has the `pdfjs-dist@4.10.38` npm package and copy them in manually; the runtime behaviour is identical.

- [ ] **Step 2: Verify the files exist and are non-trivial**

```powershell
Get-ChildItem document_search/web/static/vendor/pdfjs | Select-Object Name, Length
```

Expected: `pdf.min.mjs` (~300 KB+), `pdf.worker.min.mjs` (~1 MB+), `LICENSE` (~11 KB). If any file is < 1 KB, the download failed (likely an HTML error page) — re-run Step 1.

- [ ] **Step 3: Record the license note**

Confirm `document_search/web/static/vendor/pdfjs/LICENSE` begins with `Apache License` / `Version 2.0`. PDF.js is Apache-2.0; vendoring the minified build plus the `LICENSE` file satisfies the attribution requirement. No source modification is performed, so no "NOTICE of changes" is required.

- [ ] **Step 4: Commit**

```powershell
git add document_search/web/static/vendor/pdfjs
git commit -m "feat(preview): vendor pdf.js 4.10.38 (Apache-2.0) under static/vendor"
```

---

## Task 2: Backend — ACL helper + preview-text endpoint

**Files:**
- Modify: `document_search/app.py`
- Test: `tests/test_app_preview.py` (new)

The existing `GET /api/files/open` (`app.py:1632-1642`) only calls `require_user` — it does **not** check that the caller may read *that specific document*. This task adds a shared `_user_can_read_document` helper (reusing `visible_document_ids_subquery`) and the `preview-text` endpoint that uses it. Task 3 adds the binary `preview` endpoint and retro-fits the helper into `open`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_app_preview.py`:

```python
from datetime import UTC, datetime
from pathlib import Path
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from document_search.app import create_app
from document_search.index.sqlite_store import SqliteStore


def _login(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _seed_doc(db_path: Path, file_path: Path, body: str, ext: str = ".txt",
              blocks: list[str] | None = None, owner_principal_id=None) -> int:
    """Insert a document + content blocks + FTS rows. Returns document_id.

    owner_principal_id=None means the post-open backfill grants 'public' read
    (visible to everyone). Pass a specific principal id to restrict it.
    """
    store = SqliteStore(db_path)
    now = datetime.now(tz=UTC).isoformat()
    cur = store.conn.execute(
        "INSERT INTO documents(path, filename, extension, file_size, modified_at, "
        "sha256, indexed_at, status, owner_principal_id) VALUES(?,?,?,?,?,?,?,?,?)",
        (str(file_path), file_path.name, ext, len(body), now, "hash" + file_path.name,
         now, "ok", owner_principal_id),
    )
    doc_id = cur.lastrowid
    for i, btext in enumerate(blocks or [body], start=1):
        bcur = store.conn.execute(
            "INSERT INTO content_blocks(document_id, block_type, block_number, text, "
            "extractor, text_length) VALUES(?,?,?,?,?,?)",
            (doc_id, "paragraph", i, btext, "txt", len(btext)),
        )
        store.conn.execute(
            "INSERT INTO content_fts(document_id, block_id, path, filename, extension, "
            "block_type, block_number, text) VALUES(?,?,?,?,?,?,?,?)",
            (doc_id, bcur.lastrowid, str(file_path), file_path.name, ext, "paragraph", str(i), btext),
        )
    store.conn.commit()
    store.conn.close()
    return doc_id


def test_preview_text_returns_block_text(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    f = tmp_path / "report.txt"
    f.write_text("alpha beta gamma", encoding="utf-8")
    doc_id = _seed_doc(db, f, "alpha beta gamma", blocks=["alpha beta", "gamma delta"])
    with TestClient(app) as client:
        token = _login(client)
        r = client.get(f"/api/files/preview-text?document_id={doc_id}",
                       headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["filename"] == "report.txt"
        assert [b["text"] for b in body["blocks"]] == ["alpha beta", "gamma delta"]
        assert body["truncated"] is False


def test_preview_text_404_for_unknown_doc(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    with TestClient(app) as client:
        token = _login(client)
        r = client.get("/api/files/preview-text?document_id=999999",
                       headers={"X-Auth-Token": token})
        assert r.status_code == 404
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_preview.py -v
```

Expected: 404 on the route (endpoint not defined yet) — both tests fail.

- [ ] **Step 3: Add the ACL helper + constants near the top of `create_app`**

In `document_search/app.py`, add `import mimetypes` to the stdlib import block at the top of the file (after `import ipaddress` on line 6, keeping alphabetical-ish order):

```python
import mimetypes
```

Then, inside `create_app`, immediately **after** the line `upload_root = Path(...)` (line 266) and before `organizer = AiOrganizer()` (line 267), insert the preview constants and helper:

```python
    # ── Document preview support ──────────────────────────────────────
    MAX_PREVIEW_BLOCKS = 200
    # extension (lowercase, with dot) → preview_kind the frontend dispatches on.
    _PREVIEW_KINDS: dict[str, str] = {
        ".pdf": "pdf",
        ".png": "image", ".jpg": "image", ".jpeg": "image",
        ".gif": "image", ".webp": "image", ".bmp": "image", ".svg": "image",
        ".txt": "text", ".md": "text", ".markdown": "text",
        ".log": "text", ".csv": "text",
    }
    # MIME overrides for types stdlib mimetypes gets wrong / leaves blank on Windows.
    _MIME_OVERRIDES: dict[str, str] = {
        ".md": "text/markdown; charset=utf-8",
        ".markdown": "text/markdown; charset=utf-8",
        ".txt": "text/plain; charset=utf-8",
        ".log": "text/plain; charset=utf-8",
        ".csv": "text/csv; charset=utf-8",
        ".svg": "image/svg+xml",
    }

    def _preview_kind(extension: str) -> str:
        return _PREVIEW_KINDS.get((extension or "").lower(), "unsupported")

    def _preview_mime(extension: str) -> str:
        ext = (extension or "").lower()
        if ext in _MIME_OVERRIDES:
            return _MIME_OVERRIDES[ext]
        guessed, _enc = mimetypes.guess_type("x" + ext)
        return guessed or "application/octet-stream"

    def _user_can_read_document(db: SqliteStore, user_id: int, document_id: int) -> bool:
        """True iff `user_id` may read `document_id`, using the SAME ACL filter as
        search. Keeps preview visibility identical to search visibility."""
        from document_search.services.acl_service import visible_document_ids_subquery
        acl_sql, acl_params = visible_document_ids_subquery(user_id)
        row = db.conn.execute(
            f"SELECT 1 FROM ({acl_sql}) v WHERE v.document_id = ? LIMIT 1",
            (*acl_params, document_id),
        ).fetchone()
        return row is not None
```

- [ ] **Step 4: Add the `preview-text` endpoint**

In `document_search/app.py`, immediately **before** the existing `@app.get("/api/files/open")` (line 1632), insert:

```python
    @app.get("/api/files/preview-text")
    def api_files_preview_text(document_id: int, x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        db = store()
        doc = db.get_document_by_id(document_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        if not _user_can_read_document(db, user_id, document_id):
            # Do not leak existence: same 404 as a missing document.
            raise HTTPException(status_code=404, detail="Document not found")
        rows = db.conn.execute(
            "SELECT block_type, block_number, text FROM content_blocks "
            "WHERE document_id=? ORDER BY block_number LIMIT ?",
            (document_id, MAX_PREVIEW_BLOCKS + 1),
        ).fetchall()
        truncated = len(rows) > MAX_PREVIEW_BLOCKS
        blocks = [
            {"block_type": r["block_type"], "block_number": r["block_number"], "text": r["text"]}
            for r in rows[:MAX_PREVIEW_BLOCKS]
        ]
        return {
            "document_id": document_id,
            "filename": doc["filename"],
            "extension": doc["extension"],
            "preview_kind": _preview_kind(doc["extension"]),
            "blocks": blocks,
            "truncated": truncated,
        }
```

- [ ] **Step 5: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_preview.py -v
```

Expected: 2 passing.

- [ ] **Step 6: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: prior baseline + 2 new, zero failures.

- [ ] **Step 7: Commit**

```powershell
git add document_search/app.py tests/test_app_preview.py
git commit -m "feat(preview): ACL-checked preview-text endpoint + shared ACL helper"
```

---

## Task 3: Backend — inline binary preview endpoint + harden `/open`

**Files:**
- Modify: `document_search/app.py`
- Test: `tests/test_app_preview.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_app_preview.py`:

```python
def test_preview_serves_inline_with_text_content_type(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    f = tmp_path / "note.txt"
    f.write_text("hello inline", encoding="utf-8")
    doc_id = _seed_doc(db, f, "hello inline", ext=".txt")
    with TestClient(app) as client:
        token = _login(client)
        r = client.get(f"/api/files/preview?document_id={doc_id}",
                       headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        assert r.content == b"hello inline"
        assert r.headers["content-type"].startswith("text/plain")
        assert r.headers["content-disposition"].lower().startswith("inline")
        assert r.headers["x-preview-kind"] == "text"


def test_preview_pdf_content_type_and_kind(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    f = tmp_path / "doc.pdf"
    # Minimal bytes; we only assert headers, not PDF validity.
    f.write_bytes(b"%PDF-1.4\n%%EOF\n")
    doc_id = _seed_doc(db, f, "pdf body", ext=".pdf")
    with TestClient(app) as client:
        token = _login(client)
        r = client.get(f"/api/files/preview?document_id={doc_id}",
                       headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("application/pdf")
        assert r.headers["x-preview-kind"] == "pdf"
        assert r.headers["content-disposition"].lower().startswith("inline")


def test_preview_unsupported_extension_still_inline_with_kind(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    f = tmp_path / "archive.zip"
    f.write_bytes(b"PK\x03\x04zip")
    doc_id = _seed_doc(db, f, "zip", ext=".zip")
    with TestClient(app) as client:
        token = _login(client)
        r = client.get(f"/api/files/preview?document_id={doc_id}",
                       headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        assert r.headers["x-preview-kind"] == "unsupported"


def test_preview_is_acl_gated_non_owner_404(tmp_path):
    """A document restricted to 'admin'-only is invisible to a second user."""
    db = tmp_path / "t.db"
    app = create_app(str(db))

    # Create a second, non-admin user and a doc only the admin can see.
    seed = SqliteStore(db)
    seed.create_user("bob", "bobpw")
    now = datetime.now(tz=UTC).isoformat()
    secret = tmp_path / "secret.txt"
    secret.write_text("classified", encoding="utf-8")
    # Owner = admin's user principal; do NOT grant 'public'.
    admin_principal = seed.conn.execute(
        "SELECT principal_id FROM users WHERE username='admin'"
    ).fetchone()["principal_id"]
    cur = seed.conn.execute(
        "INSERT INTO documents(path, filename, extension, file_size, modified_at, sha256, "
        "indexed_at, status, owner_principal_id) VALUES(?,?,?,?,?,?,?,?,?)",
        (str(secret), "secret.txt", ".txt", 10, now, "hsecret", now, "ok", admin_principal),
    )
    doc_id = cur.lastrowid
    bcur = seed.conn.execute(
        "INSERT INTO content_blocks(document_id, block_type, block_number, text, extractor, text_length) "
        "VALUES(?,?,?,?,?,?)", (doc_id, "paragraph", 1, "classified", "txt", 10),
    )
    seed.conn.execute(
        "INSERT INTO content_fts(document_id, block_id, path, filename, extension, block_type, block_number, text) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (doc_id, bcur.lastrowid, str(secret), "secret.txt", ".txt", "paragraph", "1", "classified"),
    )
    # Remove any auto-granted 'public' read so bob truly can't see it.
    public_id = seed.conn.execute(
        "SELECT id FROM principals WHERE type='group' AND external_id='public'"
    ).fetchone()
    if public_id:
        seed.conn.execute(
            "DELETE FROM document_acl WHERE document_id=? AND principal_id=?",
            (doc_id, public_id["id"]),
        )
    seed.conn.commit()
    seed.conn.close()

    with TestClient(app) as client:
        bob = _login(client, "bob", "bobpw")
        # bob cannot preview, preview-text, or open the admin-only doc.
        for url in (f"/api/files/preview?document_id={doc_id}",
                    f"/api/files/preview-text?document_id={doc_id}",
                    f"/api/files/open?document_id={doc_id}"):
            r = client.get(url, headers={"X-Auth-Token": bob})
            assert r.status_code == 404, f"{url} -> {r.status_code}"
        # admin CAN preview it.
        admin = _login(client, "admin", "admin")
        r = client.get(f"/api/files/preview?document_id={doc_id}",
                       headers={"X-Auth-Token": admin})
        assert r.status_code == 200, r.text
```

> Note: if the default `admin` user does not exist with password `admin` in a fresh DB, replace the `admin` login in this test (and `_login`'s defaults) with whatever bootstrap credentials `create_app` seeds. Inspect the login path in `app.py` if `_login(client)` 401s — the same helper is used by the existing `tests/test_app_jobs.py`, so it is known-good.

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_preview.py -v
```

Expected: the four new tests fail — `/api/files/preview` doesn't exist and `/open` isn't ACL-gated yet.

- [ ] **Step 3: Add the inline `preview` endpoint**

In `document_search/app.py`, immediately **before** the `@app.get("/api/files/preview-text")` block you added in Task 2, insert:

```python
    @app.get("/api/files/preview")
    def api_files_preview(document_id: int, x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        db = store()
        doc = db.get_document_by_id(document_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        if not _user_can_read_document(db, user_id, document_id):
            raise HTTPException(status_code=404, detail="Document not found")
        p = Path(doc["path"])
        if not p.exists() or not p.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        ext = doc["extension"] or p.suffix
        kind = _preview_kind(ext)
        media_type = _preview_mime(ext)
        # Inline disposition so the browser renders rather than downloads.
        # filename uses the stored name (ASCII-safe fallback for the header).
        safe_name = doc["filename"].replace('"', "")
        return FileResponse(
            p,
            media_type=media_type,
            headers={
                "Content-Disposition": f'inline; filename="{safe_name}"',
                "X-Preview-Kind": kind,
                "Cache-Control": "private, max-age=60",
            },
        )
```

- [ ] **Step 4: Harden the existing `/api/files/open` with the same ACL check**

Replace the body of `api_files_open` (`app.py:1632-1642`) so it reuses `_user_can_read_document`:

```python
    @app.get("/api/files/open")
    def api_files_open(document_id: int, x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        db = store()
        doc = db.get_document_by_id(document_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        if not _user_can_read_document(db, user_id, document_id):
            raise HTTPException(status_code=404, detail="Document not found")
        p = Path(doc["path"])
        if not p.exists() or not p.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(p)
```

> This is a behaviour change: previously any authenticated user could download any document by id. It's a security fix consistent with the ACL plan. If an existing test asserted the old (over-permissive) behaviour, update it to seed a `public`-readable doc or to use the owning user.

- [ ] **Step 5: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_preview.py -v
```

Expected: 6 passing (2 from Task 2 + 4 new).

- [ ] **Step 6: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: baseline + 6 new, zero failures. If `tests/test_app_search.py` (or similar) has an `/api/files/open` test that relied on cross-user access, fix that test's fixture to grant `public` read (mirror `_seed_doc` with `owner_principal_id=None`).

- [ ] **Step 7: Commit**

```powershell
git add document_search/app.py tests/test_app_preview.py
git commit -m "feat(preview): inline binary preview endpoint; ACL-gate /api/files/open"
```

---

## Task 4: Backend — expose preview URLs in search results

**Files:**
- Modify: `document_search/app.py`
- Test: `tests/test_app_preview.py` (extend)

The search result builder (`app.py:1615-1620`) currently emits `open_url`. Add `preview_url`, `preview_text_url`, and `preview_kind` so the frontend can render without re-deriving anything.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_app_preview.py`:

```python
def test_search_results_include_preview_fields(tmp_path):
    db = tmp_path / "t.db"
    app = create_app(str(db))
    f = tmp_path / "soup.txt"
    f.write_text("secret recipe for soup", encoding="utf-8")
    _seed_doc(db, f, "secret recipe for soup", ext=".txt")
    with TestClient(app) as client:
        token = _login(client)
        r = client.post("/api/search", headers={"X-Auth-Token": token},
                        json={"query": "soup"})
        assert r.status_code == 200, r.text
        results = r.json()  # /api/search returns the list of result dicts directly
        assert results, "expected at least one hit"
        doc = results[0]
        assert "preview_url" in doc
        assert "preview_text_url" in doc
        assert doc["preview_kind"] == "text"
        assert doc["preview_url"].endswith(f"document_id={doc['document_id']}")
```

> Confirmed against `app.py:1579-1621`: `POST /api/search` takes `SearchRequest` (body key `query`) and **returns the list of result dicts directly** (not wrapped in `{"results": ...}`). The per-result `doc` dict already carries `extension` (built at `app.py:1599`), so `_preview_kind(doc.get("extension", ""))` works without re-deriving from the filename.

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_preview.py::test_search_results_include_preview_fields -v
```

- [ ] **Step 3: Add the fields to the result dict**

In `document_search/app.py`, find the result-building append (`app.py:1614-1620`):

```python
            output.append({
                **doc,
                "is_marked": m["is_marked"],
                "tags": m["tags"],
                "open_url": f"/api/files/open?document_id={doc_id}",
                "hit_count": len(doc["hits"]),
            })
```

Replace it with (note: `doc` here already carries an `extension` field used by `_preview_kind`):

```python
            output.append({
                **doc,
                "is_marked": m["is_marked"],
                "tags": m["tags"],
                "open_url": f"/api/files/open?document_id={doc_id}",
                "preview_url": f"/api/files/preview?document_id={doc_id}",
                "preview_text_url": f"/api/files/preview-text?document_id={doc_id}",
                "preview_kind": _preview_kind(doc.get("extension", "")),
                "hit_count": len(doc["hits"]),
            })
```

> Confirmed: the `doc` dict in this loop includes `extension` (set at `app.py:1599`), so `doc.get("extension", "")` is correct as written.

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_preview.py -v
```

Expected: 7 passing.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: baseline + 7 new, zero failures.

- [ ] **Step 6: Commit**

```powershell
git add document_search/app.py tests/test_app_preview.py
git commit -m "feat(preview): expose preview_url/preview_text_url/preview_kind in search results"
```

---

## Task 5: Frontend — modal markup, styles, and PDF.js shim

**Files:**
- Modify: `document_search/web/templates/search.html`
- Modify: `document_search/web/static/styles.css`

Verification for everything frontend in Tasks 5–6 is **manual** (Task 7) — there is no JS test harness in this repo.

- [ ] **Step 1: Add the modal markup to `search.html`**

In `document_search/web/templates/search.html`, immediately **before** `<div class="toast-wrap" id="toastWrap"></div>` (line 193), insert the modal:

```html
  <!-- ── Preview modal ── -->
  <div id="previewModal" class="pv-backdrop hidden" role="dialog" aria-modal="true"
       aria-labelledby="pvTitle" aria-hidden="true">
    <div class="pv-panel" role="document">
      <div class="pv-head">
        <h2 id="pvTitle" class="pv-title">Preview</h2>
        <div class="pv-head-actions">
          <a id="pvDownload" class="btn btn-g btn-sm" href="#" download>Download</a>
          <button id="pvClose" class="btn btn-g btn-sm" aria-label="Close preview" title="Close (Esc)">✕</button>
        </div>
      </div>
      <div id="pvBody" class="pv-body" tabindex="-1">
        <!-- Render target; filled by openPreview() -->
      </div>
      <div id="pvPager" class="pv-pager hidden">
        <button id="pvPrev" class="btn btn-g btn-sm" aria-label="Previous page">‹ Prev</button>
        <span id="pvPageInfo" class="pv-page-info" aria-live="polite">Page 1 / 1</span>
        <button id="pvNext" class="btn btn-g btn-sm" aria-label="Next page">Next ›</button>
      </div>
    </div>
  </div>
```

- [ ] **Step 2: Add the PDF.js module shim before `app.js` loads**

In `search.html`, **replace** the single script include line (line 194):

```html
  <script src="/static/app.js"></script>
```

with a module shim that loads PDF.js, wires the worker, exposes it on `window`, then loads `app.js`:

```html
  <script type="module">
    // Vendored PDF.js (Apache-2.0). Served locally — no CDN at runtime.
    import * as pdfjsLib from '/static/vendor/pdfjs/pdf.min.mjs';
    pdfjsLib.GlobalWorkerOptions.workerSrc = '/static/vendor/pdfjs/pdf.worker.min.mjs';
    window.pdfjsLib = pdfjsLib;
    window.dispatchEvent(new Event('pdfjs-ready'));
  </script>
  <script src="/static/app.js"></script>
```

`app.js` reads `window.pdfjsLib` lazily (only when a PDF preview is opened), so load order between the module and the classic script is not a hard dependency; if `window.pdfjsLib` is missing when a PDF is opened, the modal falls back to the text pane (see Task 6).

- [ ] **Step 3: Add modal styles to `styles.css`**

Append to `document_search/web/static/styles.css` (uses existing CSS custom properties `--b-lo`, `--txt-3`, etc.; if a token name differs, substitute the project's equivalent — grep `:root` in `styles.css`):

```css
/* ── Preview modal ── */
.pv-backdrop {
  position: fixed; inset: 0; z-index: 1000;
  display: flex; align-items: center; justify-content: center;
  background: rgba(0, 0, 0, .55);
  padding: 2vh 2vw;
}
.pv-backdrop.hidden { display: none; }
.pv-panel {
  background: var(--bg-1, #fff);
  color: var(--txt-1, #111);
  border: 1px solid var(--b-lo, #e2e2e2);
  border-radius: 12px;
  width: min(960px, 96vw);
  height: min(90vh, 1000px);
  display: flex; flex-direction: column;
  box-shadow: 0 24px 60px rgba(0, 0, 0, .35);
  overflow: hidden;
}
.pv-head {
  display: flex; align-items: center; justify-content: space-between;
  gap: .75rem; padding: .75rem 1rem;
  border-bottom: 1px solid var(--b-lo, #e2e2e2);
}
.pv-title {
  margin: 0; font-size: 1rem; font-weight: 600;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.pv-head-actions { display: flex; gap: .5rem; flex-shrink: 0; }
.pv-body {
  flex: 1; overflow: auto; padding: 1rem;
  background: var(--bg-2, #f6f7f9);
  display: flex; flex-direction: column; align-items: center; gap: .75rem;
}
.pv-body img { max-width: 100%; height: auto; border-radius: 6px; }
.pv-body canvas { max-width: 100%; box-shadow: 0 2px 8px rgba(0,0,0,.18); background:#fff; }
.pv-text {
  width: 100%; white-space: pre-wrap; word-break: break-word;
  font: .9rem/1.5 var(--mono, ui-monospace, monospace);
  background: var(--bg-1, #fff); padding: 1rem; border-radius: 8px;
  border: 1px solid var(--b-lo, #e2e2e2);
}
.pv-md { width: 100%; line-height: 1.6; }
.pv-md h1, .pv-md h2, .pv-md h3 { margin: .8em 0 .3em; }
.pv-md code { background: var(--bg-2, #eee); padding: .1em .3em; border-radius: 4px; }
.pv-empty { color: var(--txt-3, #888); font-style: italic; padding: 2rem; text-align: center; }
.pv-pager {
  display: flex; align-items: center; justify-content: center; gap: .75rem;
  padding: .6rem; border-top: 1px solid var(--b-lo, #e2e2e2);
}
.pv-pager.hidden { display: none; }
.pv-page-info { font-size: .85rem; color: var(--txt-3, #666); min-width: 7ch; text-align: center; }
```

- [ ] **Step 4: Commit (markup + styles only; behaviour comes in Task 6)**

```powershell
git add document_search/web/templates/search.html document_search/web/static/styles.css
git commit -m "feat(preview): preview modal markup, styles, and vendored pdf.js shim"
```

---

## Task 6: Frontend — modal behaviour and render paths in `app.js`

**Files:**
- Modify: `document_search/web/static/app.js`

- [ ] **Step 1: Add a "Preview" button to each result card**

In `renderResults` (`app.js:430-440`), the footer currently appends `openLink` ("Open file"). **Before** the `openLink` creation, add a Preview button and insert it ahead of the download link:

Find:

```javascript
    const openLink = document.createElement('a');
    openLink.className = 'btn btn-g btn-sm';
    openLink.href = doc.open_url;
    openLink.target = '_blank';
    openLink.textContent = 'Open file';

    foot.appendChild(tagWrap);
    foot.appendChild(saveBtn);
    foot.appendChild(markBtn);
    foot.appendChild(reindexBtn);
    foot.appendChild(openLink);
```

Replace with:

```javascript
    const previewBtn = document.createElement('button');
    previewBtn.className = 'btn btn-p btn-sm';
    previewBtn.textContent = 'Preview';
    previewBtn.addEventListener('click', () => openPreview({
      documentId: doc.document_id,
      filename: doc.filename,
      kind: doc.preview_kind,
      previewUrl: doc.preview_url,
      previewTextUrl: doc.preview_text_url,
      openUrl: doc.open_url,
    }));

    const openLink = document.createElement('a');
    openLink.className = 'btn btn-g btn-sm';
    openLink.href = doc.open_url;
    openLink.target = '_blank';
    openLink.textContent = 'Open file';

    foot.appendChild(tagWrap);
    foot.appendChild(saveBtn);
    foot.appendChild(markBtn);
    foot.appendChild(reindexBtn);
    foot.appendChild(previewBtn);
    foot.appendChild(openLink);
```

Also make the filename header link open the preview instead of forcing navigation. Find (`app.js:341-345`):

```javascript
    const nameLink = document.createElement('a');
    nameLink.className = 'rc-name';
    nameLink.href = doc.open_url;
    nameLink.target = '_blank';
    nameLink.textContent = doc.filename;
```

Replace with:

```javascript
    const nameLink = document.createElement('a');
    nameLink.className = 'rc-name';
    nameLink.href = doc.open_url;          // keep a real href for middle-click/copy
    nameLink.textContent = doc.filename;
    nameLink.addEventListener('click', (e) => {
      e.preventDefault();
      openPreview({
        documentId: doc.document_id,
        filename: doc.filename,
        kind: doc.preview_kind,
        previewUrl: doc.preview_url,
        previewTextUrl: doc.preview_text_url,
        openUrl: doc.open_url,
      });
    });
```

- [ ] **Step 2: Add the preview module to `app.js`**

Append the following block to the **end** of `document_search/web/static/app.js`. It is self-contained and reuses the existing `token`, `escHtml`, `showToast`, and `authedFetch`/`fetch` patterns. If the file uses a helper like `authedHeaders()` or a global `token`, this code follows the same convention (it sends `X-Auth-Token: token`). Verify the auth-header pattern matches the rest of the file before running.

```javascript
// ── Document preview modal ─────────────────────────────────────────
const _pv = {
  el: null, body: null, title: null, download: null, closeBtn: null,
  pager: null, prevBtn: null, nextBtn: null, pageInfo: null,
  lastFocused: null, pdf: null, pageNum: 1, pageCount: 1, rendering: false,
};

function _pvInit() {
  if (_pv.el) return;
  _pv.el = document.getElementById('previewModal');
  if (!_pv.el) return;
  _pv.body = document.getElementById('pvBody');
  _pv.title = document.getElementById('pvTitle');
  _pv.download = document.getElementById('pvDownload');
  _pv.closeBtn = document.getElementById('pvClose');
  _pv.pager = document.getElementById('pvPager');
  _pv.prevBtn = document.getElementById('pvPrev');
  _pv.nextBtn = document.getElementById('pvNext');
  _pv.pageInfo = document.getElementById('pvPageInfo');

  _pv.closeBtn.addEventListener('click', closePreview);
  _pv.el.addEventListener('click', (e) => { if (e.target === _pv.el) closePreview(); });
  _pv.prevBtn.addEventListener('click', () => _pvGoto(_pv.pageNum - 1));
  _pv.nextBtn.addEventListener('click', () => _pvGoto(_pv.pageNum + 1));
  document.addEventListener('keydown', (e) => {
    if (_pv.el.classList.contains('hidden')) return;
    if (e.key === 'Escape') { e.preventDefault(); closePreview(); }
    else if (e.key === 'ArrowLeft' && !_pv.pager.classList.contains('hidden')) _pvGoto(_pv.pageNum - 1);
    else if (e.key === 'ArrowRight' && !_pv.pager.classList.contains('hidden')) _pvGoto(_pv.pageNum + 1);
  });
}

function _pvAuthHeaders() {
  // Match the rest of app.js: bearer-style session token in X-Auth-Token.
  return token ? { 'X-Auth-Token': token } : {};
}

async function openPreview(info) {
  _pvInit();
  if (!_pv.el) return;
  _pv.lastFocused = document.activeElement;
  _pv.title.textContent = info.filename || 'Preview';
  _pv.download.href = info.openUrl || '#';
  _pv.download.setAttribute('download', info.filename || '');
  _pv.body.replaceChildren();
  _pv.pager.classList.add('hidden');
  _pv.pdf = null; _pv.pageNum = 1; _pv.pageCount = 1;

  _pv.el.classList.remove('hidden');
  _pv.el.setAttribute('aria-hidden', 'false');
  _pv.closeBtn.focus();

  const kind = info.kind || 'unsupported';
  try {
    if (kind === 'pdf')        await _pvRenderPdf(info);
    else if (kind === 'image') _pvRenderImage(info);
    else if (kind === 'text')  await _pvRenderText(info);
    else                       await _pvRenderUnsupported(info);
  } catch (err) {
    console.error('preview failed', err);
    _pvShowMessage('Could not render a preview. Use Download to open the file.');
  }
}

function closePreview() {
  if (!_pv.el) return;
  _pv.el.classList.add('hidden');
  _pv.el.setAttribute('aria-hidden', 'true');
  _pv.body.replaceChildren();
  _pv.pdf = null;
  if (_pv.lastFocused && typeof _pv.lastFocused.focus === 'function') _pv.lastFocused.focus();
}

function _pvShowMessage(msg) {
  const d = document.createElement('div');
  d.className = 'pv-empty';
  d.textContent = msg;
  _pv.body.replaceChildren(d);
}

// Authenticated fetch of a binary URL → object URL (so the <img>/PDF carry the token).
async function _pvFetchBlobUrl(url) {
  const r = await fetch(url, { headers: _pvAuthHeaders() });
  if (!r.ok) throw new Error('fetch failed: ' + r.status);
  const blob = await r.blob();
  return URL.createObjectURL(blob);
}

function _pvRenderImage(info) {
  // Images need the auth header; fetch as blob then show.
  _pvShowMessage('Loading image…');
  _pvFetchBlobUrl(info.previewUrl).then((objUrl) => {
    const img = document.createElement('img');
    img.alt = info.filename || 'image preview';
    img.src = objUrl;
    img.addEventListener('load', () => URL.revokeObjectURL(objUrl), { once: true });
    _pv.body.replaceChildren(img);
  }).catch(() => _pvShowMessage('Could not load image. Use Download.'));
}

async function _pvRenderText(info) {
  _pvShowMessage('Loading…');
  const r = await fetch(info.previewTextUrl, { headers: _pvAuthHeaders() });
  if (!r.ok) { _pvShowMessage('Could not load text.'); return; }
  const data = await r.json();
  const text = (data.blocks || []).map(b => b.text).join('\n\n');
  const ext = (data.extension || '').toLowerCase();
  const container = document.createElement('div');
  if (ext === '.md' || ext === '.markdown') {
    container.className = 'pv-md';
    container.innerHTML = _pvRenderMarkdown(text);
  } else {
    container.className = 'pv-text';
    container.textContent = text || '(no extractable text)';
  }
  const kids = [container];
  if (data.truncated) {
    const note = document.createElement('div');
    note.className = 'pv-empty';
    note.textContent = `Showing first ${data.blocks.length} blocks (truncated).`;
    kids.push(note);
  }
  _pv.body.replaceChildren(...kids);
}

async function _pvRenderUnsupported(info) {
  // Try the text pane first (docx/pptx have extracted blocks); else offer download.
  try {
    const r = await fetch(info.previewTextUrl, { headers: _pvAuthHeaders() });
    if (r.ok) {
      const data = await r.json();
      if ((data.blocks || []).length) { await _pvRenderText(info); return; }
    }
  } catch (_e) { /* fall through */ }
  _pvShowMessage('No in-browser preview for this file type. Use Download to open it.');
}

async function _pvRenderPdf(info) {
  const pdfjs = window.pdfjsLib;
  if (!pdfjs) { await _pvRenderUnsupported(info); return; }
  _pvShowMessage('Loading PDF…');
  const r = await fetch(info.previewUrl, { headers: _pvAuthHeaders() });
  if (!r.ok) { _pvShowMessage('Could not load PDF.'); return; }
  const buf = await r.arrayBuffer();
  const doc = await pdfjs.getDocument({ data: buf }).promise;
  _pv.pdf = doc;
  _pv.pageCount = doc.numPages;
  _pv.pageNum = 1;
  _pv.pager.classList.toggle('hidden', doc.numPages <= 1);
  await _pvRenderPdfPage(1);
}

async function _pvRenderPdfPage(n) {
  if (!_pv.pdf || _pv.rendering) return;
  _pv.rendering = true;
  try {
    const page = await _pv.pdf.getPage(n);
    const scale = Math.min(2, (_pv.body.clientWidth - 32) / page.getViewport({ scale: 1 }).width);
    const viewport = page.getViewport({ scale: Math.max(0.5, scale) });
    const canvas = document.createElement('canvas');
    canvas.width = viewport.width;
    canvas.height = viewport.height;
    const ctx = canvas.getContext('2d');
    _pv.body.replaceChildren(canvas);
    await page.render({ canvasContext: ctx, viewport }).promise;
    _pv.pageNum = n;
    _pv.pageInfo.textContent = `Page ${n} / ${_pv.pageCount}`;
    _pv.prevBtn.disabled = (n <= 1);
    _pv.nextBtn.disabled = (n >= _pv.pageCount);
  } finally {
    _pv.rendering = false;
  }
}

function _pvGoto(n) {
  if (!_pv.pdf) return;
  if (n < 1 || n > _pv.pageCount) return;
  _pvRenderPdfPage(n);
}

// Minimal, XSS-safe markdown: escape first, then apply a few inline/block rules.
function _pvRenderMarkdown(src) {
  const esc = escHtml(src);
  return esc
    .replace(/^### (.*)$/gm, '<h3>$1</h3>')
    .replace(/^## (.*)$/gm, '<h2>$1</h2>')
    .replace(/^# (.*)$/gm, '<h1>$1</h1>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
    .replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>')
    .replace(/\n{2,}/g, '<br><br>')
    .replace(/\n/g, '<br>');
}
```

> The markdown renderer relies on the existing `escHtml(...)` helper (used by `showToast` at `app.js:123`). If its name differs, point `_pvRenderMarkdown` at the project's HTML-escaping function. The escape-then-format ordering is deliberate: source is fully escaped before any tags are introduced, so user content can never inject markup.

- [ ] **Step 2 verification is manual** — see Task 7.

- [ ] **Step 3: Commit**

```powershell
git add document_search/web/static/app.js
git commit -m "feat(preview): modal behaviour with pdf.js, image, text/markdown, download fallback"
```

---

## Task 7: Manual verification (frontend)

**Files:** none (manual verification only — there is no JS test runner in this repo).

- [ ] **Step 1: Run the full backend suite once more**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: green, including all `tests/test_app_preview.py` cases.

- [ ] **Step 2: Start the app and seed at least one of each type**

```powershell
$env:PYTHONPATH = "."; python -m uvicorn document_search.app:app --reload --port 8000
```

Then, in a second terminal, index a folder that contains a PDF, a PNG/JPG, a `.txt`, a `.md`, and a `.docx` via the existing ingest flow (or `/api/index/start`).

- [ ] **Step 3: Manually verify each render path in the browser**

Open `http://localhost:8000/search`, sign in, run a query that returns the seeded docs, and for each result:

- **PDF:** click "Preview" → a canvas renders page 1; the pager shows `Page 1 / N`; `Next ›`/`‹ Prev` and `←`/`→` navigate; multi-page docs only show the pager when N > 1.
- **Image:** "Preview" shows the image inline, scaled to fit.
- **.txt:** "Preview" shows monospaced block text.
- **.md:** "Preview" renders headings/bold/code (not raw markdown).
- **.docx/.pptx (unsupported for inline):** "Preview" shows the extracted block text (from `preview-text`); if a type truly has no blocks, it shows the "Use Download" message with a working Download button.
- **Dismiss:** `Esc`, backdrop click, and the ✕ button all close the modal; focus returns to the element that opened it.
- **Accessibility:** with a screen reader / devtools, confirm `role="dialog"`, `aria-modal="true"`, `aria-hidden` toggles, and that focus moves into the dialog on open.
- **Offline check:** in DevTools → Network, confirm PDF.js loads from `/static/vendor/pdfjs/...` and **no** external/CDN request is made when opening a PDF.

- [ ] **Step 4: Confirm the download path still works**

Click "Open file" (and the Download button inside the modal) — the file downloads/opens via `/api/files/open`, which is now ACL-checked.

- [ ] **Step 5: No code change here — no commit unless fixes were needed.**

---

## Definition of Done

- [ ] `$env:PYTHONPATH = "."; pytest -q` is green; `tests/test_app_preview.py` has 7 passing tests.
- [ ] `GET /api/files/preview` returns the file with `Content-Disposition: inline`, the correct `Content-Type` (via `mimetypes` + overrides), and an `X-Preview-Kind` header in {`pdf`,`image`,`text`,`unsupported`}.
- [ ] `GET /api/files/preview-text` returns `{filename, extension, preview_kind, blocks, truncated}` from `content_blocks`, capped at 200 blocks.
- [ ] Both new endpoints AND the existing `GET /api/files/open` are ACL-gated via the shared `_user_can_read_document` helper (which reuses `visible_document_ids_subquery`); a non-owner with no ACL gets **404** on all three.
- [ ] Search results carry `preview_url`, `preview_text_url`, and `preview_kind`.
- [ ] PDF.js is **vendored** under `document_search/web/static/vendor/pdfjs/` with its Apache-2.0 `LICENSE`; no runtime CDN request is made.
- [ ] The preview modal renders PDFs (vendored PDF.js), images, text, and markdown, and falls back to the text pane or a download prompt for unsupported types.
- [ ] The modal is dismissable via `Esc`, backdrop click, and the close button; it sets `role="dialog"`/`aria-modal`, toggles `aria-hidden`, moves focus in on open, and restores focus on close (manually verified — Task 7).
- [ ] Conventional commits used throughout (`feat(preview): ...`).

---

## Notes for the executing agent

- **Why a shared `_user_can_read_document` rather than ad-hoc checks:** preview, preview-text, and open must agree on visibility, and that visibility must equal what search shows. Routing all three through `visible_document_ids_subquery` makes drift impossible. The helper wraps the subquery in `SELECT 1 FROM (<sql>) v WHERE v.document_id = ?` so it's a single bounded lookup, not a full scan.
- **Why 404, not 403, for ACL failures:** search already hides invisible documents entirely. Returning 403 would leak that a document with that id exists. 404 keeps preview consistent with search's "you can't even tell it's there" semantics.
- **The `/api/files/open` change is a real behaviour change** (it used to let any logged-in user fetch any doc by id). It is the correct, intended security posture per the ACL plan. If a pre-existing test relied on cross-user open access, fix the *test* (seed a `public`-readable doc), not the endpoint.
- **Why fetch binary previews as blobs in JS instead of pointing `<img>`/PDF directly at the URL:** the preview endpoints require the `X-Auth-Token` header, which a plain `<img src>` or `pdfjs.getDocument(url)` cannot send. Fetching with the header and handing PDF.js an `ArrayBuffer` (and the image an object URL) keeps auth intact. Remember to `URL.revokeObjectURL` after image load to avoid leaks (done in `_pvRenderImage`).
- **Why vendored PDF.js, restated:** Seekr is explicitly a locally-operated, potentially air-gapped system. A CDN dependency would (a) break offline, and (b) tell a third party which documents are being viewed. The vendoring step uses the CDN once, at build time, to populate `static/vendor/`; runtime is fully local.
- **PDF.js version pin (`4.10.38`, legacy build):** the `legacy` build targets older browsers and avoids top-level-await / newest-syntax pitfalls. If you bump the version, re-verify `GlobalWorkerOptions.workerSrc` still points at `pdf.worker.min.mjs` (the filename has changed across major versions — older builds used `pdf.worker.min.js`).
- **Markdown is rendered client-side, escape-first:** never return server-rendered HTML for previews. `_pvRenderMarkdown` escapes the entire source before introducing any tags, so document content cannot inject markup. Keep that ordering if you extend the renderer.
- **`mimetypes` on Windows** can return `None` or odd values for `.md`/`.csv`; the `_MIME_OVERRIDES` table pins the ones that matter. If you add a new previewable extension, add it to **both** `_PREVIEW_KINDS` and (if `mimetypes` is unreliable for it) `_MIME_OVERRIDES`.
- **If `_login(client)` 401s in tests:** the admin bootstrap credentials may differ from `admin`/`admin`. The same `_login` helper is used by `tests/test_app_jobs.py` (known-good in this repo) — copy its exact credentials/flow if needed.
- **Verify the `/api/search` request/response shape** before relying on Task 4's test: read the `api_search` handler in `app.py`. The per-result dict (where `preview_*` fields are added) is the stable contract; the request envelope may need a tweak to match the live endpoint.
