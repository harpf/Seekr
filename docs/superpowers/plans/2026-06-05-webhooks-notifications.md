# Outbound Webhooks / Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add outbound webhooks (ROADMAP P4 "Webhooks / notifications") so Seekr can POST a signed JSON payload to an operator-configured URL when an event happens. The first wired event is `index.completed` — fired when an `index_paths` job succeeds. Delivery is bounded-retry by reusing the existing persistent job queue (a new kind `webhook_deliver`), so a transient failure of the receiver retries via the queue's existing exponential backoff. The saved-search "new match" event is described as a follow-up that reuses the exact same delivery path.

**Architecture:** A new `webhooks` table holds subscriptions (`url`, `event_type`, `secret`, `enabled`, `created_by`). A new `webhook_deliveries` table records every delivery attempt for audit/debugging. A new service module `webhook_service.py` is the only module that writes those two tables and owns the signing + HTTP-POST logic (mirroring the `urllib.request` timeout pattern already used in `ai_organizer.py`). Firing an event is a single call — `enqueue_event(...)` — which looks up all enabled subscriptions matching the event type and enqueues one `webhook_deliver` job per subscription. The Worker's `webhook_deliver` handler performs the actual POST; a raise re-enters the queue's retry-with-backoff. The `index_paths` handler in `app.py` calls `enqueue_event("index.completed", ...)` after it finishes successfully.

**Tech Stack:** Python 3.11, SQLite, FastAPI, `urllib.request` (stdlib — no new third-party deps), `hmac` + `hashlib` (stdlib) for signing, pytest. No new dependencies.

**Scope boundaries:**

In scope:
- `webhooks` table + `webhook_deliveries` table (idempotent additive migration in `_init_schema`).
- `webhook_service.py`: CRUD helpers, HMAC signing, signed-POST delivery, `enqueue_event` fan-out.
- Admin CRUD endpoints `/api/webhooks` (GET list, POST create), `/api/webhooks/{id}` (DELETE), `/api/webhooks/{id}/deliveries` (GET audit) — all admin-only.
- A `webhook_deliver` Worker handler registered at app startup, with `max_retries=5`.
- Wiring the first event: `index.completed` fired when an `index_paths` job succeeds.
- SSRF guard: target URLs validated (scheme allow-list, no credentials, blocked private/loopback/link-local hosts) at create time and re-checked at delivery time.
- Tests: webhook CRUD (admin-only — non-admin 403); delivery POSTs a correctly-signed payload (HTTP client mocked / request captured, HMAC verified); a completed index job enqueues a `webhook_deliver` job. No real network in any test.

Out of scope (deferred):
- Saved-search "new match" event (`search.new_match`) — the table column `event_type` and `enqueue_event` already support it; only the *fire site* is deferred to the saved-search plan. Described at the end of Task 6 as a follow-up.
- Per-subscription custom headers, payload templating, or event filtering beyond `event_type`.
- Webhook delivery rate-limiting / circuit-breaking beyond the queue's existing backoff.
- A UI to manage webhooks (admin API only here).
- Outbound proxy support.

---

## File Structure

**Create:**
- `document_search/services/webhook_service.py` — DB layer for `webhooks` + `webhook_deliveries`, signing, delivery, `enqueue_event`.
- `tests/test_webhook_service.py` — unit tests for CRUD, signing, SSRF guard, delivery (mocked HTTP).
- `tests/test_app_webhooks.py` — integration tests for the admin endpoints + the `index.completed` fan-out.

**Modify:**
- `document_search/index/sqlite_store.py` — add `webhooks` + `webhook_deliveries` tables to the `executescript` block in `_init_schema`.
- `document_search/app.py` — instantiate `WebhookService`; register the `webhook_deliver` handler; fire `index.completed` from the `index_paths` handler; add the `/api/webhooks/*` admin routes; add the `WebhookCreateRequest` model.

**Untouched:**
- `JobStore` / `Worker` — reused unchanged. Webhook delivery is just another job kind.
- `ai_organizer.py` — read for the `urllib.request` pattern; not modified.

---

## Key design decisions (locked)

- **Why reuse the job queue for delivery instead of POSTing inline:** the receiver may be slow or briefly down. Enqueuing a `webhook_deliver` job means the existing `JobStore.mark_failed` backoff (`min(2**attempt, 600)` s, capped at 10 min) handles retries for free, deliveries survive a process restart (they're persisted as `pending` jobs), and the firing code path (`index_paths` handler) never blocks on network I/O.
- **One job per subscription, not per event:** `enqueue_event` fans out — if three webhooks subscribe to `index.completed`, three `webhook_deliver` jobs are enqueued. Each retries independently. A `webhook_deliveries` audit row is written on every attempt (success or failure) keyed by `(webhook_id, job_id)`.
- **Signing:** `X-Seekr-Signature: sha256=<hex>` where `<hex> = HMAC-SHA256(secret, raw_body_bytes)`. The receiver recomputes it over the exact bytes we sent. We also send `X-Seekr-Event` (the event type) and `X-Seekr-Delivery` (the delivery job id) headers. The body is `json.dumps(payload, separators=(",", ":"), sort_keys=True)` so the signed bytes are deterministic.
- **Secret generation:** if the admin doesn't supply a secret on create, we generate `secrets.token_hex(32)`. The secret is stored plaintext in SQLite (same trust model as the existing HA API keys stored in `config.json`). It is returned **once** on create and never again by the list endpoint.
- **`webhook_deliver` payload shape (the job payload, NOT the HTTP body):**
  `{"webhook_id": int, "url": str, "secret": str, "event_type": str, "event": {...}}`.
  The `event` sub-dict is what gets signed + POSTed as the HTTP body, wrapped as `{"event": event_type, "delivered_at": iso, "data": event}`.
- **SSRF guard is security-critical and runs twice:** at create time (reject bad URLs before they're stored) and again immediately before the POST in the delivery handler (defends against TOCTOU / DNS-rebind on stored rows and against rows inserted by a future bug). The guard rejects non-`http(s)` schemes, URLs with embedded credentials, and hosts that resolve to loopback / private / link-local / reserved IP ranges. See Task 2 for the exact implementation and the SSRF caveat in *Notes for the executing agent*.
- **Delivery success criterion:** HTTP status `2xx`. Anything else (including `3xx`, `4xx`, `5xx`, timeout, connection error) raises, so the job's retry policy kicks in. A `4xx` is arguably permanent, but distinguishing is out of scope — bounded retry (`max_retries=5`) caps the waste.

---

## Task 1: `webhooks` + `webhook_deliveries` schema

**Files:**
- Modify: `document_search/index/sqlite_store.py` (extend the `executescript` block in `_init_schema`, lines ~138–158 region — append after the `jobs` indexes)
- Test: `tests/test_webhook_service.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_webhook_service.py`:

```python
from pathlib import Path

import pytest

from document_search.index.sqlite_store import SqliteStore


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "test.db")


def test_webhooks_table_exists(store):
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='webhooks'"
    ).fetchall()
    assert len(rows) == 1


def test_webhook_deliveries_table_exists(store):
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='webhook_deliveries'"
    ).fetchall()
    assert len(rows) == 1


def test_webhooks_columns(store):
    cols = {c[1] for c in store.conn.execute("PRAGMA table_info(webhooks)").fetchall()}
    expected = {
        "id", "url", "event_type", "secret", "enabled", "created_by", "created_at",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_webhook_deliveries_columns(store):
    cols = {c[1] for c in store.conn.execute(
        "PRAGMA table_info(webhook_deliveries)"
    ).fetchall()}
    expected = {
        "id", "webhook_id", "job_id", "event_type", "status_code",
        "success", "error_message", "attempted_at",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_webhook_service.py -v
```

Expected: `test_webhooks_table_exists` fails — table doesn't exist yet.

- [ ] **Step 3: Add schema in `_init_schema`**

In `document_search/index/sqlite_store.py`, inside the `self.conn.executescript("""...""")` block in `_init_schema`, **append** immediately after the last `jobs` index line (`CREATE INDEX IF NOT EXISTS idx_jobs_owner ON jobs(owner_user_id);`, ~line 158) and before the closing `"""`:

```sql
CREATE TABLE IF NOT EXISTS webhooks (
  id INTEGER PRIMARY KEY,
  url TEXT NOT NULL,
  event_type TEXT NOT NULL,
  secret TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_by INTEGER,
  created_at TEXT NOT NULL,
  FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_webhooks_event   ON webhooks(event_type, enabled);
CREATE TABLE IF NOT EXISTS webhook_deliveries (
  id INTEGER PRIMARY KEY,
  webhook_id INTEGER NOT NULL,
  job_id INTEGER,
  event_type TEXT NOT NULL,
  status_code INTEGER,
  success INTEGER NOT NULL DEFAULT 0,
  error_message TEXT,
  attempted_at TEXT NOT NULL,
  FOREIGN KEY (webhook_id) REFERENCES webhooks(id) ON DELETE CASCADE,
  FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_wh_deliveries_hook ON webhook_deliveries(webhook_id);
```

No `ALTER TABLE` migration block is needed: both tables are brand new and `CREATE TABLE IF NOT EXISTS` inside the existing `executescript` is idempotent on every startup, matching the pattern used for `jobs` in the job-queue plan.

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_webhook_service.py -v
```

Expected: 4 passing.

- [ ] **Step 5: Full-suite check**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: baseline + 4 new, zero failures.

- [ ] **Step 6: Commit**

```powershell
git add document_search/index/sqlite_store.py tests/test_webhook_service.py
git commit -m "feat(webhooks): add webhooks and webhook_deliveries tables"
```

---

## Task 2: SSRF URL guard

**Files:**
- Create: `document_search/services/webhook_service.py` (first slice — just the guard)
- Test: `tests/test_webhook_service.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_webhook_service.py`:

```python
from document_search.services.webhook_service import validate_webhook_url, WebhookUrlError


def test_validate_accepts_public_http_url():
    # Should not raise. Uses a documented TEST-NET address that is non-routable
    # but not in the blocked private/loopback ranges checked by the guard.
    validate_webhook_url("https://hooks.example.com/seekr")


def test_validate_rejects_non_http_scheme():
    with pytest.raises(WebhookUrlError):
        validate_webhook_url("ftp://example.com/x")
    with pytest.raises(WebhookUrlError):
        validate_webhook_url("file:///etc/passwd")


def test_validate_rejects_embedded_credentials():
    with pytest.raises(WebhookUrlError):
        validate_webhook_url("http://user:pass@example.com/x")


def test_validate_rejects_loopback():
    with pytest.raises(WebhookUrlError):
        validate_webhook_url("http://127.0.0.1/x")
    with pytest.raises(WebhookUrlError):
        validate_webhook_url("http://localhost/x")


def test_validate_rejects_private_and_linklocal():
    with pytest.raises(WebhookUrlError):
        validate_webhook_url("http://10.0.0.5/x")
    with pytest.raises(WebhookUrlError):
        validate_webhook_url("http://192.168.1.10/x")
    with pytest.raises(WebhookUrlError):
        validate_webhook_url("http://169.254.169.254/latest/meta-data")


def test_validate_rejects_missing_host():
    with pytest.raises(WebhookUrlError):
        validate_webhook_url("http:///nohost")
```

- [ ] **Step 2: Run, expect ImportError**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_webhook_service.py -v
```

Expected: ImportError — `webhook_service` module doesn't exist.

- [ ] **Step 3: Implement the guard**

Create `document_search/services/webhook_service.py` with this first slice (the rest of the class is added in Task 3 — keep this exact content, Task 3 appends):

```python
"""Outbound webhook subscriptions + signed delivery.

The only module that writes the `webhooks` and `webhook_deliveries` tables.
Delivery reuses the persistent job queue: `enqueue_event` enqueues one
`webhook_deliver` job per matching subscription, and the Worker handler
registered in app.py calls `deliver()` which performs the signed HTTP POST.

Signing: `X-Seekr-Signature: sha256=<hex>` where
    <hex> = HMAC-SHA256(secret, raw_body_bytes)
over the exact bytes POSTed. The body is canonical JSON
(`separators=(",", ":")`, `sort_keys=True`) so signed bytes are deterministic.

HTTP delivery mirrors the stdlib `urllib.request` timeout pattern already used
in `ai_organizer.py` — no new third-party dependency.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import urllib.error
import urllib.request
from datetime import UTC, datetime
from urllib.parse import urlsplit

from document_search.index.sqlite_store import SqliteStore
from document_search.services.job_store import JobStore

logger = logging.getLogger(__name__)

# Event types Seekr can emit. `search.new_match` is reserved for the saved-search
# follow-up; the delivery path already supports it.
KNOWN_EVENT_TYPES = ("index.completed", "search.new_match")

_ALLOWED_SCHEMES = ("http", "https")


class WebhookUrlError(ValueError):
    """Raised when a webhook target URL fails the SSRF guard."""


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_webhook_url(url: str) -> None:
    """SSRF guard. Raise WebhookUrlError if `url` is not a safe outbound target.

    Rejects: non-http(s) schemes, embedded credentials, missing host, and any
    host that resolves to a loopback / private / link-local / reserved address.
    Called at create time AND again immediately before each POST.
    """
    parts = urlsplit(url)
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise WebhookUrlError(f"URL scheme must be http or https, got '{parts.scheme}'")
    if parts.username or parts.password:
        raise WebhookUrlError("URL must not contain embedded credentials")
    host = parts.hostname
    if not host:
        raise WebhookUrlError("URL must contain a host")

    # Resolve every address the host maps to and block if ANY is internal.
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise WebhookUrlError(f"Host could not be resolved: {exc}") from exc

    seen = False
    for info in infos:
        sockaddr = info[4]
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        seen = True
        if _is_blocked_ip(ip):
            raise WebhookUrlError(f"Host resolves to a blocked address: {ip}")
    if not seen:
        raise WebhookUrlError("Host did not resolve to any usable address")
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_webhook_service.py -v
```

Expected: 4 (Task 1) + 6 (Task 2) = 10 passing.

> Note: `test_validate_accepts_public_http_url` performs a real DNS lookup of `hooks.example.com`. `example.com` is an IANA-reserved domain guaranteed to resolve to a documented public address, so this is safe and offline-stable in practice. If your CI sandbox blocks all DNS, change that one test to a literal public IP, e.g. `validate_webhook_url("https://93.184.216.34/seekr")`, which skips DNS entirely.

- [ ] **Step 5: Commit**

```powershell
git add document_search/services/webhook_service.py tests/test_webhook_service.py
git commit -m "feat(webhooks): SSRF guard for outbound target URLs"
```

---

## Task 3: `WebhookService` — CRUD, signing, delivery, fan-out

**Files:**
- Modify: `document_search/services/webhook_service.py` (append the `WebhookService` class + signing helper)
- Test: `tests/test_webhook_service.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_webhook_service.py`:

```python
import json as _json

from document_search.services.job_store import JobStore
from document_search.services.webhook_service import (
    WebhookService,
    sign_payload,
    canonical_body,
)


@pytest.fixture
def svc(store):
    return WebhookService(store, JobStore(store))


def test_sign_payload_is_hmac_sha256_over_canonical_body():
    import hashlib
    import hmac as _hmac
    body = canonical_body({"event": "index.completed", "data": {"b": 2, "a": 1}})
    sig = sign_payload("topsecret", body)
    expected = "sha256=" + _hmac.new(
        b"topsecret", body, hashlib.sha256
    ).hexdigest()
    assert sig == expected
    # Canonical body is deterministic regardless of key insertion order
    assert canonical_body({"data": {"a": 1, "b": 2}, "event": "index.completed"}) == body


def test_create_webhook_generates_secret_and_returns_it_once(svc):
    wh = svc.create(url="https://hooks.example.com/x", event_type="index.completed",
                    secret=None, created_by=1)
    assert isinstance(wh["id"], int)
    assert wh["secret"]  # returned on create
    listed = svc.list_webhooks()
    assert listed[0]["id"] == wh["id"]
    assert "secret" not in listed[0]  # never leaked by list


def test_create_webhook_rejects_unsafe_url(svc):
    from document_search.services.webhook_service import WebhookUrlError
    with pytest.raises(WebhookUrlError):
        svc.create(url="http://127.0.0.1/x", event_type="index.completed",
                   secret=None, created_by=1)


def test_create_webhook_rejects_unknown_event_type(svc):
    with pytest.raises(ValueError, match="event_type"):
        svc.create(url="https://hooks.example.com/x", event_type="bogus.event",
                   secret=None, created_by=1)


def test_delete_webhook(svc):
    wh = svc.create(url="https://hooks.example.com/x", event_type="index.completed",
                    secret=None, created_by=1)
    assert svc.delete(wh["id"]) is True
    assert svc.list_webhooks() == []
    assert svc.delete(wh["id"]) is False  # already gone


def test_enqueue_event_fans_out_one_job_per_matching_enabled_webhook(svc, store):
    svc.create(url="https://hooks.example.com/a", event_type="index.completed",
               secret="s1", created_by=1)
    svc.create(url="https://hooks.example.com/b", event_type="index.completed",
               secret="s2", created_by=1)
    # A different event type must NOT match
    svc.create(url="https://hooks.example.com/c", event_type="search.new_match",
               secret="s3", created_by=1)
    # A disabled webhook must NOT match
    disabled = svc.create(url="https://hooks.example.com/d", event_type="index.completed",
                          secret="s4", created_by=1)
    store.conn.execute("UPDATE webhooks SET enabled=0 WHERE id=?", (disabled["id"],))
    store.conn.commit()

    n = svc.enqueue_event("index.completed", {"job_id": 42, "found": 3})
    assert n == 2
    jobs = store.conn.execute(
        "SELECT kind, payload_json, max_retries FROM jobs WHERE kind='webhook_deliver'"
    ).fetchall()
    assert len(jobs) == 2
    payload = _json.loads(jobs[0]["payload_json"])
    assert payload["event_type"] == "index.completed"
    assert payload["event"] == {"job_id": 42, "found": 3}
    assert payload["url"].startswith("https://hooks.example.com/")
    assert jobs[0]["max_retries"] == 5


def test_deliver_posts_signed_body_and_records_success(svc, store, monkeypatch):
    captured = {}

    class _FakeResp:
        status = 204
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b""

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = req.data
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["timeout"] = timeout
        return _FakeResp()

    monkeypatch.setattr(
        "document_search.services.webhook_service.urllib.request.urlopen",
        _fake_urlopen,
    )
    # Skip the SSRF DNS lookup for this unit test (delivery re-validates the URL).
    monkeypatch.setattr(
        "document_search.services.webhook_service.validate_webhook_url",
        lambda url: None,
    )

    wh = svc.create(url="https://hooks.example.com/x", event_type="index.completed",
                    secret="shh", created_by=1)
    job_payload = {
        "webhook_id": wh["id"],
        "url": "https://hooks.example.com/x",
        "secret": "shh",
        "event_type": "index.completed",
        "event": {"job_id": 7, "found": 2},
    }
    svc.deliver(job_payload, job_id=99)

    # The signature header must verify against the EXACT bytes posted.
    import hashlib
    import hmac as _hmac
    sent_body = captured["body"]
    expected_sig = "sha256=" + _hmac.new(b"shh", sent_body, hashlib.sha256).hexdigest()
    assert captured["headers"]["x-seekr-signature"] == expected_sig
    assert captured["headers"]["x-seekr-event"] == "index.completed"
    assert captured["headers"]["content-type"] == "application/json"
    # Body structure
    body = _json.loads(sent_body)
    assert body["event"] == "index.completed"
    assert body["data"] == {"job_id": 7, "found": 2}
    assert "delivered_at" in body

    # An audit row recorded the success
    row = store.conn.execute(
        "SELECT success, status_code FROM webhook_deliveries WHERE webhook_id=?",
        (wh["id"],),
    ).fetchone()
    assert row["success"] == 1
    assert row["status_code"] == 204


def test_deliver_raises_and_records_failure_on_non_2xx(svc, store, monkeypatch):
    import urllib.error

    def _fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "boom", hdrs=None, fp=None)

    monkeypatch.setattr(
        "document_search.services.webhook_service.urllib.request.urlopen",
        _fake_urlopen,
    )
    monkeypatch.setattr(
        "document_search.services.webhook_service.validate_webhook_url",
        lambda url: None,
    )
    wh = svc.create(url="https://hooks.example.com/x", event_type="index.completed",
                    secret="shh", created_by=1)
    job_payload = {
        "webhook_id": wh["id"], "url": "https://hooks.example.com/x",
        "secret": "shh", "event_type": "index.completed", "event": {"x": 1},
    }
    with pytest.raises(Exception):
        svc.deliver(job_payload, job_id=100)
    row = store.conn.execute(
        "SELECT success, error_message FROM webhook_deliveries WHERE webhook_id=?",
        (wh["id"],),
    ).fetchone()
    assert row["success"] == 0
    assert row["error_message"]
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_webhook_service.py -v
```

Expected: ImportError on `WebhookService` / `sign_payload` / `canonical_body`.

- [ ] **Step 3: Implement the service**

**Append** to `document_search/services/webhook_service.py` (below the `validate_webhook_url` function from Task 2):

```python
def canonical_body(payload: dict) -> bytes:
    """Deterministic JSON bytes for signing + posting (stable key order)."""
    return json.dumps(
        payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False
    ).encode("utf-8")


def sign_payload(secret: str, body: bytes) -> str:
    """Return the `X-Seekr-Signature` header value for `body`."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class WebhookService:
    """CRUD + delivery for outbound webhooks. Writes `webhooks` and
    `webhook_deliveries`; enqueues `webhook_deliver` jobs via JobStore."""

    JOB_KIND = "webhook_deliver"
    DELIVERY_MAX_RETRIES = 5
    DELIVERY_TIMEOUT_S = 15

    def __init__(self, store: SqliteStore, job_store: JobStore):
        self.store = store
        self.conn = store.conn
        self.job_store = job_store

    @staticmethod
    def _now() -> str:
        return datetime.now(tz=UTC).isoformat()

    # ── CRUD ───────────────────────────────────────────────────────────

    def create(
        self,
        url: str,
        event_type: str,
        secret: str | None,
        created_by: int | None,
    ) -> dict:
        if event_type not in KNOWN_EVENT_TYPES:
            raise ValueError(
                f"Unknown event_type '{event_type}'. "
                f"Allowed: {', '.join(KNOWN_EVENT_TYPES)}"
            )
        validate_webhook_url(url)  # SSRF guard at create time
        secret = secret or secrets.token_hex(32)
        now = self._now()
        cur = self.conn.execute(
            "INSERT INTO webhooks(url, event_type, secret, enabled, created_by, created_at) "
            "VALUES(?, ?, ?, 1, ?, ?)",
            (url, event_type, secret, created_by, now),
        )
        self.conn.commit()
        return {
            "id": int(cur.lastrowid),
            "url": url,
            "event_type": event_type,
            "secret": secret,  # returned ONCE, here only
            "enabled": True,
            "created_at": now,
        }

    def list_webhooks(self) -> list[dict]:
        """List subscriptions WITHOUT secrets."""
        rows = self.conn.execute(
            "SELECT id, url, event_type, enabled, created_by, created_at "
            "FROM webhooks ORDER BY id"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["enabled"] = bool(d["enabled"])
            out.append(d)
        return out

    def delete(self, webhook_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM webhooks WHERE id=?", (webhook_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def list_deliveries(self, webhook_id: int, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, webhook_id, job_id, event_type, status_code, success, "
            "error_message, attempted_at FROM webhook_deliveries "
            "WHERE webhook_id=? ORDER BY id DESC LIMIT ?",
            (webhook_id, limit),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["success"] = bool(d["success"])
            out.append(d)
        return out

    # ── Fan-out ────────────────────────────────────────────────────────

    def enqueue_event(self, event_type: str, event: dict) -> int:
        """Enqueue one `webhook_deliver` job per enabled subscription matching
        `event_type`. Returns the number of jobs enqueued."""
        rows = self.conn.execute(
            "SELECT id, url, secret FROM webhooks WHERE event_type=? AND enabled=1",
            (event_type,),
        ).fetchall()
        count = 0
        for r in rows:
            self.job_store.enqueue(
                self.JOB_KIND,
                payload={
                    "webhook_id": r["id"],
                    "url": r["url"],
                    "secret": r["secret"],
                    "event_type": event_type,
                    "event": event,
                },
                owner_user_id=None,
                max_retries=self.DELIVERY_MAX_RETRIES,
            )
            count += 1
        return count

    # ── Delivery (called by the Worker handler) ────────────────────────

    def deliver(self, job_payload: dict, job_id: int | None = None) -> None:
        """Perform the signed POST. Raises on any non-2xx / transport error so
        the job queue's retry-with-backoff re-attempts. Records an audit row
        on every attempt (success or failure)."""
        url = job_payload["url"]
        secret = job_payload["secret"]
        event_type = job_payload["event_type"]
        event = job_payload["event"]

        # Re-validate the stored URL right before sending (TOCTOU / DNS-rebind).
        validate_webhook_url(url)

        http_body = canonical_body({
            "event": event_type,
            "delivered_at": self._now(),
            "data": event,
        })
        signature = sign_payload(secret, http_body)
        req = urllib.request.Request(
            url,
            data=http_body,
            headers={
                "Content-Type": "application/json",
                "X-Seekr-Event": event_type,
                "X-Seekr-Signature": signature,
                "X-Seekr-Delivery": str(job_id) if job_id is not None else "",
                "User-Agent": "Seekr-webhook/1.0",
            },
            method="POST",
        )

        status_code: int | None = None
        error_message: str | None = None
        try:
            with urllib.request.urlopen(req, timeout=self.DELIVERY_TIMEOUT_S) as resp:
                status_code = getattr(resp, "status", None)
                if status_code is None or not (200 <= status_code < 300):
                    error_message = f"Non-2xx response: {status_code}"
        except urllib.error.HTTPError as exc:
            status_code = exc.code
            error_message = f"HTTP {exc.code}: {exc.reason}"
        except urllib.error.URLError as exc:
            error_message = f"URLError: {exc.reason}"
        except Exception as exc:  # noqa: BLE001 - audit then re-raise below
            error_message = f"{type(exc).__name__}: {exc}"

        success = error_message is None
        self._record_delivery(
            webhook_id=job_payload["webhook_id"],
            job_id=job_id,
            event_type=event_type,
            status_code=status_code,
            success=success,
            error_message=error_message,
        )
        if not success:
            raise RuntimeError(error_message)

    def _record_delivery(
        self,
        webhook_id: int,
        job_id: int | None,
        event_type: str,
        status_code: int | None,
        success: bool,
        error_message: str | None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO webhook_deliveries("
            "webhook_id, job_id, event_type, status_code, success, error_message, attempted_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (webhook_id, job_id, event_type, status_code,
             1 if success else 0, error_message, self._now()),
        )
        self.conn.commit()
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_webhook_service.py -v
```

Expected: 10 (Tasks 1–2) + 8 new = 18 passing.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: previous total + 8, zero failures.

- [ ] **Step 6: Commit**

```powershell
git add document_search/services/webhook_service.py tests/test_webhook_service.py
git commit -m "feat(webhooks): WebhookService CRUD, HMAC signing, queued delivery"
```

---

## Task 4: Wire `WebhookService` + `webhook_deliver` handler into the app

**Files:**
- Modify: `document_search/app.py` — instantiate `WebhookService` next to `job_store`/`worker`; register the `webhook_deliver` handler; add the `WebhookCreateRequest` model.
- Test: `tests/test_app_webhooks.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_app_webhooks.py`:

```python
import pytest

pytestmark = pytest.mark.skipif(
    not pytest.importorskip("fastapi", reason="fastapi not installed"),
    reason="fastapi required",
)

from fastapi.testclient import TestClient

from document_search.app import create_app


def _login(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_app_exposes_webhook_service_and_handler(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app):
        assert hasattr(app.state, "webhook_service")
        # The webhook_deliver handler must be registered on the worker.
        assert "webhook_deliver" in app.state.worker._handlers
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_webhooks.py -v
```

Expected: FAIL — `app.state.webhook_service` not set.

- [ ] **Step 3: Wire it in `app.py`**

First, add the request model. In `document_search/app.py`, after the `HaSearchRequest` class (~line 196), insert:

```python
class WebhookCreateRequest(BaseModel):
    url: str = Field(min_length=1)
    event_type: str
    secret: str | None = None
```

Then, in `create_app`, find the block right after `app.state.worker = worker` (~line 276). **After** that line and before the `@app.on_event("startup")` decorator, insert:

```python
    # Outbound webhooks
    from document_search.services.webhook_service import WebhookService
    webhook_service = WebhookService(_startup_db, job_store)
    app.state.webhook_service = webhook_service
```

Then, alongside the other `@worker.handler(...)` registrations (after the `ai_reorganize` handler, ~line 391), append:

```python
    @worker.handler("webhook_deliver")
    def _handle_webhook_deliver(payload: dict, progress_cb):
        # Use a worker-thread-owned service so DB access stays off the
        # request-thread-local store(). The signed POST happens here; a raise
        # re-enters the queue's retry-with-backoff.
        from document_search.services.webhook_service import WebhookService
        svc = WebhookService(SqliteStore(Path(db_path)), job_store)
        svc.deliver(payload, job_id=payload.get("_job_id"))
        return {"delivered": True}
```

> The handler signature receives only `(payload, progress_cb)` — it does not get the job id directly. `enqueue_event` does not know the job id at enqueue time, and the audit row's `job_id` is best-effort. Passing `payload.get("_job_id")` yields `None` here, which is fine: the `webhook_deliveries.job_id` column is nullable and the delivery still records correctly. (If you later want the exact job id in the audit row, extend `Worker._execute` to inject `job["id"]` into the payload dict before calling the handler — out of scope for this plan.)

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_webhooks.py -v
```

Expected: 1 passing.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: previous total + 1, zero failures.

- [ ] **Step 6: Commit**

```powershell
git add document_search/app.py tests/test_app_webhooks.py
git commit -m "feat(webhooks): wire WebhookService and webhook_deliver handler into app"
```

---

## Task 5: Admin CRUD endpoints `/api/webhooks/*`

**Files:**
- Modify: `document_search/app.py` — add the four admin routes.
- Test: `tests/test_app_webhooks.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_app_webhooks.py`:

```python
def test_create_webhook_admin_only(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        admin_token = _login(client)
        # Create a non-admin user and log in as them.
        client.post(
            "/api/users",
            headers={"X-Auth-Token": admin_token},
            json={"username": "bob", "password": "bob-password", "role": "user"},
        )
        bob_token = _login(client, "bob", "bob-password")

        # Non-admin is rejected (403).
        r = client.post(
            "/api/webhooks",
            headers={"X-Auth-Token": bob_token},
            json={"url": "https://hooks.example.com/x", "event_type": "index.completed"},
        )
        assert r.status_code == 403, r.text

        # Admin succeeds and gets the secret back exactly once.
        r = client.post(
            "/api/webhooks",
            headers={"X-Auth-Token": admin_token},
            json={"url": "https://hooks.example.com/x", "event_type": "index.completed"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["secret"]
        wh_id = body["id"]

        # List (admin-only) does NOT leak the secret.
        r = client.get("/api/webhooks", headers={"X-Auth-Token": admin_token})
        assert r.status_code == 200, r.text
        rows = r.json()
        assert any(w["id"] == wh_id for w in rows)
        assert all("secret" not in w for w in rows)

        # Non-admin cannot list.
        r = client.get("/api/webhooks", headers={"X-Auth-Token": bob_token})
        assert r.status_code == 403


def test_create_webhook_rejects_unsafe_url_via_api(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        r = client.post(
            "/api/webhooks",
            headers={"X-Auth-Token": token},
            json={"url": "http://127.0.0.1/x", "event_type": "index.completed"},
        )
        assert r.status_code == 400, r.text


def test_create_webhook_rejects_unknown_event_via_api(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        r = client.post(
            "/api/webhooks",
            headers={"X-Auth-Token": token},
            json={"url": "https://hooks.example.com/x", "event_type": "nope"},
        )
        assert r.status_code == 400, r.text


def test_delete_webhook_admin_only(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        wh_id = client.post(
            "/api/webhooks",
            headers={"X-Auth-Token": token},
            json={"url": "https://hooks.example.com/x", "event_type": "index.completed"},
        ).json()["id"]
        r = client.delete(f"/api/webhooks/{wh_id}", headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        # 404 on second delete
        r = client.delete(f"/api/webhooks/{wh_id}", headers={"X-Auth-Token": token})
        assert r.status_code == 404
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_webhooks.py -v
```

Expected: the four new tests fail (routes don't exist → 404/405).

- [ ] **Step 3: Add the routes in `app.py`**

In `document_search/app.py`, after the `api_index_extensions` endpoint (~line 833, just before the `/api/update/check` block) insert these admin routes. They follow the same `require_admin` + `store()` pattern as the existing user-management routes:

```python
    # ── Webhooks (admin) ───────────────────────────────────────────────

    @app.get("/api/webhooks", tags=["webhooks"])
    def api_list_webhooks(x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        return webhook_service.list_webhooks()

    @app.post("/api/webhooks", tags=["webhooks"])
    def api_create_webhook(
        req: WebhookCreateRequest,
        x_auth_token: str | None = Header(default=None),
    ):
        admin_id = require_admin(x_auth_token)
        from document_search.services.webhook_service import WebhookUrlError
        try:
            return webhook_service.create(
                url=req.url,
                event_type=req.event_type,
                secret=req.secret,
                created_by=admin_id,
            )
        except WebhookUrlError as e:
            raise HTTPException(status_code=400, detail=f"Invalid webhook URL: {e}")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.delete("/api/webhooks/{webhook_id}", tags=["webhooks"])
    def api_delete_webhook(
        webhook_id: int,
        x_auth_token: str | None = Header(default=None),
    ):
        require_admin(x_auth_token)
        if not webhook_service.delete(webhook_id):
            raise HTTPException(status_code=404, detail="Webhook not found")
        return {"status": "deleted", "id": webhook_id}

    @app.get("/api/webhooks/{webhook_id}/deliveries", tags=["webhooks"])
    def api_webhook_deliveries(
        webhook_id: int,
        x_auth_token: str | None = Header(default=None),
    ):
        require_admin(x_auth_token)
        return webhook_service.list_deliveries(webhook_id)
```

> `webhook_service` was created with the worker's `_startup_db` (Task 4). All these routes run on FastAPI request threads but only touch the `webhooks` / `webhook_deliveries` tables through that one connection. SQLite is opened `check_same_thread=False`; for the realistic admin-CRUD volume this is fine, and it mirrors how `job_store` (also built on `_startup_db`) is already shared across request threads in the existing code. If contention ever shows up, wrap `WebhookService` writes in the same `RLock` pattern `JobStore` uses — not needed for this plan.

Also add a `webhooks` OpenAPI tag entry. Find `_OPENAPI_TAGS` near the top of `app.py` and append a dict `{"name": "webhooks", "description": "Outbound webhook subscriptions (admin)."}` to that list. (If `_OPENAPI_TAGS` doesn't define `ha` either, skip this — the `tags=["webhooks"]` decorator still works without a registered description.)

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_webhooks.py -v
```

Expected: 5 passing (1 from Task 4 + 4 new).

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: previous total + 4, zero failures.

- [ ] **Step 6: Commit**

```powershell
git add document_search/app.py tests/test_app_webhooks.py
git commit -m "feat(webhooks): admin CRUD endpoints for /api/webhooks"
```

---

## Task 6: Fire `index.completed` when an `index_paths` job succeeds

**Files:**
- Modify: `document_search/app.py` — at the end of the `index_paths` handler, fan out `index.completed`.
- Test: `tests/test_app_webhooks.py` (extend)

This is the first wired event. The cleanest fire site is the end of the `index_paths` handler (after the indexing loop returns its `counts`), so the event carries the final counts. Firing there (rather than in a generic `mark_succeeded` hook) keeps the queue generic and avoids every job kind accidentally emitting events.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_app_webhooks.py`:

```python
def test_completed_index_job_enqueues_webhook_delivery(tmp_path):
    """When an index_paths job succeeds, one webhook_deliver job is enqueued
    per matching subscription. We don't let it POST — we assert the queued job."""
    import time

    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = _login(client)
        # Subscribe to index.completed.
        client.post(
            "/api/webhooks",
            headers={"X-Auth-Token": token},
            json={"url": "https://hooks.example.com/x", "event_type": "index.completed"},
        )
        # Start an index over an empty dir (finishes instantly, no documents).
        empty = tmp_path / "empty"
        empty.mkdir()
        start = client.post(
            "/api/index/start",
            headers={"X-Auth-Token": token},
            json={"paths": [str(empty)]},
        ).json()
        index_job_id = start["job_id"]

        # Wait for the index job to finish.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            body = client.get(
                f"/api/index/jobs/{index_job_id}",
                headers={"X-Auth-Token": token},
            ).json()
            if body["status"] in ("finished", "failed", "interrupted"):
                break
            time.sleep(0.05)
        assert body["status"] == "finished"

        # A webhook_deliver job must now exist (queued by enqueue_event).
        store = app.state.job_store
        deadline = time.monotonic() + 5
        rows = []
        while time.monotonic() < deadline:
            rows = store.list_jobs(kind="webhook_deliver", limit=10)
            if rows:
                break
            time.sleep(0.05)
        assert len(rows) >= 1
        import json as _json
        payload = _json.loads(rows[0]["payload_json"])
        assert payload["event_type"] == "index.completed"
        assert "found" in payload["event"]
        assert payload["event"]["index_job_id"] == int(index_job_id)
```

> Note on the delivery job's own fate: the `webhook_deliver` job will try to POST to `https://hooks.example.com/x`, fail (no receiver), and retry-then-fail per its `max_retries=5`. That's fine — the test only asserts the delivery job was *enqueued*, never that it succeeded, and no real successful network call occurs. The first attempt's outbound POST hits a non-existent receiver and errors out quickly via `urllib`; the SSRF guard allows the host (public) but the connection fails. To keep the test fully offline and avoid a real outbound DNS/connection attempt during the background retry, the assertion completes well before the queue's backoff schedules a second attempt (backoff is `2**1 = 2s` minimum). If your CI forbids any outbound socket, register a stub `webhook_deliver` handler on `app.state.worker` before `TestClient(app)` that just returns `{"ok": True}` — the fan-out assertion still holds because the job is enqueued regardless of handler behaviour.

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_webhooks.py::test_completed_index_job_enqueues_webhook_delivery -v
```

Expected: FAIL — no `webhook_deliver` job is enqueued because the handler doesn't fire the event yet.

- [ ] **Step 3: Fire the event from the `index_paths` handler**

In `document_search/app.py`, locate the end of `_handle_index_paths` (the `return counts` line, ~line 328). **Replace** that final `return counts` with a fan-out then return:

```python
        webhook_service.enqueue_event(
            "index.completed",
            {
                "index_job_id": int(payload.get("_job_id")) if payload.get("_job_id") else None,
                "paths": paths,
                "found": counts["found"],
                "indexed": counts["indexed"],
                "skipped": counts["skipped"],
                "updated": counts["updated"],
                "errors": counts["errors"],
            },
        )
        return counts
```

But the handler doesn't receive its own job id (see Task 4 note). The test asserts `payload["event"]["index_job_id"] == int(index_job_id)`, so the job id MUST be present. The minimal, surgical way to make the job id available to the handler is to have the Worker inject it. In `document_search/services/job_worker.py`, in `Worker._execute`, change the payload-decoding line so the job id is added to the dict before the handler runs:

```python
            payload = json.loads(job["payload_json"])
            payload["_job_id"] = job_id
```

(Find the existing `payload = json.loads(job["payload_json"])` line inside `_execute` and append the `payload["_job_id"] = job_id` line directly after it. `job_id` is already in scope.)

This is backward-compatible: every existing handler ignores unknown payload keys, and the `webhook_deliver` handler already reads `payload.get("_job_id")` for its audit row (Task 4).

> Design note: injecting `_job_id` at dispatch time (rather than persisting it in `payload_json`) keeps the stored payload clean and avoids a chicken-and-egg problem (the job id isn't known until after `enqueue`). The key is underscore-prefixed to mark it as worker-injected metadata, not user payload.

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_webhooks.py -v
```

Expected: 6 passing.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: previous total + 1, zero failures. The job-worker tests from the job-queue plan still pass because `_job_id` injection is additive and ignored by their synthetic handlers.

- [ ] **Step 6: Commit**

```powershell
git add document_search/app.py document_search/services/job_worker.py tests/test_app_webhooks.py
git commit -m "feat(webhooks): fire index.completed event when index_paths job succeeds"
```

### Follow-up (out of scope here): the `search.new_match` event

The saved-search plan will, after persisting a "new match" detection, call exactly:

```python
webhook_service.enqueue_event("search.new_match", {
    "saved_search_id": <id>,
    "query": <query>,
    "document_id": <doc_id>,
    "path": <path>,
    "snippet": <snippet>,
})
```

No webhook-side code changes are needed: `search.new_match` is already in `KNOWN_EVENT_TYPES`, the `webhooks.event_type` column already stores it, the fan-out (`enqueue_event`), the signed delivery, the SSRF guard, retry, and the audit trail all work identically. The saved-search plan only has to (1) detect a new match and (2) make that one `enqueue_event` call from wherever the detection runs (most naturally a new Worker handler kind, e.g. `saved_search_scan`, that calls `webhook_service.enqueue_event` when it finds a new hit).

---

## Task 7: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Run full suite cleanly**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: zero failures, zero flaky reruns.

- [ ] **Step 2: Manual smoke test of CRUD + signing via TestClient**

```powershell
$env:PYTHONPATH = "."; python -c @'
from fastapi.testclient import TestClient
from document_search.app import create_app
from document_search.services.webhook_service import canonical_body, sign_payload
import tempfile, pathlib, json
tmp = pathlib.Path(tempfile.mkdtemp())
app = create_app(str(tmp / "smoke.db"))
with TestClient(app) as c:
    tok = c.post("/api/login", json={"username":"admin","password":"admin"}).json()["token"]
    h = {"X-Auth-Token": tok}
    wh = c.post("/api/webhooks", headers=h, json={"url":"https://hooks.example.com/x","event_type":"index.completed"}).json()
    print("created:", wh["id"], "secret_len:", len(wh["secret"]))
    listed = c.get("/api/webhooks", headers=h).json()
    assert all("secret" not in w for w in listed), "secret leaked in list!"
    # Verify the signature scheme is reproducible
    body = canonical_body({"event":"index.completed","delivered_at":"t","data":{"a":1}})
    sig = sign_payload(wh["secret"], body)
    assert sig.startswith("sha256="), sig
    print("signature ok:", sig[:24], "...")
    d = c.delete(f"/api/webhooks/{wh[\"id\"]}", headers=h)
    print("deleted:", d.status_code)
print("OK")
'@
```

Expected: prints `created: <id> secret_len: 64`, `signature ok: sha256=... ...`, `deleted: 200`, then `OK`. No `secret leaked` assertion error.

- [ ] **Step 3: Confirm `WebhookService` is the only writer of the webhook tables**

```powershell
$env:PYTHONPATH = "."; Select-String -Path document_search\*.py, document_search\**\*.py -Pattern "INSERT INTO webhooks|INSERT INTO webhook_deliveries|UPDATE webhooks"
```

Expected: every match is inside `document_search/services/webhook_service.py`. (Tests may also write `UPDATE webhooks SET enabled=0` directly to simulate a disabled subscription — that's intentional in test setup, not production code.)

- [ ] **Step 4: No commit (verification only)**

---

## Definition of Done

- [ ] `pytest -q` is green: 19 new tests pass (4 schema + 6 SSRF + 8 service in `test_webhook_service.py`; 6 in `test_app_webhooks.py`) on top of the existing baseline, zero failures, zero flaky reruns.
- [ ] `webhooks` table exists (`id`, `url`, `event_type`, `secret`, `enabled`, `created_by`, `created_at`) and `webhook_deliveries` table exists (`id`, `webhook_id`, `job_id`, `event_type`, `status_code`, `success`, `error_message`, `attempted_at`), both created idempotently in `_init_schema`.
- [ ] `WebhookService` is the only production module that writes `webhooks` / `webhook_deliveries` (grep enforcement in Task 7 Step 3).
- [ ] `/api/webhooks` (GET list, POST create), `/api/webhooks/{id}` (DELETE), `/api/webhooks/{id}/deliveries` (GET) all require admin; non-admin gets 403.
- [ ] Create generates a secret when none is supplied, returns it exactly once, and never leaks it via the list endpoint.
- [ ] Create rejects unsafe URLs (non-http(s), embedded credentials, loopback/private/link-local hosts) with HTTP 400, and rejects unknown `event_type` with HTTP 400.
- [ ] Delivery POSTs canonical-JSON body with `X-Seekr-Signature: sha256=HMAC-SHA256(secret, body)`, `X-Seekr-Event`, and `X-Seekr-Delivery` headers; the signature verifies against the exact bytes sent (proven by `test_deliver_posts_signed_body_and_records_success`).
- [ ] Delivery raises on non-2xx / transport error so the job queue's existing retry-with-backoff re-attempts; `webhook_deliver` jobs are enqueued with `max_retries=5`.
- [ ] Every delivery attempt (success or failure) writes a `webhook_deliveries` audit row.
- [ ] A succeeded `index_paths` job fans out one `webhook_deliver` job per matching enabled `index.completed` subscription, carrying the final counts and the source `index_job_id`.
- [ ] `search.new_match` is reserved in `KNOWN_EVENT_TYPES` and documented as a drop-in follow-up requiring no webhook-side changes.
- [ ] No test performs a real successful network call; all HTTP is mocked or captured.

---

## Notes for the executing agent

- **SSRF is the headline risk here — do not weaken `validate_webhook_url`.** A webhook URL is an attacker-controllable string that the server fetches. Without the guard, an admin (or anyone who compromises an admin session) could point a webhook at `http://169.254.169.254/...` (cloud metadata), `http://127.0.0.1:<port>/...` (internal services), or a private-range host and turn Seekr into an SSRF proxy. The guard runs **twice** by design: at create time (reject before storing) and again immediately before each POST in `deliver()` (defends against TOCTOU and DNS-rebind on already-stored rows). Keep both. The guard resolves the host and blocks if *any* resolved address is internal — a single A record pointing at a public IP plus an AAAA record at `::1` must still be rejected.
- **The SSRF guard is best-effort, not bulletproof.** `socket.getaddrinfo` at validate time and the actual connect at POST time can resolve differently (DNS rebinding). A fully hardened version would resolve once and pin the connection to that IP (custom `http.client` connection or a `urllib` opener with a pinned-IP socket). That hardening is deliberately out of scope; the double-check plus bounded retry limits the blast radius. If this project ever exposes webhook creation to non-admins, revisit and add IP pinning before shipping.
- **Secrets are stored plaintext.** This matches the existing trust model (HA API keys live plaintext in `config.json`; admin already has full filesystem/DB access). Returning the secret only once on create reduces accidental exposure via the list endpoint. Do not add a "reveal secret" endpoint.
- **Why delivery goes through the job queue, not an inline POST:** see Key Design Decisions. The practical payoff is that the `index_paths` handler never blocks on a slow/dead receiver, deliveries survive restarts (persisted as `pending` jobs), and retry/backoff is free. The cost is one extra job per subscription per event — acceptable at this project's scale.
- **`4xx` responses are retried.** The success criterion is strictly `2xx`. A `404`/`410` from the receiver is arguably permanent and retrying wastes attempts, but distinguishing permanent from transient `4xx` is fiddly and out of scope. `max_retries=5` caps the waste (6 attempts total, backoff `2,4,8,16,32` s). If a future plan wants smarter handling, add a `mark_failed_permanent` path in the handler for specific 4xx codes.
- **`_job_id` injection in `Worker._execute` is the only change to the job-worker module.** It's additive (handlers that don't read it are unaffected) and underscore-prefixed to signal "worker-injected metadata, not user payload." It exists so the `index.completed` event can reference the source index job and so the webhook delivery audit row can record which delivery job it belongs to. Verify the existing `test_job_worker.py` suite still passes after this change — it should, since synthetic handlers ignore extra keys.
- **Canonical JSON matters for signatures.** The body is signed and posted as the *same bytes* (`canonical_body`): `separators=(",", ":")`, `sort_keys=True`. If any code path re-serialises the dict with different settings before posting, the receiver's signature check will fail. Always sign the exact bytes you send — never re-`json.dumps` between signing and posting.
- **Event payload stability is a contract.** Receivers will parse `data.found`, `data.indexed`, etc. Adding fields to the `index.completed` event is safe; renaming or removing them is a breaking change for subscribers. Treat the event schema like a public API.
- **When implementing the `search.new_match` follow-up:** reuse `enqueue_event` verbatim. Do not add a second delivery path. The only new code is the detection logic and a single `webhook_service.enqueue_event("search.new_match", {...})` call.