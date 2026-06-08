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
