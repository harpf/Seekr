# P0 Security Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the six P0 security gaps from `docs/ROADMAP.md` (§"P0 — Stabilise & secure"): leaky error responses, root-in-container, symlink/extension-bypassable uploads, unpinned security-sensitive deps, SMB/NFS credentials visible in `/proc/<pid>/cmdline`, and unbounded `subprocess.run` calls. Each item ships with a concrete pytest that asserts the *observable* secure behaviour.

**Architecture:** All web surface lives in `document_search/app.py` (one `create_app(db_path)` factory, ~1650 lines). Hardening stays inside that factory plus a small new helper module for upload validation. Error sanitisation introduces a module-level `logging` logger and a `_client_error(...)` helper that logs the real exception server-side and raises a generic `HTTPException`. Upload hardening adds `document_search/services/upload_validation.py` (containment + `..` rejection + libmagic magic-byte sniffing) that both `/api/upload` and the reorganize path can share. Container hardening edits `Dockerfile` and `docker-compose.yml` only. Dependency pinning edits `requirements.txt` and adds `requirements.lock`. SMB/NFS hardening rewrites `api_path_mount` to use a `credentials=` file and validates the remote path on mount/unmount/test.

**Tech Stack:** Python 3.11, FastAPI/Starlette, SQLite, pytest. One new runtime dependency: `python-magic` (the `libmagic1` shared library is *already* installed in the `Dockerfile`, line 14). On Windows dev hosts `python-magic-bin` provides the DLL; tests degrade gracefully when libmagic is unavailable (see Task 3).

**Scope boundaries:**

In scope:
- Sanitise the two `raise HTTPException(detail=str(e))` leaks (`app.py:1098`, `app.py:1329`) plus add a reusable `_client_error` helper and a module logger.
- Non-root container: add a `seekr` user + group in `Dockerfile`, fix `/data` and `/documents` ownership, drop to that user.
- Upload hardening: explicit containment + `..` rejection + magic-byte sniffing in `/api/upload` (`app.py:661-722`).
- Pin `cryptography`, `PyYAML`, `pillow`, `jinja2`, `python-multipart`; add a `requirements.lock`.
- SMB/NFS: credentials file instead of argv, remote-path validation, `req.path` validation on `api_path_mount` / `api_path_unmount` / `api_path_test`.
- Add `timeout=` to the one `subprocess.run` that lacks it (`app.py:900`, the `/api/update/run` runner).

Out of scope (deferred — see ROADMAP "Next" / later plans):
- Full structured-logging rollout and replacing every `except Exception: pass` (ROADMAP §36 — large, separate plan). This plan adds *only* the logger needed for error sanitisation.
- Linux capability dropping in `docker-compose.yml` beyond what the mount feature needs (the mount feature is documented as requiring `SYS_ADMIN`; we keep it opt-in, see Task 2 Notes).
- CI / ruff wiring (ROADMAP §133 first-slice item, but tracked separately).
- Magic-byte sniffing on the SSL upload endpoint (`/api/ssl/upload`) — PEM is text; out of scope here.

---

## File Structure

**Create:**
- `document_search/services/upload_validation.py` — containment check + libmagic sniffing, shared.
- `tests/test_upload_validation.py` — unit tests for the validator.
- `tests/test_security_hardening.py` — integration tests for error sanitisation, upload rejection, and mount-cred non-leak.
- `requirements.lock` — pinned lockfile (generated, then committed).

**Modify:**
- `document_search/app.py` — add logger + `_client_error`; sanitise `app.py:1098` and `app.py:1329`; harden `/api/upload`; rewrite `api_path_mount`; validate paths on mount/unmount/test; add `timeout=` at `app.py:900`.
- `requirements.txt` — pin the five security-sensitive deps; add `python-magic`.
- `Dockerfile` — create `seekr` user, `chown` writable dirs, add `USER seekr`.
- `docker-compose.yml` — document/keep `cap_add` opt-in for the mount feature.

**Untouched (explicitly):**
- The `git rev-parse` / `nvidia-smi` / Ollama subprocess calls already carry `timeout=` (`app.py:848`, `app.py:1380`) — verified, no change.
- `document_search/extractors/legacy_office_extractor.py:14` already passes `timeout=30` — no change.

---

## Key design decisions (locked)

- **Generic client messages, real detail in the log.** Clients get a stable, non-leaking string (e.g. `"Could not create user"`); the server logs `log.warning("...: %s", exc)` (or `log.exception(...)` for 500s) with the full exception. We never echo `str(e)` to the wire.
- **One logger per module.** `log = logging.getLogger("document_search.app")`. We do NOT call `logging.basicConfig()` inside `create_app` (that would clobber the host's logging config — uvicorn owns it). Tests assert via `caplog`.
- **Upload validation is a pure function** returning `(ok: bool, reason: str | None)` plus a separate `magic_matches_extension(content, ext)` so it's unit-testable without HTTP and degrades when libmagic is missing.
- **Magic-byte policy is allow-by-default-when-unknown for *text* formats.** `.txt`/`.md` have no reliable magic signature; we accept them if libmagic reports a text/plain-ish type OR if the bytes are valid UTF-8-ish text. Binary formats (`.pdf`, `.docx`, `.pptx`, `.doc`, `.ppt`) MUST match their known signature. A `.exe` renamed to `.pdf` is rejected because its magic (`application/x-dosexec`) does not match `.pdf`.
- **Mount credentials go in a `0600` temp file** passed via `-o credentials=<file>`, deleted in a `finally`. The file is created with `tempfile.mkstemp` (then `os.chmod(..., 0o600)`) in the system temp dir the `seekr` user owns. Argv now contains no `password=...`.
- **Remote-path validation** rejects empty paths and shell-meta / option-injection attempts (a leading `-`, embedded NUL). SMB remote paths must start with `//` (UNC); NFS must contain `:` (host:/export). This is defence-in-depth; `subprocess.run` with a list already avoids shell interpretation.
- **Container runs as a fixed UID/GID (10001)** so bind-mounted host volumes can be `chown`ed predictably.

---

## Task 1: Sanitise error responses (+ module logger + `_client_error` helper)

**Files:**
- Modify: `document_search/app.py` — add `import logging` (top, after line 7 `import os`), add module logger (after imports, ~line 47), add `_client_error` helper inside `create_app`, fix the two leaks at `app.py:1098` and `app.py:1329`.
- Test: `tests/test_security_hardening.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_security_hardening.py`:

```python
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from document_search.app import create_app


def _login(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_create_user_duplicate_does_not_leak_exception_text(tmp_path):
    """Creating a duplicate user must return a generic message, NOT the raw
    sqlite3 IntegrityError / UNIQUE-constraint string."""
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        # First create succeeds
        r1 = client.post(
            "/api/users",
            headers={"X-Auth-Token": token},
            json={"username": "dupe", "password": "password123", "role": "user"},
        )
        assert r1.status_code == 200, r1.text
        # Second create collides on the UNIQUE(username) constraint
        r2 = client.post(
            "/api/users",
            headers={"X-Auth-Token": token},
            json={"username": "dupe", "password": "password123", "role": "user"},
        )
        assert r2.status_code == 400
        detail = r2.json()["detail"].lower()
        # Must NOT contain internal exception fingerprints
        for leak in ("unique", "constraint", "sqlite", "integrityerror", "traceback"):
            assert leak not in detail, f"leaked internal detail: {detail!r}"
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_security_hardening.py -v
```

Expected: FAIL — current `app.py:1098` does `raise HTTPException(status_code=400, detail=str(e))`, so the response leaks the sqlite UNIQUE-constraint text.

- [ ] **Step 3: Add the logger import**

In `document_search/app.py`, the top imports run `import json` (line 2) … `import subprocess` (line 16). Add `logging` alphabetically. Find:

```python
import json
import datetime as dt
import hashlib
import html
import ipaddress
import os
```

Replace with:

```python
import json
import datetime as dt
import hashlib
import html
import ipaddress
import logging
import os
```

- [ ] **Step 4: Add the module logger**

In `document_search/app.py`, immediately after the import block (the line `from document_search.services.file_service import fingerprint` at line 46, before the blank lines and the `# Singletons` comment at line 49), insert:

```python


log = logging.getLogger("document_search.app")
```

- [ ] **Step 5: Add the `_client_error` helper inside `create_app`**

In `document_search/app.py`, inside `create_app`, the HA-key helpers section begins at `# ── HA key helpers ───` (line 406). Insert the helper just **above** that comment (after the `def get_config():`/`return AppConfig()` block that ends at line 404):

```python
    # ── Error sanitisation ─────────────────────────────────────────────
    def _client_error(
        public_message: str,
        exc: BaseException,
        status_code: int = 400,
        *,
        server_error: bool = False,
    ) -> HTTPException:
        """Log the real exception server-side and return a generic HTTPException.

        Never echo `str(exc)` to the client — it can leak filesystem paths,
        SQL constraint text, or stack details. `server_error=True` uses
        log.exception (full traceback) for 5xx; otherwise log.warning.
        """
        if server_error:
            log.exception("%s", public_message)
        else:
            log.warning("%s: %s", public_message, exc)
        return HTTPException(status_code=status_code, detail=public_message)
```

- [ ] **Step 6: Fix the user-create leak (`app.py:1098`)**

Find the `api_create_user` `try/except` (lines 1094–1098):

```python
        try:
            user_id = db.create_user(req.username, req.password, req.role)
            return {"id": user_id, "username": req.username, "role": req.role}
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
```

Replace with:

```python
        try:
            user_id = db.create_user(req.username, req.password, req.role)
            return {"id": user_id, "username": req.username, "role": req.role}
        except Exception as e:
            raise _client_error("Could not create user (it may already exist).", e, 400)
```

- [ ] **Step 7: Fix the SSL-generate leak (`app.py:1329`)**

Find the tail of `api_ssl_generate` (lines 1326–1329):

```python
        except ImportError:
            raise HTTPException(status_code=501, detail="cryptography package not installed")
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
```

Replace with:

```python
        except ImportError:
            raise HTTPException(status_code=501, detail="cryptography package not installed")
        except Exception as e:
            raise _client_error("Certificate generation failed.", e, 500, server_error=True)
```

- [ ] **Step 8: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_security_hardening.py -v
```

Expected: `test_create_user_duplicate_does_not_leak_exception_text` passes.

- [ ] **Step 9: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: full suite still green (no behavioural change beyond the two error messages).

- [ ] **Step 10: Commit**

```powershell
git add document_search/app.py tests/test_security_hardening.py
git commit -m "fix(security): sanitise error responses; log detail server-side"
```

---

## Task 2: Run the Docker container as a non-root user

**Files:**
- Modify: `Dockerfile`
- Modify: `docker-compose.yml`
- Test: `tests/test_security_hardening.py` (extend — a static assertion over the Dockerfile, since we cannot run Docker in CI here)

The `Dockerfile` (read in full) has no `USER` directive: `FROM python:3.11-slim` → `WORKDIR /app` → apt installs (incl. `libmagic1` on line 14) → `pip install` → `COPY . .` → `CMD ["uvicorn", ...]`. The app (and the `/api/update/run` shell subprocess) run as root.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_security_hardening.py`:

```python
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_runs_as_non_root():
    """The Dockerfile must create a dedicated user and drop to it before CMD."""
    text = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    # A non-root user is created and selected.
    assert "useradd" in text or "adduser" in text, "no user creation in Dockerfile"
    assert "USER seekr" in text, "Dockerfile does not switch to the seekr user"
    # The USER directive must come before the final CMD.
    user_idx = text.index("USER seekr")
    cmd_idx = text.rindex("CMD")
    assert user_idx < cmd_idx, "USER must precede CMD"
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_security_hardening.py::test_dockerfile_runs_as_non_root -v
```

Expected: FAIL — no `USER seekr`.

- [ ] **Step 3: Edit the `Dockerfile`**

Open `Dockerfile`. The current tail is:

```dockerfile
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV DOCUMENT_SEARCH_DB=/data/document_index.db
EXPOSE 8080

CMD ["uvicorn", "document_search.app:app", "--host", "0.0.0.0", "--port", "8080"]
```

Replace that tail with:

```dockerfile
# Create a dedicated unprivileged user (fixed UID/GID so bind mounts are predictable)
RUN groupadd --gid 10001 seekr \
  && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin seekr

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Writable runtime dirs must be owned by the non-root user.
# /data holds the SQLite DB, config.json, uploads, and SSL material.
RUN mkdir -p /data /documents \
  && chown -R seekr:seekr /data /documents /app

ENV DOCUMENT_SEARCH_DB=/data/document_index.db
EXPOSE 8080

USER seekr

CMD ["uvicorn", "document_search.app:app", "--host", "0.0.0.0", "--port", "8080"]
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_security_hardening.py::test_dockerfile_runs_as_non_root -v
```

Expected: PASS.

- [ ] **Step 5: Make the mount capability explicit and opt-in in `docker-compose.yml`**

In `docker-compose.yml`, the `document-search` service currently lists `expose`, `volumes`, `depends_on`, `restart`. The in-container `mount` feature (`/api/paths/mount`) needs `CAP_SYS_ADMIN`; as a non-root user it ALSO needs that capability granted. Add a commented, opt-in `cap_add` block plus a `user` pin so the bind-mounted `./data` is owned correctly. Find:

```yaml
    expose:
      - "8080"
    volumes:
      - ./data:/data
      - ./documents:/documents:ro
```

Replace with:

```yaml
    expose:
      - "8080"
    # The image runs as the unprivileged user UID/GID 10001 (see Dockerfile).
    # Ensure ./data on the host is writable by that UID, e.g.:
    #   mkdir -p ./data && sudo chown -R 10001:10001 ./data
    user: "10001:10001"
    # ── In-app SMB/NFS mounting (optional) ──────────────────────────────
    # /api/paths/mount needs CAP_SYS_ADMIN even as a non-root user.
    # Uncomment ONLY if you use the in-app network-mount feature.
    # cap_add:
    #   - SYS_ADMIN
    volumes:
      - ./data:/data
      - ./documents:/documents:ro
```

- [ ] **Step 6: Commit**

```powershell
git add Dockerfile docker-compose.yml tests/test_security_hardening.py
git commit -m "fix(security): run container as non-root user (uid 10001)"
```

---

## Task 3: Harden upload path validation (containment + `..` rejection + magic-byte sniffing)

**Files:**
- Create: `document_search/services/upload_validation.py`
- Test: `tests/test_upload_validation.py` (new)
- Then: integration test in `tests/test_security_hardening.py` (extend) + wire into `/api/upload` (`app.py:661-722`).

The current `/api/upload` (read in full, lines 661–692) derives `safe_name = Path(file.filename or "upload.bin").name`, checks the extension against an allowlist (line 672), then does `target = (upload_root / target_subpath).resolve()` and `if upload_root.resolve() not in target.parents and target != upload_root.resolve()`. There is no content/magic check, so a `.exe` renamed `report.pdf` passes the allowlist, and the containment uses `resolve()` only (symlink-exploitable per ROADMAP §30).

### Part A — the validator module (pure functions)

- [ ] **Step 1: Write the failing unit test**

Create `tests/test_upload_validation.py`:

```python
from pathlib import Path
import pytest

from document_search.services.upload_validation import (
    is_within,
    reject_traversal,
    magic_matches_extension,
)


def test_is_within_accepts_child(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir()
    assert is_within(root, root / "sub" / "a.pdf") is True


def test_is_within_rejects_parent_escape(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir()
    assert is_within(root, tmp_path / "etc" / "passwd") is False


def test_reject_traversal_flags_dotdot():
    assert reject_traversal("../../etc/passwd") is True
    assert reject_traversal("a/../../b") is True
    assert reject_traversal("normal/sub/dir") is False
    assert reject_traversal("") is False


# ── Magic-byte sniffing ─────────────────────────────────────────────────
# Real PDF magic header is "%PDF-"; a DOS/PE ("MZ") is an .exe.
PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n"
EXE_BYTES = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00"
TXT_BYTES = b"hello world, this is a plain text note.\n"


def test_magic_accepts_real_pdf_as_pdf():
    ok, _ = magic_matches_extension(PDF_BYTES, ".pdf")
    assert ok is True


def test_magic_rejects_exe_renamed_to_pdf():
    ok, reason = magic_matches_extension(EXE_BYTES, ".pdf")
    assert ok is False
    assert "pdf" in reason.lower()


def test_magic_accepts_plain_text_as_txt():
    ok, _ = magic_matches_extension(TXT_BYTES, ".txt")
    assert ok is True


def test_magic_rejects_binary_blob_as_txt():
    ok, _ = magic_matches_extension(EXE_BYTES, ".txt")
    assert ok is False
```

- [ ] **Step 2: Run, expect FAIL (ImportError)**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_upload_validation.py -v
```

Expected: ImportError — module doesn't exist.

- [ ] **Step 3: Implement `upload_validation.py`**

Create `document_search/services/upload_validation.py`:

```python
"""Upload path + content validation shared by /api/upload and the reorganize path.

Three concerns:
  1. `reject_traversal` — fast textual `..` / NUL screen on the client-supplied
     subpath, before any filesystem resolution.
  2. `is_within` — strict containment check using the *real* (symlink-resolved)
     paths of both the root and the candidate target.
  3. `magic_matches_extension` — libmagic content sniffing so a renamed binary
     (e.g. an .exe called report.pdf) can't slip past the extension allowlist.

libmagic is provided by the `libmagic1` system package (already installed in the
Dockerfile) plus the `python-magic` Python binding. When the binding/library is
unavailable (some dev hosts), magic sniffing for *binary* formats fails closed
only if it can load; if it cannot load at all we degrade to a header-byte check
so the feature still rejects the obvious `.exe`-as-`.pdf` case.
"""
from __future__ import annotations

import os
from pathlib import Path

# Known leading magic signatures, used as a fallback when libmagic is absent
# and as a fast pre-check. Keys are file extensions.
_BINARY_EXTS = {".pdf", ".docx", ".pptx", ".doc", ".ppt"}
_TEXT_EXTS = {".txt", ".md"}

# PDF starts with "%PDF-". OOXML (.docx/.pptx) and modern Office are ZIP ("PK").
# Legacy .doc/.ppt are OLE2 compound files (D0 CF 11 E0 A1 B1 1A E1).
_HEADER_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    ".pdf": (b"%PDF-",),
    ".docx": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    ".pptx": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    ".doc": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    ".ppt": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
}

# Acceptable libmagic MIME types per extension (binary formats only).
_ALLOWED_MIME: dict[str, set[str]] = {
    ".pdf": {"application/pdf"},
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",  # OOXML is a zip; libmagic sometimes reports the container
    },
    ".pptx": {
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/zip",
    },
    ".doc": {"application/msword", "application/x-ole-storage", "application/vnd.ms-office"},
    ".ppt": {"application/vnd.ms-powerpoint", "application/x-ole-storage", "application/vnd.ms-office"},
}


def reject_traversal(subpath: str) -> bool:
    """True if `subpath` must be rejected for traversal/NUL reasons."""
    if not subpath:
        return False
    if "\x00" in subpath:
        return True
    # Normalise separators and look for any '..' path component.
    normalised = subpath.replace("\\", "/")
    return any(part == ".." for part in normalised.split("/"))


def is_within(root: Path, candidate: Path) -> bool:
    """True iff `candidate`, after symlink resolution, is `root` or below it."""
    root_resolved = root.resolve()
    cand_resolved = candidate.resolve()
    if cand_resolved == root_resolved:
        return True
    return root_resolved in cand_resolved.parents


def _header_matches(content: bytes, ext: str) -> bool:
    sigs = _HEADER_SIGNATURES.get(ext)
    if not sigs:
        return False
    return any(content.startswith(sig) for sig in sigs)


def _looks_like_text(content: bytes) -> bool:
    """Heuristic: a small sample decodes as UTF-8 and has no NUL bytes."""
    sample = content[:4096]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def magic_matches_extension(content: bytes, ext: str) -> tuple[bool, str | None]:
    """Return (ok, reason). `ok=False` means the content does not match `ext`.

    Text extensions (.txt/.md) pass when the bytes look like text.
    Binary extensions must match a known MIME (via libmagic) or, if libmagic is
    unavailable, a known leading header signature.
    """
    ext = ext.lower()
    if not content:
        return False, "empty file"

    if ext in _TEXT_EXTS:
        if _looks_like_text(content):
            return True, None
        return False, f"content does not look like text for {ext}"

    if ext in _BINARY_EXTS:
        detected_mime: str | None = None
        try:
            import magic  # python-magic binding over libmagic
            detected_mime = magic.from_buffer(content[:8192], mime=True)
        except Exception:
            detected_mime = None  # libmagic unavailable → fall back to header bytes

        if detected_mime is not None:
            if detected_mime in _ALLOWED_MIME.get(ext, set()):
                return True, None
            return False, (
                f"file content ({detected_mime}) does not match extension {ext}"
            )
        # Fallback: leading signature bytes.
        if _header_matches(content, ext):
            return True, None
        return False, f"file content does not match extension {ext}"

    # Unknown extension — caller's allowlist should already have rejected it.
    return False, f"unsupported extension {ext}"
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_upload_validation.py -v
```

Expected: 8 passing. (`test_magic_rejects_exe_renamed_to_pdf` passes via libmagic MIME `application/x-dosexec` not in the allowlist, OR via the header-byte fallback: `MZ...` does not start with `%PDF-`.)

- [ ] **Step 5: Commit**

```powershell
git add document_search/services/upload_validation.py tests/test_upload_validation.py
git commit -m "feat(security): add upload containment + magic-byte validation helpers"
```

### Part B — wire the validator into `/api/upload`

- [ ] **Step 6: Write the failing integration test**

Append to `tests/test_security_hardening.py`:

```python
import io


def _upload(client, token, filename, content: bytes, subpath=""):
    return client.post(
        "/api/upload",
        headers={"X-Auth-Token": token},
        files={"file": (filename, io.BytesIO(content), "application/octet-stream")},
        data={"target_subpath": subpath},
    )


def test_upload_rejects_exe_renamed_to_pdf(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_UPLOAD_ROOT", str(tmp_path / "uploads"))
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        exe_bytes = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 64
        r = _upload(client, token, "report.pdf", exe_bytes)
        assert r.status_code == 400, r.text
        assert "match" in r.json()["detail"].lower() or "content" in r.json()["detail"].lower()


def test_upload_rejects_traversal_subpath(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_UPLOAD_ROOT", str(tmp_path / "uploads"))
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        r = _upload(client, token, "note.txt", b"hello text", subpath="../../etc")
        assert r.status_code == 400, r.text
        assert "subpath" in r.json()["detail"].lower() or "invalid" in r.json()["detail"].lower()


def test_upload_accepts_real_text_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_UPLOAD_ROOT", str(tmp_path / "uploads"))
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        r = _upload(client, token, "note.txt", b"a genuine plain text note\n")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "uploaded"
```

- [ ] **Step 7: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_security_hardening.py -k upload -v
```

Expected: `test_upload_rejects_exe_renamed_to_pdf` and `test_upload_rejects_traversal_subpath` FAIL — current upload accepts both.

- [ ] **Step 8: Wire validation into `/api/upload`**

In `document_search/app.py`, the upload handler runs lines 661–692. The current body from `user_id = require_user(...)` through `out.write_bytes(content)` is:

```python
        user_id = require_user(x_auth_token)
        safe_name = Path(file.filename or "upload.bin").name
        ext = Path(safe_name).suffix.lower()
        allowed = {".pdf", ".docx", ".pptx", ".txt", ".md", ".doc", ".ppt"}
        if ext not in allowed:
            raise HTTPException(status_code=400, detail=f"Unsupported file extension: {ext}")

        if len(metadata_json) > 8192:
            raise HTTPException(status_code=400, detail="metadata_json exceeds 8 KB limit")

        target = (upload_root / target_subpath).resolve()
        if upload_root.resolve() not in target.parents and target != upload_root.resolve():
            raise HTTPException(status_code=400, detail="Invalid target_subpath")
        target.mkdir(parents=True, exist_ok=True)

        content = await file.read()
        cfg = load_effective_config()
        max_bytes = cfg.max_file_size_mb * 1024 * 1024
        if len(content) > max_bytes:
            raise HTTPException(status_code=413, detail=f"File exceeds {cfg.max_file_size_mb} MB limit")

        digest = hashlib.sha256(content).hexdigest()[:8]
        out = target / f"{Path(safe_name).stem}_{digest}{ext}"
        out.write_bytes(content)
```

Replace exactly that span with:

```python
        user_id = require_user(x_auth_token)
        from document_search.services.upload_validation import (
            is_within,
            reject_traversal,
            magic_matches_extension,
        )
        safe_name = Path(file.filename or "upload.bin").name
        ext = Path(safe_name).suffix.lower()
        allowed = {".pdf", ".docx", ".pptx", ".txt", ".md", ".doc", ".ppt"}
        if ext not in allowed:
            raise HTTPException(status_code=400, detail=f"Unsupported file extension: {ext}")

        if len(metadata_json) > 8192:
            raise HTTPException(status_code=400, detail="metadata_json exceeds 8 KB limit")

        # Reject traversal textually before touching the filesystem.
        if reject_traversal(target_subpath):
            raise HTTPException(status_code=400, detail="Invalid target_subpath")
        target = (upload_root / target_subpath).resolve()
        # Strict containment using symlink-resolved real paths.
        if not is_within(upload_root, target):
            raise HTTPException(status_code=400, detail="Invalid target_subpath")
        target.mkdir(parents=True, exist_ok=True)

        content = await file.read()
        cfg = load_effective_config()
        max_bytes = cfg.max_file_size_mb * 1024 * 1024
        if len(content) > max_bytes:
            raise HTTPException(status_code=413, detail=f"File exceeds {cfg.max_file_size_mb} MB limit")

        # Magic-byte check: the declared extension must match the real content.
        ok, reason = magic_matches_extension(content, ext)
        if not ok:
            log.warning("Rejected upload %r: %s", safe_name, reason)
            raise HTTPException(
                status_code=400,
                detail="File content does not match its extension.",
            )

        digest = hashlib.sha256(content).hexdigest()[:8]
        out = target / f"{Path(safe_name).stem}_{digest}{ext}"
        out.write_bytes(content)
```

- [ ] **Step 9: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_security_hardening.py -k upload -v
```

Expected: all three upload tests pass. (If libmagic is unavailable on the dev host, the `.exe`-as-`.pdf` case still fails the header-byte fallback.)

- [ ] **Step 10: Add `python-magic` to `requirements.txt`** (pin happens in Task 4; add the bare line now so the dep is declared)

In `requirements.txt`, after the `python-multipart>=0.0.9` line (line 12) add:

```
python-magic>=0.4.27
```

- [ ] **Step 11: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: full suite green.

- [ ] **Step 12: Commit**

```powershell
git add document_search/app.py requirements.txt tests/test_security_hardening.py
git commit -m "fix(security): harden upload with containment, traversal + magic-byte checks"
```

---

## Task 4: Pin security-sensitive dependencies + lockfile

**Files:**
- Modify: `requirements.txt`
- Create: `requirements.lock`
- Test: `tests/test_security_hardening.py` (extend — assert the five deps are pinned to a bounded range)

The current `requirements.txt` (read in full) leaves `cryptography>=42.0.0`, `PyYAML>=6.0.1`, `pillow>=10.0.0`, `jinja2>=3.1.4`, `python-multipart>=0.0.9` open-ended (only a floor, no ceiling).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_security_hardening.py`:

```python
import re


def test_security_sensitive_deps_are_bounded():
    """cryptography, PyYAML, pillow, jinja2, python-multipart must have an upper
    bound (a `<` constraint) so a surprise major bump can't land unreviewed."""
    text = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    sensitive = ["cryptography", "PyYAML", "pillow", "jinja2", "python-multipart"]
    lines = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[<>=!~ ]", line, 1)[0]
        lines[name.lower()] = line
    for dep in sensitive:
        assert dep.lower() in lines, f"{dep} missing from requirements.txt"
        assert "<" in lines[dep.lower()], f"{dep} has no upper bound: {lines[dep.lower()]!r}"


def test_lockfile_exists_and_pins_sensitive_deps():
    lock = REPO_ROOT / "requirements.lock"
    assert lock.exists(), "requirements.lock missing"
    text = lock.read_text(encoding="utf-8").lower()
    for dep in ("cryptography", "pyyaml", "pillow", "jinja2", "python-multipart"):
        assert dep in text, f"{dep} not pinned in requirements.lock"
        # A lockfile pins exact versions with '=='
    assert "==" in text
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_security_hardening.py -k "deps or lockfile" -v
```

Expected: both FAIL — no upper bounds, no lockfile.

- [ ] **Step 3: Pin the five deps in `requirements.txt`**

The current `requirements.txt` is:

```
pypdf>=4.0.0
python-docx>=1.1.0
python-pptx>=0.6.23
PyYAML>=6.0.1
fastapi>=0.115.0
uvicorn>=0.32.0
jinja2>=3.1.4
pillow>=10.0.0
pytesseract>=0.3.10
pdf2image>=1.17.0
pytest>=8.0.0
python-multipart>=0.0.9
python-magic>=0.4.27

starlette==0.50.0
cryptography>=42.0.0
psutil>=5.9.0
```

Replace the whole file with (upper bounds on the five security-sensitive packages; everything else unchanged):

```
pypdf>=4.0.0
python-docx>=1.1.0
python-pptx>=0.6.23
PyYAML>=6.0.1,<7.0
fastapi>=0.115.0
uvicorn>=0.32.0
jinja2>=3.1.4,<4.0
pillow>=10.0.0,<12.0
pytesseract>=0.3.10
pdf2image>=1.17.0
pytest>=8.0.0
python-multipart>=0.0.9,<0.1
python-magic>=0.4.27,<0.5

starlette==0.50.0
cryptography>=42.0.0,<46.0
psutil>=5.9.0
```

- [ ] **Step 4: Generate the lockfile**

Use pip's own resolver to freeze a reproducible pin set from the current environment. From the repo root:

```powershell
pip install -r requirements.txt
pip freeze > requirements.lock
```

> If `pip freeze` emits editable/`-e` or local path entries, delete those lines from `requirements.lock` by hand — the lockfile must contain only `name==version` rows. Verify it contains `cryptography==`, `PyYAML==`/`pyyaml==`, `pillow==`/`Pillow==`, `Jinja2==`/`jinja2==`, `python-multipart==`.

- [ ] **Step 5: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_security_hardening.py -k "deps or lockfile" -v
```

Expected: both pass.

- [ ] **Step 6: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: green.

- [ ] **Step 7: Commit**

```powershell
git add requirements.txt requirements.lock tests/test_security_hardening.py
git commit -m "fix(security): pin security-sensitive deps and add requirements.lock"
```

---

## Task 5: Protect SMB/NFS credentials (credentials file + remote-path validation)

**Files:**
- Modify: `document_search/app.py` — rewrite `api_path_mount` (`app.py:1167-1199`); validate `req.path` in `api_path_unmount` (`app.py:1201-1212`) and `api_path_test` (`app.py:1134-1165`); add a `_validate_remote_path` / `_validate_mount_path` helper.
- Test: `tests/test_security_hardening.py` (extend)

The current `api_path_mount` (read in full, lines 1178–1192) builds, for SMB:

```python
            options = ["vers=3.0"]
            if req.username:
                options.append(f"username={req.username}")
            if req.password:
                options.append(f"password={req.password}")
            if req.domain:
                options.append(f"domain={req.domain}")
            cmd = ["mount", "-t", "cifs", req.remote_path, str(mount_point), "-o", ",".join(options)]
```

— so the password lands in `cmd` (argv), readable via `/proc/<pid>/cmdline`. The NFS branch (line 1188) and `req.remote_path` are unvalidated.

The cleanest *testable* assertion: monkeypatch `subprocess.run` and assert that no password value ever appears in the argv (`cmd`) it receives, while the password DOES reach a `credentials=` file path passed via `-o`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_security_hardening.py`:

```python
import document_search.app as appmod


def test_mount_does_not_leak_credentials_in_argv(tmp_path, monkeypatch):
    """The mount subprocess argv must never contain the literal password;
    credentials must be passed via an -o credentials=<file> option instead."""
    captured = {}

    def fake_run(cmd, *a, **kw):
        captured["cmd"] = list(cmd)
        # Read back the credentials file if one was referenced.
        creds_file = None
        for i, tok in enumerate(cmd):
            if tok == "-o":
                opts = cmd[i + 1]
                for opt in opts.split(","):
                    if opt.startswith("credentials="):
                        creds_file = opt.split("=", 1)[1]
        captured["creds_file"] = creds_file
        if creds_file:
            captured["creds_contents"] = Path(creds_file).read_text(encoding="utf-8")

        class _P:
            returncode = 0
            stdout = ""
            stderr = ""
        return _P()

    monkeypatch.setattr(appmod.subprocess, "run", fake_run)
    monkeypatch.setattr(appmod.os, "name", "posix", raising=False)

    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        r = client.post(
            "/api/paths/mount",
            headers={"X-Auth-Token": token},
            json={
                "remote_path": "//server/share",
                "mount_point": str(tmp_path / "mnt"),
                "share_type": "smb",
                "username": "alice",
                "password": "SuperSecret123",
                "domain": "WORKGROUP",
            },
        )
        assert r.status_code == 200, r.text

    argv_joined = " ".join(captured["cmd"])
    assert "SuperSecret123" not in argv_joined, f"password leaked in argv: {captured['cmd']}"
    assert "password=SuperSecret123" not in argv_joined
    # Credentials must have gone to a file referenced by -o credentials=
    assert captured.get("creds_file"), "no credentials= file used"
    assert "SuperSecret123" in captured.get("creds_contents", "")


def test_mount_rejects_bad_remote_path(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod.os, "name", "posix", raising=False)
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        # Leading dash → option-injection attempt; must be rejected.
        r = client.post(
            "/api/paths/mount",
            headers={"X-Auth-Token": token},
            json={
                "remote_path": "-o,attacker=1",
                "mount_point": str(tmp_path / "mnt"),
                "share_type": "smb",
            },
        )
        assert r.status_code == 400, r.text
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_security_hardening.py -k mount -v
```

Expected: `test_mount_does_not_leak_credentials_in_argv` FAILS (password is in argv); `test_mount_rejects_bad_remote_path` FAILS (no validation).

- [ ] **Step 3: Add `tempfile` import**

In `document_search/app.py`, the import block ends with `import subprocess` (line 16). Add `tempfile` alphabetically:

```python
import subprocess
import tempfile
```

- [ ] **Step 4: Add the remote-path validator inside `create_app`**

In `document_search/app.py`, just below the `_client_error` helper you added in Task 1 (still inside `create_app`, before `# ── HA key helpers ───`), insert:

```python
    def _validate_remote_path(remote_path: str, share_type: str) -> None:
        """Reject empty / option-injecting / malformed remote share paths."""
        rp = (remote_path or "").strip()
        if not rp:
            raise HTTPException(status_code=400, detail="remote_path must not be empty")
        if "\x00" in rp or "," in rp:
            raise HTTPException(status_code=400, detail="remote_path contains illegal characters")
        if rp.startswith("-"):
            raise HTTPException(status_code=400, detail="remote_path must not start with '-'")
        if share_type == "smb" and not rp.startswith("//"):
            raise HTTPException(status_code=400, detail="SMB remote_path must be a //server/share UNC path")
        if share_type == "nfs" and ":" not in rp:
            raise HTTPException(status_code=400, detail="NFS remote_path must be host:/export")
```

- [ ] **Step 5: Rewrite `api_path_mount`**

In `document_search/app.py`, replace the whole `api_path_mount` body (lines 1167–1199 — from `@app.post("/api/paths/mount")` through the `return {...}` ending with `"mount_point": str(mount_point),\n        }`):

```python
    @app.post("/api/paths/mount")
    def api_path_mount(req: MountRequest, x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        if os.name == "nt":
            raise HTTPException(status_code=400, detail="Mount via API not supported on Windows; use Docker volume mounts instead")
        if req.share_type not in ("smb", "nfs"):
            raise HTTPException(status_code=400, detail=f"Unknown share type: {req.share_type}. Use 'smb' or 'nfs'")
        _validate_remote_path(req.remote_path, req.share_type)

        mount_point = Path(req.mount_point)
        try:
            mount_point.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise _client_error("Cannot create mount point.", e, 500, server_error=True)

        creds_file: str | None = None
        try:
            if req.share_type == "smb":
                options = ["vers=3.0"]
                if req.username or req.password or req.domain:
                    # Write credentials to a 0600 temp file so they never appear in argv.
                    fd, creds_file = tempfile.mkstemp(prefix="seekr-creds-")
                    lines = []
                    if req.username:
                        lines.append(f"username={req.username}")
                    if req.password:
                        lines.append(f"password={req.password}")
                    if req.domain:
                        lines.append(f"domain={req.domain}")
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        fh.write("\n".join(lines) + "\n")
                    os.chmod(creds_file, 0o600)
                    options.append(f"credentials={creds_file}")
                cmd = ["mount", "-t", "cifs", req.remote_path, str(mount_point), "-o", ",".join(options)]
            else:  # nfs (validated above)
                cmd = ["mount", "-t", "nfs", req.remote_path, str(mount_point)]

            proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
            return {
                "mounted": proc.returncode == 0,
                "exit_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "mount_point": str(mount_point),
            }
        finally:
            if creds_file:
                try:
                    os.remove(creds_file)
                except OSError as e:
                    log.warning("Could not remove mount credentials file %s: %s", creds_file, e)
```

- [ ] **Step 6: Validate `req.path` on unmount**

In `document_search/app.py`, replace the `api_path_unmount` body (lines 1201–1212):

```python
    @app.post("/api/paths/unmount")
    def api_path_unmount(req: PathTestRequest, x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        if os.name == "nt":
            raise HTTPException(status_code=400, detail="Unmount not supported on Windows")
        path = (req.path or "").strip()
        if not path or path.startswith("-") or "\x00" in path:
            raise HTTPException(status_code=400, detail="Invalid path")
        proc = subprocess.run(["umount", path], capture_output=True, text=True, check=False, timeout=30)
        return {
            "unmounted": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
```

- [ ] **Step 7: Validate `req.path` on the path-test endpoint**

In `document_search/app.py`, `api_path_test` begins at line 1134; its body starts:

```python
        require_admin(x_auth_token)
        p = Path(req.path)
```

Replace those two lines with:

```python
        require_admin(x_auth_token)
        if "\x00" in (req.path or ""):
            raise HTTPException(status_code=400, detail="Invalid path")
        p = Path(req.path)
```

- [ ] **Step 8: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_security_hardening.py -k mount -v
```

Expected: both mount tests pass.

- [ ] **Step 9: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: green.

- [ ] **Step 10: Commit**

```powershell
git add document_search/app.py tests/test_security_hardening.py
git commit -m "fix(security): pass SMB creds via 0600 file; validate mount paths"
```

---

## Task 6: Add a timeout to the remaining `subprocess.run`

**Files:**
- Modify: `document_search/app.py:900` — the `/api/update/run` `_runner` shell call.
- Test: `tests/test_security_hardening.py` (extend — a static source assertion, since invoking the real update script is out of scope).

A grep of `subprocess.run` across `document_search/` shows every call carries `timeout=` **except** `app.py:900` (the system-update runner) — that is the one gap. (`app.py:848`, `app.py:1192`, `app.py:1206`, `app.py:1380`, and `extractors/legacy_office_extractor.py:14` all already pass `timeout=`.)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_security_hardening.py`:

```python
import inspect


def test_all_subprocess_run_calls_have_timeout():
    """Every subprocess.run(...) in app.py must pass a timeout= kwarg so a hung
    child cannot block a worker thread forever."""
    src = inspect.getsource(appmod)
    # Walk each `subprocess.run(` call and ensure a `timeout=` appears before the
    # matching close paren.
    idx = 0
    offenders = []
    needle = "subprocess.run("
    while True:
        start = src.find(needle, idx)
        if start == -1:
            break
        depth = 0
        i = start + len(needle) - 1  # position of the '('
        end = None
        for j in range(i, len(src)):
            if src[j] == "(":
                depth += 1
            elif src[j] == ")":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        call_text = src[start: (end + 1) if end else len(src)]
        if "timeout=" not in call_text:
            offenders.append(call_text[:80])
        idx = (end + 1) if end else start + len(needle)
    assert not offenders, f"subprocess.run without timeout: {offenders}"
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_security_hardening.py::test_all_subprocess_run_calls_have_timeout -v
```

Expected: FAIL — `app.py:900` has no `timeout=`.

- [ ] **Step 3: Add the timeout**

In `document_search/app.py`, the `_runner` inside `api_run_update` (line 900) is:

```python
        def _runner():
            proc = subprocess.run(["/bin/sh", str(script)], capture_output=True, text=True, check=False)
            _update_job.update({
```

Replace the `subprocess.run` line with a bounded, exception-handling version:

```python
        def _runner():
            try:
                proc = subprocess.run(
                    ["/bin/sh", str(script)],
                    capture_output=True, text=True, check=False, timeout=600,
                )
            except subprocess.TimeoutExpired as e:
                log.warning("Update script timed out after 600s")
                _update_job.update({
                    "status": "error",
                    "exit_code": None,
                    "stdout": (e.stdout or "")[-4000:] if isinstance(e.stdout, str) else "",
                    "stderr": "Update timed out after 600 seconds.",
                })
                return
            _update_job.update({
```

(The existing `_update_job.update({ "status": "done" if ... })` block that follows is unchanged — it now runs only on the non-timeout path.)

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_security_hardening.py::test_all_subprocess_run_calls_have_timeout -v
```

Expected: PASS.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: green.

- [ ] **Step 6: Commit**

```powershell
git add document_search/app.py tests/test_security_hardening.py
git commit -m "fix(security): bound the update-script subprocess with a 600s timeout"
```

---

## Task 7: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full suite cleanly**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: zero failures, zero errors. The new files contribute the tests added across Tasks 1–6 (`tests/test_security_hardening.py`, `tests/test_upload_validation.py`).

- [ ] **Step 2: Confirm no remaining `detail=str(e)` leaks**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_security_hardening.py -v
```

Then grep to be sure no new leaks crept in (expect only intentional, non-exception detail strings):

```powershell
Select-String -Path document_search/app.py -Pattern "detail=str\(e\)"
```

Expected: no matches.

- [ ] **Step 3: Smoke-test upload rejection via TestClient**

```powershell
$env:PYTHONPATH = "."; python -c "
import io, tempfile, pathlib, os
from fastapi.testclient import TestClient
tmp = pathlib.Path(tempfile.mkdtemp())
os.environ['DOCUMENT_SEARCH_UPLOAD_ROOT'] = str(tmp / 'uploads')
from document_search.app import create_app
app = create_app(str(tmp / 'smoke.db'))
with TestClient(app) as c:
    tok = c.post('/api/login', json={'username':'admin','password':'admin'}).json()['token']
    exe = b'MZ' + b'\x00'*64
    r = c.post('/api/upload', headers={'X-Auth-Token': tok},
               files={'file': ('x.pdf', io.BytesIO(exe), 'application/octet-stream')},
               data={'target_subpath': ''})
    print('exe-as-pdf status =', r.status_code)
    assert r.status_code == 400
    r2 = c.post('/api/upload', headers={'X-Auth-Token': tok},
                files={'file': ('note.txt', io.BytesIO(b'real text\n'), 'text/plain')},
                data={'target_subpath': ''})
    print('text status =', r2.status_code)
    assert r2.status_code == 200
print('OK')
"
```

Expected: prints `exe-as-pdf status = 400`, `text status = 200`, then `OK`.

- [ ] **Step 4: Confirm the Dockerfile / lockfile invariants**

```powershell
Select-String -Path Dockerfile -Pattern "USER seekr"
Test-Path requirements.lock
```

Expected: a match for `USER seekr`; `requirements.lock` → `True`.

- [ ] **Step 5: No commit (verification only)**

---

## Definition of Done

- [ ] `pytest -q` is green on a clean checkout, including the new `tests/test_security_hardening.py` and `tests/test_upload_validation.py`.
- [ ] No `raise HTTPException(detail=str(e))` (or `detail=f"...{e}"` echoing the raw exception) remains in `document_search/app.py`; the two former leaks (`app.py:1098`, `app.py:1329`) now return generic messages and log the detail via the `document_search.app` logger.
- [ ] `Dockerfile` creates the `seekr` user (UID/GID 10001), `chown`s `/data`, `/documents`, `/app`, and switches with `USER seekr` before `CMD`; `docker-compose.yml` pins `user: "10001:10001"` and documents the opt-in `SYS_ADMIN` capability for the mount feature.
- [ ] `/api/upload` rejects: (a) a binary (`.exe`) renamed to an allowed extension via magic-byte sniffing, and (b) a `../` traversal `target_subpath`; it still accepts a genuine `.txt`/`.pdf`.
- [ ] `cryptography`, `PyYAML`, `pillow`, `jinja2`, `python-multipart` carry upper bounds in `requirements.txt`; `requirements.lock` exists and pins them with `==`.
- [ ] `/api/paths/mount` passes SMB credentials via a `0600` `credentials=` file (never in argv); the credentials file is deleted in a `finally`; `remote_path` is validated; `req.path` is validated on mount/unmount/test.
- [ ] Every `subprocess.run(...)` in `document_search/app.py` carries a `timeout=` (verified by `test_all_subprocess_run_calls_have_timeout`).
- [ ] Smoke test (Task 7 Step 3) passes.

---

## Notes for the executing agent

- **Why `_client_error` instead of a global exception handler:** a Starlette exception handler would centralise this, but the two leak sites are inside narrow `try/except Exception` blocks that already choose their status codes (400 vs 500). A tiny helper keeps the diff local and obvious, and lets each call pick its own public string. A global handler is a reasonable later refactor but is out of scope.
- **Why NOT call `logging.basicConfig()` in `create_app`:** uvicorn configures the root logger; calling `basicConfig` in app code can double-log or clobber the host's format. `logging.getLogger("document_search.app")` inherits uvicorn's handlers in production and is captured by pytest's `caplog` in tests. The full logging rollout (ROADMAP §36) is a separate plan.
- **libmagic availability:** the `Dockerfile` already installs `libmagic1` (line 14), so production has it. On a Windows dev host the `python-magic` binding may not find a DLL — `magic_matches_extension` is written to **fall back to leading-signature bytes** when `import magic` / `magic.from_buffer` raises, so the `.exe`-as-`.pdf` test passes either way. If you want full parity locally, `pip install python-magic-bin` provides the DLL; do not add it to `requirements.txt` (it is Windows-only).
- **Magic policy is deliberately permissive for ZIP-based OOXML:** libmagic frequently reports `.docx`/`.pptx` as `application/zip` (the OOXML container) rather than the specific MIME. The allowlist in `upload_validation.py` includes `application/zip` for those two extensions on purpose; tightening it would reject many legitimate Office files. The signature fallback (`PK\x03\x04`) covers the libmagic-absent case.
- **Mount credentials file ownership:** under `USER seekr` the temp file is created by `tempfile.mkstemp` in the system temp dir, owned by `seekr`, mode `0600`. The `mount` binary reads it (it runs with the granted `SYS_ADMIN` capability). We delete it in `finally` even on subprocess timeout. If the host disables in-app mounting (the common case), this path is never exercised.
- **Remote-path validation is defence-in-depth, not the only guard:** `subprocess.run` is always called with an argv **list**, so there is no shell to interpret metacharacters. The validator exists to block `mount` *option injection* (a `remote_path` like `-o something`) and obviously-malformed input, and to give the operator a clear 400 instead of a confusing mount error.
- **Why UID/GID 10001:** picking a fixed, high, unprivileged ID makes host-side `chown -R 10001:10001 ./data` deterministic across rebuilds. Any free UID works; 10001 avoids collisions with distro system users.
- **The `/api/update/run` timeout is 600 s** to match the worker-queue convention (the job-queue plan caps retry backoff at 600 s) and because a full container rebuild + restart can legitimately take minutes. On `TimeoutExpired` the job is marked `error` so the UI doesn't spin on a hung `running` state forever.
- **Do not weaken any test to make it pass.** If `test_security_sensitive_deps_are_bounded` fails after editing `requirements.txt`, fix the constraint, not the test. If a magic test fails on a host without libmagic, confirm the signature fallback is reached — don't relax the assertion.
</content>
</invoke>
