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
