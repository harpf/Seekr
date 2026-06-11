"""Prometheus metrics for Seekr.

A single dedicated CollectorRegistry owns every metric object. We deliberately
do NOT use prometheus_client's global default registry: `create_app()` runs
many times across the test suite, and registering the same timeseries against
the default registry twice raises `Duplicated timeseries in CollectorRegistry`.
A private registry keeps app construction idempotent.

Metric naming follows the Prometheus convention `seekr_<subsystem>_<unit>`.
"""
from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# One registry for the whole app. Imported by app.py and by tests.
REGISTRY = CollectorRegistry()

# HTTP request count, labelled by method, route template and status code.
REQUEST_COUNT = Counter(
    "seekr_http_requests_total",
    "Total HTTP requests processed.",
    ["method", "path", "status"],
    registry=REGISTRY,
)

# HTTP request latency in seconds, labelled by method + route template.
REQUEST_LATENCY = Histogram(
    "seekr_http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["method", "path"],
    registry=REGISTRY,
)

# Current number of jobs in each queue state (refreshed at scrape time).
QUEUE_DEPTH = Gauge(
    "seekr_queue_depth",
    "Number of jobs currently in each queue state.",
    ["state"],
    registry=REGISTRY,
)

# Terminal job outcomes, labelled by job kind and outcome (succeeded|failed).
JOBS_TOTAL = Counter(
    "seekr_jobs_total",
    "Total jobs that reached a terminal state.",
    ["kind", "outcome"],
    registry=REGISTRY,
)

# Documents processed by the indexer, labelled by outcome.
INDEX_DOCS_TOTAL = Counter(
    "seekr_index_documents_total",
    "Documents processed by the indexer.",
    ["outcome"],
    registry=REGISTRY,
)

# Scanned documents ingested, labelled by inbox and outcome (pending|error).
SCAN_INGESTED_TOTAL = Counter(
    "seekr_scan_ingested_total",
    "Scanned documents processed by the scan-inbox ingester.",
    ["inbox", "outcome"],
    registry=REGISTRY,
)

# Current number of scan reviews awaiting human action (refreshed at scrape time).
SCAN_REVIEW_PENDING = Gauge(
    "seekr_scan_review_pending",
    "Scan reviews currently in the pending state.",
    registry=REGISTRY,
)

# Every queue state we want a gauge series for, so absent states report 0
# rather than vanishing from the exposition.
_QUEUE_STATES = ("pending", "running", "succeeded", "failed", "interrupted")


def set_queue_depth(counts: dict[str, int]) -> None:
    """Set the QUEUE_DEPTH gauge for every known state.

    `counts` maps state -> count; missing states are set to 0 so the series
    are always present and monotonic-looking dashboards don't gap.
    """
    for state in _QUEUE_STATES:
        QUEUE_DEPTH.labels(state=state).set(counts.get(state, 0))


def render_metrics() -> tuple[bytes, str]:
    """Return (body, content_type) for the /metrics endpoint."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
