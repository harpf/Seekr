
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


from document_search.services.webhook_service import WebhookUrlError, validate_webhook_url


def test_validate_accepts_public_http_url():
    validate_webhook_url("https://93.184.216.34/seekr")


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


import json as _json

from document_search.services.job_store import JobStore
from document_search.services.webhook_service import (
    WebhookService,
    canonical_body,
    sign_payload,
)


@pytest.fixture
def svc(store):
    store.create_user("admin", "admin-password")  # user id 1 -> created_by=1 is a valid FK
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
    assert canonical_body({"data": {"a": 1, "b": 2}, "event": "index.completed"}) == body


def test_create_webhook_generates_secret_and_returns_it_once(svc):
    wh = svc.create(url="https://93.184.216.34/x", event_type="index.completed",
                    secret=None, created_by=1)
    assert isinstance(wh["id"], int)
    assert wh["secret"]
    listed = svc.list_webhooks()
    assert listed[0]["id"] == wh["id"]
    assert "secret" not in listed[0]


def test_create_webhook_rejects_unsafe_url(svc):
    from document_search.services.webhook_service import WebhookUrlError
    with pytest.raises(WebhookUrlError):
        svc.create(url="http://127.0.0.1/x", event_type="index.completed",
                   secret=None, created_by=1)


def test_create_webhook_rejects_unknown_event_type(svc):
    with pytest.raises(ValueError, match="event_type"):
        svc.create(url="https://93.184.216.34/x", event_type="bogus.event",
                   secret=None, created_by=1)


def test_delete_webhook(svc):
    wh = svc.create(url="https://93.184.216.34/x", event_type="index.completed",
                    secret=None, created_by=1)
    assert svc.delete(wh["id"]) is True
    assert svc.list_webhooks() == []
    assert svc.delete(wh["id"]) is False


def test_enqueue_event_fans_out_one_job_per_matching_enabled_webhook(svc, store):
    svc.create(url="https://93.184.216.34/a", event_type="index.completed",
               secret="s1", created_by=1)
    svc.create(url="https://93.184.216.34/b", event_type="index.completed",
               secret="s2", created_by=1)
    svc.create(url="https://93.184.216.34/c", event_type="search.new_match",
               secret="s3", created_by=1)
    disabled = svc.create(url="https://93.184.216.34/d", event_type="index.completed",
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
    assert payload["url"].startswith("https://93.184.216.34/")
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
    monkeypatch.setattr(
        "document_search.services.webhook_service.validate_webhook_url",
        lambda url: None,
    )

    wh = svc.create(url="https://93.184.216.34/x", event_type="index.completed",
                    secret="shh", created_by=1)
    job_payload = {
        "webhook_id": wh["id"],
        "url": "https://93.184.216.34/x",
        "secret": "shh",
        "event_type": "index.completed",
        "event": {"job_id": 7, "found": 2},
    }
    svc.deliver(job_payload, job_id=99)

    import hashlib
    import hmac as _hmac
    sent_body = captured["body"]
    expected_sig = "sha256=" + _hmac.new(b"shh", sent_body, hashlib.sha256).hexdigest()
    assert captured["headers"]["x-seekr-signature"] == expected_sig
    assert captured["headers"]["x-seekr-event"] == "index.completed"
    assert captured["headers"]["content-type"] == "application/json"
    body = _json.loads(sent_body)
    assert body["event"] == "index.completed"
    assert body["data"] == {"job_id": 7, "found": 2}
    assert "delivered_at" in body

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
    wh = svc.create(url="https://93.184.216.34/x", event_type="index.completed",
                    secret="shh", created_by=1)
    job_payload = {
        "webhook_id": wh["id"], "url": "https://93.184.216.34/x",
        "secret": "shh", "event_type": "index.completed", "event": {"x": 1},
    }
    with pytest.raises(RuntimeError):
        svc.deliver(job_payload, job_id=100)
    row = store.conn.execute(
        "SELECT success, error_message FROM webhook_deliveries WHERE webhook_id=?",
        (wh["id"],),
    ).fetchone()
    assert row["success"] == 0
    assert row["error_message"]
