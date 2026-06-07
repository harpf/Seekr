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
