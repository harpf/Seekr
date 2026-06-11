import document_search.observability as obs


def test_scan_metrics_registered():
    assert obs.SCAN_INGESTED_TOTAL is not None
    assert obs.SCAN_REVIEW_PENDING is not None
    obs.SCAN_INGESTED_TOTAL.labels(inbox="b", outcome="pending").inc()
