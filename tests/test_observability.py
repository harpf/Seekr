import pytest

prometheus_client = pytest.importorskip("prometheus_client")


def test_observability_module_exposes_registry_and_metrics():
    from document_search import observability as obs

    # A dedicated registry (not the global default) keeps create_app idempotent.
    assert obs.REGISTRY is not None
    # All metric objects exist and are distinct names.
    names = {
        obs.REQUEST_COUNT._name,
        obs.REQUEST_LATENCY._name,
        obs.QUEUE_DEPTH._name,
        obs.JOBS_TOTAL._name,
        obs.INDEX_DOCS_TOTAL._name,
    }
    assert len(names) == 5


def test_render_metrics_returns_prometheus_text():
    from document_search import observability as obs

    body, content_type = obs.render_metrics()
    assert isinstance(body, bytes)
    assert "text/plain" in content_type
    # Exposition always contains HELP/TYPE comment lines for registered metrics.
    text = body.decode("utf-8")
    assert "seekr_http_requests_total" in text
    assert "seekr_queue_depth" in text


def test_worker_is_alive_reflects_poll_thread(tmp_path):
    from document_search.index.sqlite_store import SqliteStore
    from document_search.services.job_store import JobStore
    from document_search.services.job_worker import Worker

    store = SqliteStore(tmp_path / "t.db")
    worker = Worker(JobStore(store), max_concurrent=1, poll_interval_s=0.01)

    # Not started yet -> not alive.
    assert worker.is_alive() is False
    worker.start()
    try:
        assert worker.is_alive() is True
    finally:
        worker.stop()
    # Stopped -> not alive.
    assert worker.is_alive() is False


from fastapi.testclient import TestClient  # noqa: E402

from document_search.app import create_app  # noqa: E402


def test_health_returns_200_and_version(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok"
        assert body["version"] == app.version


def test_health_needs_no_auth(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        # No auth header at all — liveness must never require credentials.
        r = client.get("/health")
        assert r.status_code == 200, r.text


def test_ready_returns_200_when_db_and_worker_ok(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        r = client.get("/ready")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ready"] is True
        assert body["checks"]["database"] is True
        assert body["checks"]["worker"] is True


def test_ready_returns_503_when_db_unreachable(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        # Stop the poll thread first: closing the shared sqlite connection while
        # the worker is mid-`execute` on it triggers a native crash on Windows.
        # Stopping the worker also makes worker.is_alive() False, but the
        # assertion below targets the database check specifically.
        app.state.worker.stop(timeout=5.0)
        # Break the exact connection the /ready route's SELECT 1 runs against.
        app.state.job_store.store.conn.close()
        r = client.get("/ready")
        assert r.status_code == 503, r.text
        body = r.json()
        assert body["ready"] is False
        assert body["checks"]["database"] is False


def test_metrics_endpoint_exposes_expected_names(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        r = client.get("/metrics")
        assert r.status_code == 200, r.text
        body = r.text
        assert "seekr_http_requests_total" in body
        assert "seekr_http_request_duration_seconds" in body
        assert "seekr_queue_depth" in body


def test_metrics_middleware_records_request_count(tmp_path):
    from document_search import observability as _obs

    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        # Drive a request through the middleware against a stable route template.
        before = _obs.REQUEST_COUNT.labels(
            method="GET", path="/health", status="200"
        )._value.get()
        r = client.get("/health")
        assert r.status_code == 200, r.text
        after = _obs.REQUEST_COUNT.labels(
            method="GET", path="/health", status="200"
        )._value.get()
        assert after == before + 1


def test_metrics_queue_depth_reflects_pending_job(tmp_path):
    from document_search import observability as _obs

    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        # Enqueue a job directly so a pending row exists at scrape time.
        app.state.worker.stop(timeout=5.0)  # keep it from being claimed
        app.state.job_store.enqueue("index_paths", {"paths": []})
        r = client.get("/metrics")
        assert r.status_code == 200, r.text
        # The gauge for the pending state must be >= 1 after the scrape refresh.
        assert _obs.QUEUE_DEPTH.labels(state="pending")._value.get() >= 1


def test_metrics_token_gate_when_env_set(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_METRICS_TOKEN", "s3cr3t")
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        # No token -> 401.
        assert client.get("/metrics").status_code == 401
        # Wrong token -> 401.
        assert client.get(
            "/metrics", headers={"Authorization": "Bearer nope"}
        ).status_code == 401
        # Correct token -> 200.
        ok = client.get("/metrics", headers={"Authorization": "Bearer s3cr3t"})
        assert ok.status_code == 200, ok.text
