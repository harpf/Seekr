# Structured Logging & In-Memory State Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `document_search/app.py` (~1647 lines, currently ZERO logging) a consistent `logging` setup honouring a `DOCUMENT_SEARCH_LOG_LEVEL` env var, replace every silent `except Exception: pass` with a `log.warning(...)` / `log.exception(...)` call that preserves the existing control flow, and add bounded periodic eviction to the unbounded in-memory `sessions` and `_login_failures` (rate-limit) dicts so they cannot grow forever.

**Architecture:** A new tiny module `document_search/logging_config.py` exposes `configure_logging()` — an idempotent function that reads `DOCUMENT_SEARCH_LOG_LEVEL` and calls `logging.basicConfig(...)` once with the same format string `job_worker.py` / `main.py` already use. `create_app()` calls it at the top so every uvicorn-hosted run is configured. `app.py` gets a module-level `log = logging.getLogger(__name__)`; each silent `except` block keeps its existing fallback (`return []`, `pass`, default value) but logs the swallowed diagnostic first. Session and rate-limit eviction is a pure helper (`_evict_expired`) called opportunistically on each login and each `require_user`/`require_admin` check, plus a hard cap so a flood of tokens can't exhaust memory. No behaviour visible to a correctly-authenticated user changes — expired/stale entries that were already treated as invalid are simply pruned from the dict.

**Tech Stack:** Python 3.11, stdlib `logging`, FastAPI, pytest (`caplog` fixture for log assertions). No new third-party dependencies.

**Scope boundaries (out of scope for this plan, picked up by later plans):**
- **Subprocess hardening** (sandboxing `git`, `nvidia-smi`, `mount`, validating argv) is owned by the security plan. This plan only adds `log.*` calls around those `except` blocks and notes where a `timeout=` is already present vs. missing. Where a blocking subprocess in a worker/handler context lacks a `timeout=`, this plan adds the `timeout=` (state-hygiene: don't wedge a thread) but does NOT change the command, the cwd, or argv validation.
- **Persistent session / rate-limit store** (Redis or a DB table for multi-replica) is a P3 item. This plan keeps the in-memory dicts but makes them self-pruning and bounded.
- **Per-request structured/JSON log records, request IDs, access logging.** Out of scope — a sane human-readable format only.
- **Error-message sanitisation** (not leaking `str(exc)` to HTTP clients) is a separate P0 item, untouched here.

---

## File Structure

**Create:**
- `document_search/logging_config.py` — `configure_logging()` idempotent setup honouring `DOCUMENT_SEARCH_LOG_LEVEL`.
- `tests/test_logging_config.py` — unit tests for the logging setup.
- `tests/test_app_logging.py` — `caplog`-based tests proving silent blocks now log; eviction tests.

**Modify:**
- `document_search/app.py`:
  - add `import logging` + module-level `log = logging.getLogger(__name__)` (top of file, ~line 12).
  - call `configure_logging()` at the top of `create_app` (~line 248).
  - replace the silent `except` blocks at lines **413**, **421**, **750**, **852**, **1154**, **1392**, **1410** with logging-while-preserving-flow.
  - add `_SESSION_TTL_S` constant + `_evict_expired_sessions(...)` and `_evict_expired_failures(...)` helpers; call them from `api_login`, `require_user`, `require_admin` (~lines 469-513, 569-582).
  - add a `timeout=` to the `nvidia-smi` call only if missing (verify at line 1378 — it already has `timeout=5`, so NO change there; documented in Task 4).

**Untouched:**
- `document_search/main.py` — already calls `logging.basicConfig(...)` in `main()`; leave as-is. `configure_logging()` is idempotent so the CLI path keeps working.
- `document_search/services/job_worker.py`, `ocr_service.py` — already use `logging.getLogger(__name__)`; the new `basicConfig` makes their records visible without edits.
- The `except Exception as e:` blocks that re-raise as `HTTPException` or return a structured `{"error": ...}` (lines 871, 957, 1097, 1175, 1237, 1266, 1328, 1545, 1576) and the per-item loop blocks (lines 1528, 1545) — these already surface the error to the caller and are NOT silent swallows. They are out of scope.

---

## Key design decisions (locked)

- **`configure_logging()` is idempotent and non-clobbering.** It uses `logging.basicConfig(...)`, which is a no-op if the root logger already has handlers (so pytest's own capture and `main.py`'s `basicConfig` are not overridden). It additionally calls `logging.getLogger().setLevel(level)` so the level still reflects `DOCUMENT_SEARCH_LOG_LEVEL` even when handlers already exist.
- **Format string matches the existing codebase** (`main.py:113`, `ocr_service`): `"%(asctime)s %(levelname)s %(name)s %(message)s"`.
- **Default level is `INFO`.** Invalid `DOCUMENT_SEARCH_LOG_LEVEL` values fall back to `INFO` and emit one `warning` about the bad value (does not raise — config must never crash startup).
- **Choice of `log.warning` vs `log.exception`:** blocks that swallow an *expected, recoverable* condition (missing/garbled config file, optional `nvidia-smi`/Ollama not present) use `log.warning(...)`. Blocks where the exception is genuinely *unexpected* (write-test failure on a mount, git subprocess raising) use `log.exception(...)` so the traceback is captured. Control flow is unchanged in every case.
- **Session eviction is opportunistic, not a background thread.** Adding a timer thread is more moving parts than this P0 fix warrants. Pruning on every auth touch is O(n) over the dict but n is bounded (see next point) and auth is not hot enough to matter. A hard cap (`_SESSION_MAX = 10_000`) guards against a token-flood DoS by evicting the oldest entries when exceeded.
- **`_login_failures` is pruned per-IP already** inside `_check_rate_limit` (line 472 rebuilds the list), but the *keys* (IPs) are never removed once added — an attacker rotating IPs grows the dict forever. This plan removes a key when its pruned list becomes empty, and caps the number of tracked IPs.

---

## Task 1: `logging_config.py`

**Files:**
- Create: `document_search/logging_config.py`
- Test: `tests/test_logging_config.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_logging_config.py`:

```python
import logging
import pytest
from document_search.logging_config import configure_logging


@pytest.fixture(autouse=True)
def _reset_root_logger():
    """Snapshot and restore the root logger so tests don't leak handlers/levels."""
    root = logging.getLogger()
    old_handlers = root.handlers[:]
    old_level = root.level
    yield
    root.handlers[:] = old_handlers
    root.setLevel(old_level)


def test_default_level_is_info(monkeypatch):
    monkeypatch.delenv("DOCUMENT_SEARCH_LOG_LEVEL", raising=False)
    logging.getLogger().handlers.clear()
    configure_logging()
    assert logging.getLogger().level == logging.INFO


def test_env_var_sets_level(monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_LOG_LEVEL", "DEBUG")
    logging.getLogger().handlers.clear()
    configure_logging()
    assert logging.getLogger().level == logging.DEBUG


def test_invalid_level_falls_back_to_info_and_warns(monkeypatch, caplog):
    monkeypatch.setenv("DOCUMENT_SEARCH_LOG_LEVEL", "NOTALEVEL")
    logging.getLogger().handlers.clear()
    with caplog.at_level(logging.WARNING):
        configure_logging()
    assert logging.getLogger().level == logging.INFO
    assert any("NOTALEVEL" in r.message for r in caplog.records)


def test_idempotent_does_not_add_duplicate_handlers(monkeypatch):
    monkeypatch.delenv("DOCUMENT_SEARCH_LOG_LEVEL", raising=False)
    logging.getLogger().handlers.clear()
    configure_logging()
    n_after_first = len(logging.getLogger().handlers)
    configure_logging()
    n_after_second = len(logging.getLogger().handlers)
    assert n_after_first == n_after_second
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_logging_config.py -v
```

Expected: ImportError — `document_search.logging_config` does not exist yet.

- [ ] **Step 3: Implement `logging_config.py`**

Create `document_search/logging_config.py`:

```python
"""Central logging configuration for the document_search package.

Call `configure_logging()` once at process entry (FastAPI `create_app`, CLI
`main`). It is idempotent: safe to call multiple times and a no-op for handler
creation if the root logger is already configured (e.g. under pytest or uvicorn).
"""
from __future__ import annotations

import logging
import os

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_DEFAULT_LEVEL = "INFO"


def _resolve_level() -> tuple[int, str | None]:
    """Return (numeric_level, bad_value_or_None).

    Reads DOCUMENT_SEARCH_LOG_LEVEL. Unknown values fall back to INFO and the
    offending string is returned so the caller can warn about it.
    """
    raw = os.getenv("DOCUMENT_SEARCH_LOG_LEVEL", _DEFAULT_LEVEL).strip().upper()
    level = logging.getLevelName(raw)
    if isinstance(level, int):
        return level, None
    return logging.INFO, raw


def configure_logging() -> None:
    """Configure the root logger once, honouring DOCUMENT_SEARCH_LOG_LEVEL.

    - Default level INFO.
    - Invalid level values fall back to INFO and emit a single warning.
    - `basicConfig` only installs a handler if none exists, so we additionally
      force the root level so the env var still applies under pre-existing
      handlers (pytest, uvicorn, the CLI's own basicConfig).
    """
    level, bad_value = _resolve_level()
    logging.basicConfig(level=level, format=_LOG_FORMAT)
    logging.getLogger().setLevel(level)
    if bad_value is not None:
        logging.getLogger(__name__).warning(
            "Invalid DOCUMENT_SEARCH_LOG_LEVEL=%r; falling back to INFO", bad_value
        )
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_logging_config.py -v
```

Expected: 4 passing.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: existing baseline + 4 new, all green.

- [ ] **Step 6: Commit**

```powershell
git add document_search/logging_config.py tests/test_logging_config.py
git commit -m "chore(logging): add configure_logging honouring DOCUMENT_SEARCH_LOG_LEVEL"
```

---

## Task 2: Wire logging into `app.py` and add module logger

**Files:**
- Modify: `document_search/app.py:1-18` (imports + module logger), `document_search/app.py:248-267` (call `configure_logging` in `create_app`)
- Test: `tests/test_app_logging.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_app_logging.py`:

```python
import logging
import pytest

pytest.importorskip("fastapi")


def test_app_module_has_logger():
    from document_search import app as app_mod
    assert isinstance(getattr(app_mod, "log", None), logging.Logger)
    assert app_mod.log.name == "document_search.app"


def test_create_app_configures_logging(tmp_path, monkeypatch):
    """Creating the app must call configure_logging (root level reflects env)."""
    monkeypatch.setenv("DOCUMENT_SEARCH_LOG_LEVEL", "WARNING")
    # Clear handlers so basicConfig + setLevel take effect deterministically.
    logging.getLogger().handlers.clear()
    from document_search.app import create_app
    create_app(str(tmp_path / "t.db"))
    assert logging.getLogger().level == logging.WARNING
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_logging.py -v
```

Expected: `test_app_module_has_logger` fails (`app_mod.log` is `None`); `test_create_app_configures_logging` fails (root level not forced).

- [ ] **Step 3: Add the import + module logger**

In `document_search/app.py`, the imports currently start (lines 1-18):

```python
from __future__ import annotations
import json
import datetime as dt
import hashlib
import html
import ipaddress
import os
import posixpath
import re
import secrets
import sqlite3
import threading
import uuid
import time
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
```

Add `logging` to the import block and a module-level logger. Change the line:

```python
import ipaddress
import os
```

to:

```python
import ipaddress
import logging
import os
```

Then, immediately **after** the `from document_search.services.file_service import fingerprint` import (line 46) and before the `# Singletons` comment (line 49), add:

```python
from document_search.logging_config import configure_logging

log = logging.getLogger(__name__)
```

- [ ] **Step 4: Call `configure_logging()` in `create_app`**

In `document_search/app.py`, `create_app` begins at line 248:

```python
def create_app(db_path: str = "./document_index.db") -> FastAPI:
    config_path = Path(os.getenv("DOCUMENT_SEARCH_CONFIG_PATH", "./config.json"))
```

Insert the configure call as the very first statement of the function body:

```python
def create_app(db_path: str = "./document_index.db") -> FastAPI:
    configure_logging()
    config_path = Path(os.getenv("DOCUMENT_SEARCH_CONFIG_PATH", "./config.json"))
```

- [ ] **Step 5: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_logging.py -v
```

Expected: 2 passing.

- [ ] **Step 6: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: previous total + 2 new, all green.

- [ ] **Step 7: Commit**

```powershell
git add document_search/app.py tests/test_app_logging.py
git commit -m "chore(logging): configure logging in create_app and add module logger"
```

---

## Task 3: Replace silent config-load `except` blocks with logging

**Files:**
- Modify: `document_search/app.py:411-414` (`_load_ha_keys`), `document_search/app.py:416-423` (`_save_ha_keys`), `document_search/app.py:746-751` (`api_source_folders`)
- Test: `tests/test_app_logging.py` (extend)

These three blocks swallow JSON-decode / read failures of `config.json` and silently return defaults. After this task they log a warning first, then keep the exact same fallback.

- [ ] **Step 1: Write the failing test (caplog)**

Append to `tests/test_app_logging.py`:

```python
def test_load_ha_keys_logs_warning_on_malformed_config(tmp_path, monkeypatch, caplog):
    """A corrupt config.json must log a warning, not silently return []."""
    bad_config = tmp_path / "config.json"
    bad_config.write_text("{ this is not valid json", encoding="utf-8")
    monkeypatch.setenv("DOCUMENT_SEARCH_CONFIG_PATH", str(bad_config))

    from document_search.app import create_app
    from fastapi.testclient import TestClient

    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        r = client.post("/api/login", json={"username": "admin", "password": "admin"})
        token = r.json()["token"]
        with caplog.at_level(logging.WARNING, logger="document_search.app"):
            # /api/source-folders triggers the same malformed-config read path
            resp = client.get("/api/source-folders", headers={"X-Auth-Token": token})
        assert resp.status_code == 200          # fallback preserved
        assert resp.json() == []                # empty, as before
    assert any(
        "config" in r.message.lower() for r in caplog.records
    ), f"expected a config-parse warning, got: {[r.message for r in caplog.records]}"
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_logging.py::test_load_ha_keys_logs_warning_on_malformed_config -v
```

Expected: FAIL — no warning is emitted (the block is silent).

- [ ] **Step 3: Patch `_load_ha_keys` (lines 411-414)**

Current:

```python
        try:
            return json.loads(config_path.read_text(encoding="utf-8")).get("ha_api_keys", [])
        except Exception:
            return []
```

Replace with:

```python
        try:
            return json.loads(config_path.read_text(encoding="utf-8")).get("ha_api_keys", [])
        except Exception:
            log.warning("Failed to parse HA keys from config %s; treating as empty", config_path, exc_info=True)
            return []
```

- [ ] **Step 4: Patch `_save_ha_keys` (lines 419-422)**

Current:

```python
        if config_path.exists():
            try:
                raw = json.loads(config_path.read_text(encoding="utf-8"))
            except Exception:
                pass
```

Replace with:

```python
        if config_path.exists():
            try:
                raw = json.loads(config_path.read_text(encoding="utf-8"))
            except Exception:
                log.warning("Existing config %s is unreadable; overwriting with fresh keys", config_path, exc_info=True)
```

(Removing the `pass` is intentional — `log.warning(...)` is now the body of the `except`. `raw` stays `{}` exactly as before.)

- [ ] **Step 5: Patch `api_source_folders` (lines 746-751)**

Current:

```python
        if config_path.exists():
            try:
                raw_source_paths = json.loads(config_path.read_text(encoding="utf-8")).get("source_paths", [])
                if not isinstance(raw_source_paths, list):
                    raw_source_paths = []
            except Exception:
                pass
```

Replace with:

```python
        if config_path.exists():
            try:
                raw_source_paths = json.loads(config_path.read_text(encoding="utf-8")).get("source_paths", [])
                if not isinstance(raw_source_paths, list):
                    raw_source_paths = []
            except Exception:
                log.warning("Failed to read source_paths from config %s; returning none", config_path, exc_info=True)
```

- [ ] **Step 6: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_logging.py -v
```

Expected: 3 passing (2 from Task 2 + 1 new).

- [ ] **Step 7: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: still green.

- [ ] **Step 8: Commit**

```powershell
git add document_search/app.py tests/test_app_logging.py
git commit -m "fix(app): log warnings on malformed config instead of swallowing"
```

---

## Task 4: Replace silent subprocess / optional-tool `except` blocks with logging

**Files:**
- Modify: `document_search/app.py:844-853` (git `rev-parse`), `document_search/app.py:1377-1393` (`nvidia-smi`), `document_search/app.py:1399-1411` (Ollama `/api/tags`), `document_search/app.py:1149-1155` (mount-point write-test)
- Test: `tests/test_app_logging.py` (extend)

These swallow failures of optional/external tools. The git and write-test blocks are *unexpected* failures → `log.exception`. The `nvidia-smi` and Ollama blocks are *expected when the tool is absent* → `log.warning`. **Control flow is unchanged: each block keeps its existing fallback value.**

> **Cross-reference (subprocess hardening — security plan owns this):** the `git rev-parse` call (line 845) and `nvidia-smi` call (line 1378) already pass `timeout=10` / `timeout=5`. **No `timeout=` is added or changed here** — both already have one, so the "add timeouts where subprocess blocks worker threads" item from the roadmap is already satisfied for these two call sites. If a future audit finds a subprocess call in a handler/worker thread WITHOUT a timeout, adding one belongs in this plan's spirit; none of the four sites in this task lack one. Argv/cwd hardening stays with the security plan.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_app_logging.py`:

```python
def test_nvidia_smi_absence_logs_warning(tmp_path, monkeypatch, caplog):
    """When nvidia-smi raises (not installed), the block must log a warning and
    still return gpu_info=None — i.e. the system endpoint must not 500."""
    import subprocess as _sp
    from document_search.app import create_app
    from fastapi.testclient import TestClient

    def _boom(*args, **kwargs):
        raise FileNotFoundError("nvidia-smi not found")

    monkeypatch.setattr(_sp, "run", _boom)

    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = client.post(
            "/api/login", json={"username": "admin", "password": "admin"}
        ).json()["token"]
        with caplog.at_level(logging.WARNING, logger="document_search.app"):
            r = client.get("/api/ai/system-info", headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        assert r.json().get("gpu") is None
    assert any(
        "nvidia" in rec.message.lower() or "gpu" in rec.message.lower()
        for rec in caplog.records
    ), f"expected a GPU warning, got: {[rec.message for rec in caplog.records]}"
```

> **Verified:** the `gpu_info` block under test is the `try`/`except` at `app.py:1377`, inside `@app.get("/api/ai/system-info")` (line 1361, auth = `require_user`). The route serialises it as `"gpu": gpu_info` (line 1428), so on a `nvidia-smi` failure the response key `"gpu"` is `None`. The assertion only needs the warning to be emitted.

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_logging.py::test_nvidia_smi_absence_logs_warning -v
```

Expected: FAIL — no warning emitted.

- [ ] **Step 3: Patch the git `rev-parse` block (lines 850-853)**

Current:

```python
            if proc.returncode == 0:
                current_commit = proc.stdout.strip()
        except Exception:
            pass
        if not current_commit:
            current_commit = os.getenv("GIT_COMMIT")
```

Replace the `except` with:

```python
            if proc.returncode == 0:
                current_commit = proc.stdout.strip()
        except Exception:
            log.exception("git rev-parse HEAD failed; falling back to GIT_COMMIT env var")
        if not current_commit:
            current_commit = os.getenv("GIT_COMMIT")
```

- [ ] **Step 4: Patch the `nvidia-smi` block (lines 1392-1393)**

Current:

```python
        except Exception:
            pass

        # Models from Ollama with sizes
```

Replace with:

```python
        except Exception:
            log.warning("nvidia-smi unavailable or failed; reporting no GPU info", exc_info=True)

        # Models from Ollama with sizes
```

- [ ] **Step 5: Patch the Ollama `/api/tags` block (lines 1410-1411)**

Current:

```python
        except Exception:
            pass

        # Tier recommendation + fit label per model
```

Replace with:

```python
        except Exception:
            log.warning("Could not list models from Ollama at %s; returning empty list", organizer.base_url, exc_info=True)

        # Tier recommendation + fit label per model
```

- [ ] **Step 6: Patch the mount-point write-test block (lines 1154-1155)**

Current:

```python
            try:
                test_file = p / f".seekr_write_test_{uuid.uuid4().hex[:6]}"
                test_file.touch()
                test_file.unlink()
                writable = True
            except Exception:
                pass
```

Replace with:

```python
            try:
                test_file = p / f".seekr_write_test_{uuid.uuid4().hex[:6]}"
                test_file.touch()
                test_file.unlink()
                writable = True
            except Exception:
                log.warning("Write test failed for path %s; marking not writable", p, exc_info=True)
```

- [ ] **Step 7: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_logging.py -v
```

Expected: 4 passing (3 prior + 1 new).

- [ ] **Step 8: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: still green.

- [ ] **Step 9: Commit**

```powershell
git add document_search/app.py tests/test_app_logging.py
git commit -m "fix(app): log subprocess and optional-tool failures instead of swallowing"
```

---

## Task 5: Bounded eviction for the in-memory `sessions` dict

**Files:**
- Modify: `document_search/app.py:69-71` (constants), `document_search/app.py:263` (`sessions` decl), `document_search/app.py:495-513` (`require_user`/`require_admin`), `document_search/app.py:569-582` (`api_login`)
- Test: `tests/test_app_logging.py` (extend)

The `sessions` dict (line 263) only ever has entries *popped* when a specific expired token is touched (lines 500, 509). Tokens that are never touched again live forever. This task adds a single source of truth for the 8-hour TTL and an eviction helper that prunes all expired tokens, plus a hard cap.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_app_logging.py`:

```python
def test_expired_sessions_are_evicted_on_login(tmp_path, monkeypatch):
    """Logging in prunes other expired tokens from the in-memory sessions dict."""
    import time as _time
    from fastapi.testclient import TestClient
    from document_search.app import create_app

    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        # First login → token A
        a = client.post("/api/login", json={"username": "admin", "password": "admin"}).json()["token"]
        sessions = app.state.sessions  # exposed for testability (added in Step 3)
        assert a in sessions
        # Backdate token A's issued-at to 9 hours ago (TTL is 8h) → expired
        user_id, _issued, role = sessions[a]
        sessions[a] = (user_id, _time.time() - 9 * 3600, role)
        # Second login → token B; eviction during login must drop the stale A
        b = client.post("/api/login", json={"username": "admin", "password": "admin"}).json()["token"]
        assert b in sessions
        assert a not in sessions, "expired session A should have been evicted"


def test_sessions_hard_cap_evicts_oldest(tmp_path):
    import time as _time
    from fastapi.testclient import TestClient
    from document_search.app import create_app
    from document_search.app import _SESSION_MAX

    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        sessions = app.state.sessions
        # Fill past the cap with synthetic, non-expired entries
        now = _time.time()
        for i in range(_SESSION_MAX + 5):
            sessions[f"tok{i}"] = (1, now + i, "user")  # ascending issued-at
        # A real login triggers the cap check
        client.post("/api/login", json={"username": "admin", "password": "admin"})
        assert len(sessions) <= _SESSION_MAX + 1  # +1 for the just-issued real token
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_logging.py::test_expired_sessions_are_evicted_on_login tests/test_app_logging.py::test_sessions_hard_cap_evicts_oldest -v
```

Expected: FAIL — `app.state.sessions` and `_SESSION_MAX` don't exist; no eviction happens.

- [ ] **Step 3: Add the TTL constant and module-level cap (lines 69-71 area)**

In `document_search/app.py`, the rate-limit constants are at lines 70-71:

```python
_RATE_LIMIT_MAX = 10       # max failures
_RATE_LIMIT_WINDOW = 300   # seconds (5 min)
```

Immediately **after** them, add the session-hygiene constants:

```python
_SESSION_TTL_S = 60 * 60 * 8     # 8 hours — single source of truth for session lifetime
_SESSION_MAX = 10_000            # hard cap on tracked sessions (DoS guard)
_FAILURE_IP_MAX = 10_000         # hard cap on tracked rate-limit IPs (DoS guard)
```

- [ ] **Step 4: Expose `sessions` on `app.state` and add the eviction helper**

In `create_app`, the dict is declared at line 263:

```python
    sessions: dict[str, tuple[int, float, str]] = {}
```

Immediately **after** it, expose it for testability:

```python
    sessions: dict[str, tuple[int, float, str]] = {}
    app.state.sessions = sessions
```

Then, immediately **after** the `_check_rate_limit` / `_record_failure` / `_clear_failures` helpers (after line 481, before `_validate_username`), add the eviction helper:

```python
    def _evict_expired_sessions() -> None:
        """Prune expired sessions and enforce the hard cap.

        Called opportunistically on each login and auth check. O(n) over the
        sessions dict, but n is bounded by `_SESSION_MAX`.
        """
        now = time.time()
        expired = [t for t, (_uid, issued, _role) in sessions.items() if now - issued > _SESSION_TTL_S]
        for t in expired:
            sessions.pop(t, None)
        if expired:
            log.info("Evicted %d expired session(s)", len(expired))
        if len(sessions) > _SESSION_MAX:
            # Drop the oldest sessions (smallest issued-at) down to the cap.
            overflow = len(sessions) - _SESSION_MAX
            oldest = sorted(sessions.items(), key=lambda kv: kv[1][1])[:overflow]
            for t, _ in oldest:
                sessions.pop(t, None)
            log.warning("Session cap %d exceeded; evicted %d oldest session(s)", _SESSION_MAX, overflow)
```

- [ ] **Step 5: Call eviction from `require_user`, `require_admin`, and `api_login`; use the TTL constant**

Replace `require_user` and `require_admin` (lines 495-513). Current `require_user`:

```python
    def require_user(token: str | None) -> int:
        if not token or token not in sessions:
            raise HTTPException(status_code=401, detail="Unauthorized")
        user_id, issued, _role = sessions[token]
        if time.time() - issued > 60 * 60 * 8:
            sessions.pop(token, None)
            raise HTTPException(status_code=401, detail="Session expired")
        return user_id
```

Replace with:

```python
    def require_user(token: str | None) -> int:
        _evict_expired_sessions()
        if not token or token not in sessions:
            raise HTTPException(status_code=401, detail="Unauthorized")
        user_id, issued, _role = sessions[token]
        if time.time() - issued > _SESSION_TTL_S:
            sessions.pop(token, None)
            raise HTTPException(status_code=401, detail="Session expired")
        return user_id
```

Current `require_admin`:

```python
    def require_admin(token: str | None) -> int:
        if not token or token not in sessions:
            raise HTTPException(status_code=401, detail="Unauthorized")
        user_id, issued, role = sessions[token]
        if time.time() - issued > 60 * 60 * 8:
            sessions.pop(token, None)
            raise HTTPException(status_code=401, detail="Session expired")
        if role != "admin":
            raise HTTPException(status_code=403, detail="Admin role required")
        return user_id
```

Replace with:

```python
    def require_admin(token: str | None) -> int:
        _evict_expired_sessions()
        if not token or token not in sessions:
            raise HTTPException(status_code=401, detail="Unauthorized")
        user_id, issued, role = sessions[token]
        if time.time() - issued > _SESSION_TTL_S:
            sessions.pop(token, None)
            raise HTTPException(status_code=401, detail="Session expired")
        if role != "admin":
            raise HTTPException(status_code=403, detail="Admin role required")
        return user_id
```

In `api_login` (lines 569-582), call eviction right after the rate-limit check. Current:

```python
    @app.post("/api/login")
    def api_login(req: LoginRequest, request: Request):
        ip = request.client.host if request.client else "unknown"
        _check_rate_limit(ip)
        db = store()
```

Replace with:

```python
    @app.post("/api/login")
    def api_login(req: LoginRequest, request: Request):
        ip = request.client.host if request.client else "unknown"
        _check_rate_limit(ip)
        _evict_expired_sessions()
        db = store()
```

> **Note:** `api_me` (line 586) also reads `sessions` inline with the literal `60 * 60 * 8`. Leave its expiry comparison as-is for this task to keep the diff focused on eviction — OR, if you prefer consistency, replace the literal with `_SESSION_TTL_S` there too (a one-token change, no behaviour difference). The tests do not depend on it.

- [ ] **Step 6: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_logging.py -v
```

Expected: 6 passing (4 prior + 2 new).

- [ ] **Step 7: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: still green — `_SESSION_TTL_S` equals the previous literal, so no auth behaviour changes.

- [ ] **Step 8: Commit**

```powershell
git add document_search/app.py tests/test_app_logging.py
git commit -m "fix(app): evict expired sessions and cap session dict size"
```

---

## Task 6: Bounded eviction for the `_login_failures` (rate-limit) dict

**Files:**
- Modify: `document_search/app.py:470-481` (`_check_rate_limit` / `_record_failure`)
- Test: `tests/test_app_logging.py` (extend)

`_check_rate_limit` already prunes the *per-IP list* (line 472) but never removes an IP key once its list goes empty, so a rotating-IP attacker grows the dict forever. This task drops empty keys and caps the number of tracked IPs.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_app_logging.py`:

```python
def test_rate_limit_dict_drops_empty_ip_keys(tmp_path, monkeypatch):
    """An IP whose failures have all aged out must be removed from the tracker."""
    import time as _time
    from fastapi.testclient import TestClient
    from document_search.app import create_app, _login_failures, _RATE_LIMIT_WINDOW

    _login_failures.clear()
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        # Seed an aged-out failure for a fake IP
        old = _time.time() - (_RATE_LIMIT_WINDOW + 10)
        _login_failures["10.0.0.99"] = [old]
        # Any login attempt runs _check_rate_limit for the *real* client IP, but
        # the eviction sweep must also drop the aged-out fake IP.
        client.post("/api/login", json={"username": "admin", "password": "wrong"})
        assert "10.0.0.99" not in _login_failures, "aged-out IP should be evicted"
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_logging.py::test_rate_limit_dict_drops_empty_ip_keys -v
```

Expected: FAIL — the fake IP key persists.

- [ ] **Step 3: Patch `_check_rate_limit` and `_record_failure` (lines 470-481)**

Current:

```python
    def _check_rate_limit(ip: str) -> None:
        now = time.time()
        recent = [t for t in _login_failures.get(ip, []) if now - t < _RATE_LIMIT_WINDOW]
        _login_failures[ip] = recent
        if len(recent) >= _RATE_LIMIT_MAX:
            raise HTTPException(status_code=429, detail="Too many failed login attempts. Try again in 5 minutes.")

    def _record_failure(ip: str) -> None:
        _login_failures.setdefault(ip, []).append(time.time())

    def _clear_failures(ip: str) -> None:
        _login_failures.pop(ip, None)
```

Replace with:

```python
    def _evict_stale_failures(now: float) -> None:
        """Drop IP keys whose failures have all aged out, and enforce a cap."""
        stale = [
            ip for ip, ts in _login_failures.items()
            if not any(now - t < _RATE_LIMIT_WINDOW for t in ts)
        ]
        for ip in stale:
            _login_failures.pop(ip, None)
        if len(_login_failures) > _FAILURE_IP_MAX:
            overflow = len(_login_failures) - _FAILURE_IP_MAX
            # Evict the IPs with the oldest most-recent failure first.
            oldest = sorted(_login_failures.items(), key=lambda kv: max(kv[1]))[:overflow]
            for ip, _ in oldest:
                _login_failures.pop(ip, None)
            log.warning("Rate-limit IP cap %d exceeded; evicted %d oldest IP(s)", _FAILURE_IP_MAX, overflow)

    def _check_rate_limit(ip: str) -> None:
        now = time.time()
        _evict_stale_failures(now)
        recent = [t for t in _login_failures.get(ip, []) if now - t < _RATE_LIMIT_WINDOW]
        if recent:
            _login_failures[ip] = recent
        else:
            _login_failures.pop(ip, None)
        if len(recent) >= _RATE_LIMIT_MAX:
            raise HTTPException(status_code=429, detail="Too many failed login attempts. Try again in 5 minutes.")

    def _record_failure(ip: str) -> None:
        _login_failures.setdefault(ip, []).append(time.time())

    def _clear_failures(ip: str) -> None:
        _login_failures.pop(ip, None)
```

> **Behaviour preserved:** when `ip` itself has recent failures, its list is kept exactly as before; only empty/aged-out keys are removed. The `>= _RATE_LIMIT_MAX` check uses the freshly-pruned `recent` list, identical to today.

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_logging.py::test_rate_limit_dict_drops_empty_ip_keys -v
```

Expected: PASS.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: all green. In particular the existing rate-limit tests (search `tests/` for `_check_rate_limit` / `429` / `rate`) must still pass — the threshold logic is unchanged.

- [ ] **Step 6: Commit**

```powershell
git add document_search/app.py tests/test_app_logging.py
git commit -m "fix(app): evict aged-out rate-limit IPs and cap tracker size"
```

---

## Task 7: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Full suite cleanly**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: all tests pass, zero failures, no flaky reruns.

- [ ] **Step 2: Confirm no remaining silent swallows in the targeted set**

```powershell
$env:PYTHONPATH = "."; python -c "import pathlib,re; src=pathlib.Path('document_search/app.py').read_text(encoding='utf-8').splitlines(); bad=[i+1 for i,l in enumerate(src) if l.strip()=='except Exception:' and i+1<len(src) and src[i+1].strip()=='pass']; print('silent except-pass at lines:', bad)"
```

Expected: prints `silent except-pass at lines: []` (the four `except Exception:` blocks that previously ended in a bare `pass` — lines 421, 750, 1154, 1392 — now log). Lines whose `except` returns a value / re-raises `HTTPException` are intentionally out of scope and are not `except Exception:` + `pass`, so they won't appear.

- [ ] **Step 3: Smoke-test logging output and env var**

```powershell
$env:PYTHONPATH = "."; $env:DOCUMENT_SEARCH_LOG_LEVEL = "DEBUG"; python -c "
from fastapi.testclient import TestClient
from document_search.app import create_app
import tempfile, pathlib
tmp = pathlib.Path(tempfile.mkdtemp())
app = create_app(str(tmp / 'smoke.db'))
import logging
assert logging.getLogger().level == logging.DEBUG, logging.getLogger().level
with TestClient(app) as c:
    tok = c.post('/api/login', json={'username':'admin','password':'admin'}).json()['token']
    r = c.get('/api/source-folders', headers={'X-Auth-Token': tok})
    assert r.status_code == 200, r.text
print('OK level=DEBUG, source-folders 200')
"
Remove-Item Env:\DOCUMENT_SEARCH_LOG_LEVEL
```

Expected: prints `OK level=DEBUG, source-folders 200`. Log lines in the configured format appear on stderr.

- [ ] **Step 4: No commit (verification only).**

---

## Definition of Done

- [ ] `$env:PYTHONPATH = "."; pytest -q` is green on a clean clone.
- [ ] `document_search/logging_config.py` exists; `configure_logging()` honours `DOCUMENT_SEARCH_LOG_LEVEL`, defaults to INFO, is idempotent, and warns (does not raise) on an invalid level.
- [ ] `app.py` has a module-level `log = logging.getLogger(__name__)` and calls `configure_logging()` as the first statement of `create_app`.
- [ ] Every previously-silent `except Exception: pass` / config-swallowing block in `app.py` (lines 413, 421, 750, 852, 1154, 1392, 1410) now emits `log.warning(...)` or `log.exception(...)` while keeping its original fallback value and control flow.
- [ ] The `sessions` dict is pruned of all expired entries on each login and each auth check, and is hard-capped at `_SESSION_MAX`.
- [ ] The `_login_failures` rate-limit dict drops empty/aged-out IP keys and is hard-capped at `_FAILURE_IP_MAX`; the threshold/429 behaviour is unchanged.
- [ ] The single 8-hour session TTL lives in one constant (`_SESSION_TTL_S`), used by `require_user`/`require_admin`.
- [ ] No subprocess `timeout=` was removed; the in-scope call sites (`git rev-parse` line 845, `nvidia-smi` line 1378) already had timeouts and keep them.
- [ ] Smoke test passes; `DOCUMENT_SEARCH_LOG_LEVEL=DEBUG` is reflected on the root logger.

---

## Notes for the executing agent

- **Line numbers will drift as you edit.** Each task adds/removes lines, so blocks below a previous edit shift. Always re-locate a block by its surrounding code (shown verbatim in each step), not by absolute line number. The numbers in this plan are anchored to the state of `app.py` at the time of writing (1647 lines, `app = create_app(...)` on the last line).
- **`configure_logging()` must never crash startup.** It catches no exceptions itself because `logging.basicConfig` and `getLevelName` don't raise on the inputs we feed them; an unknown level string returns the literal back (handled). Do not add a `raise` for bad levels — log and continue.
- **Why `exc_info=True` on warnings:** the config/optional-tool blocks use `log.warning(..., exc_info=True)` rather than `log.exception(...)` so the level stays WARNING (not ERROR) while still capturing the traceback — appropriate for "recoverable, but I want to see why". `log.exception(...)` (level ERROR, traceback) is reserved for the git and write-test blocks where the failure is genuinely unexpected.
- **Eviction is intentionally synchronous, not a timer thread.** A background eviction thread would need its own lifecycle (start/stop) and lock discipline against the request threads. Pruning on auth touch is simpler, correct, and bounded. If profiling ever shows the O(n) sweep is hot (it won't at this scale), a periodic-with-cooldown guard (`if now - _last_evict > 60: ...`) is the smallest next step — but not in this plan.
- **Thread-safety caveat (documented, not fixed here):** `sessions` and `_login_failures` are plain dicts mutated from multiple uvicorn worker threads. The roadmap (P0 item) explicitly defers a persistent/thread-safe backing store to P3. CPython dict operations used here (`pop`, item-set, `setdefault`) are individually atomic under the GIL, so the eviction sweep won't corrupt the dict; it may race a concurrent insert (a token created microseconds before the sweep could be evaluated as "not expired" and kept, which is correct). Do NOT add locking in this plan — it widens scope and the GIL makes the per-op mutations safe enough for the single-replica deployment this targets.
- **Don't touch the out-of-scope `except` blocks.** Lines 871, 957, 1097, 1175, 1237, 1266, 1328, 1545, 1576 already convert the exception into an `HTTPException` or a structured error response — they are not silent. Adding logging there is acceptable polish but is explicitly NOT required by this plan; if you add it, keep it to one extra commit and one `log.warning`, and do not change the response.
- **`main.py` keeps its own `basicConfig`.** The CLI calls `logging.basicConfig(...)` in `main()`; `configure_logging()` is only invoked from `create_app`. The two never fight because `basicConfig` is a no-op once handlers exist, and the CLI path never calls `create_app`. No change to `main.py` is needed.
- **`nvidia-smi` test endpoint is verified:** the GPU block is the `try`/`except` at `app.py:1377`, served by `@app.get("/api/ai/system-info")` (line 1361), which serialises it as `"gpu": gpu_info` (line 1428). The test already targets that path. If line numbers have drifted, grep for `gpu_info` to re-confirm; the warning assertion is endpoint-agnostic.
