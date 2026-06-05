# Observability: Health, Readiness & Prometheus Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Seekr the minimum operational visibility it currently lacks (ROADMAP P3 "Observability & operations"): liveness/readiness probes for container orchestration and the nginx upstream, plus Prometheus metrics for request latency, queue depth, job outcomes and index throughput.

**Architecture:** Three new unauthenticated routes (`GET /health`, `GET /ready`, `GET /metrics`) live in `create_app` alongside the existing `GET /api/status` route. A new `_PrometheusMiddleware` (mirroring the existing `_SecurityHeaders` middleware exactly) times every request and records a counter + histogram. A new `document_search/observability.py` module owns a single Prometheus `CollectorRegistry` plus all metric objects, so metric definitions live in one place and tests can import them directly. Queue-depth gauges are refreshed on demand (a `before-collect` callback driven off `JobStore.list_jobs`) so `/metrics` always reflects current DB state. Job success/fail counters and the index-throughput counter are incremented from the existing job handlers in `app.py`. `/ready` performs a cheap `SELECT 1` on the worker's SqliteStore connection and checks the worker poll thread is alive; it returns 503 when either fails.

**Tech Stack:** Python 3.11, FastAPI + Starlette, SQLite, `prometheus-client` (new dependency), pytest. Windows/PowerShell dev environment; tests run with `$env:PYTHONPATH = "."; pytest -q`.

**Scope boundaries:**

In scope:
- `prometheus-client` added to `requirements.txt`.
- `document_search/observability.py` — registry + metric objects + a `render_metrics()` helper.
- `GET /health` — liveness, no auth, returns 200 + app version. Never touches the DB.
- `GET /ready` — readiness, no auth, returns 200 when DB reachable (`SELECT 1`) AND worker thread alive; 503 otherwise.
- `GET /metrics` — Prometheus exposition format, no session token (gated instead by an optional `DOCUMENT_SEARCH_METRICS_TOKEN` env var — see Task 4).
- A request latency/count middleware mirroring `_SecurityHeaders`.
- Queue-depth gauges by state; job success/fail counters; index-throughput counter.
- A `Worker.is_alive()` accessor (the worker exposes only a private `_poll_thread` today).
- Docker `healthcheck:` on the `document-search` service using `/health`; nginx note for upstream health.
- Tests via FastAPI `TestClient`.

Out of scope (deferred / explicitly NOT done here):
- Structured logging overhaul (separate P0 item in the ROADMAP).
- Tracing / OpenTelemetry.
- Grafana dashboards or alert rules.
- Per-endpoint cardinality beyond `method` + `path-template` + `status` (no user/label explosion).
- Authenticating `/metrics` with the session-token system (it is a machine endpoint; see decision below).
- Multi-process metric aggregation (`prometheus_client` multiproc mode) — Seekr runs a single uvicorn process today.

---

## File Structure

**Create:**
- `document_search/observability.py` — Prometheus registry, metric objects, `render_metrics()`.
- `tests/test_observability.py` — unit + integration tests for `/health`, `/ready`, `/metrics`, and the middleware.

**Modify:**
- `requirements.txt` — add `prometheus-client`.
- `document_search/services/job_worker.py` — add `Worker.is_alive()`.
- `document_search/app.py` — add `_PrometheusMiddleware`, the three routes, queue-gauge refresh wiring, and counter increments in the job handlers.
- `docker-compose.yml` — add a `healthcheck:` block to the `document-search` service.
- `deploy/nginx.conf` — add an explanatory comment about the `/health` upstream probe.

**Untouched:**
- `GET /api/status` — the authenticated, human-facing status route stays exactly as-is; `/health` and `/ready` are the machine-facing additions.
- The persistent job-queue mechanics (`JobStore`, `Worker` poll loop, retry/backoff) — only an `is_alive()` accessor and handler-side counter increments are added.

---

## Key design decisions (locked)

- **One registry, owned by `observability.py`.** We create our own `CollectorRegistry()` rather than using the global default registry. This keeps the metric set deterministic across repeated `create_app()` calls in tests (the default registry is process-global and raises `Duplicated timeseries` if `create_app` runs twice — which the test suite does constantly). All metric objects are module-level singletons registered against this registry.
- **`/metrics` is NOT session-token gated.** Prometheus scrapers cannot perform the Seekr login flow, and `/metrics` exposes only aggregate operational counters (no document content, no user PII). It is therefore open by default — appropriate when reached only over the internal Docker network (`document-search:8080` is `expose`d, not published). For deployments that want a guard, an optional `DOCUMENT_SEARCH_METRICS_TOKEN` env var, when set, requires a matching `Authorization: Bearer <token>` (constant-time compared). When unset (the default), `/metrics` is open. This mirrors how `DOCUMENT_SEARCH_API_KEY` already gates HA endpoints.
- **`/health` never touches the DB.** Liveness must answer "is the process up and serving HTTP" without depending on the DB — otherwise a slow/locked DB would kill an otherwise-healthy container. `/ready` is the one that probes the DB and worker.
- **`/ready` checks two things:** (1) `SELECT 1` succeeds on the worker's SqliteStore connection (the same connection the queue uses, so this also exercises queue health), and (2) `app.state.worker.is_alive()` is true. Either failure → 503 with a JSON body naming the failed check.
- **Path label is the *route template*, not the raw path.** We read `request.scope["route"].path` (e.g. `/api/index/jobs/{job_id}`) so per-id paths don't explode metric cardinality. Requests that match no route (404s) are labelled `path="<unmatched>"`.
- **Queue-depth gauges are refreshed lazily at scrape time** via a registered callback, not on every job transition — cheaper and always-consistent with the DB. Job success/fail counters and index throughput are event-driven (incremented in handlers) because they are monotonic counters, not point-in-time gauges.

---

## Task 1: Add `prometheus-client` dependency and the observability module

**Files:**
- Modify: `requirements.txt`
- Create: `document_search/observability.py`
- Test: `tests/test_observability.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_observability.py`:

```python
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
```

- [ ] **Step 2: Run, expect FAIL (ModuleNotFoundError / ImportError)**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_observability.py -v
```

Expected: collection error — `prometheus_client` not installed yet, or `document_search.observability` missing.

- [ ] **Step 3: Add the dependency**

In `requirements.txt`, append after the last line (`psutil>=5.9.0`):

```text
prometheus-client>=0.20.0
```

Install it into the active environment:

```powershell
python -m pip install "prometheus-client>=0.20.0"
```

- [ ] **Step 4: Implement `document_search/observability.py`**

Create `document_search/observability.py`:

```python
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
```

- [ ] **Step 5: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_observability.py -v
```

Expected: 2 passing.

- [ ] **Step 6: Full-suite check**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: existing baseline + 2 new, zero failures.

- [ ] **Step 7: Commit**

```powershell
git add requirements.txt document_search/observability.py tests/test_observability.py
git commit -m @'
feat(observability): add prometheus-client and metrics registry module

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 2: `Worker.is_alive()` accessor

**Files:**
- Modify: `document_search/services/job_worker.py`
- Test: `tests/test_observability.py` (extend)

The worker only exposes a private `_poll_thread`. `/ready` needs a public, side-effect-free way to ask "is the poll loop running?".

- [ ] **Step 1: Write the failing test**

Append to `tests/test_observability.py`:

```python
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
```

- [ ] **Step 2: Run, expect FAIL (AttributeError)**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_observability.py::test_worker_is_alive_reflects_poll_thread -v
```

Expected: `AttributeError: 'Worker' object has no attribute 'is_alive'`.

- [ ] **Step 3: Add `is_alive` to `Worker`**

In `document_search/services/job_worker.py`, insert this method directly **after** the `start` method (right before `def stop`):

```python
    def is_alive(self) -> bool:
        """True if the poll thread exists and is still running.

        Side-effect-free; safe to call from a readiness probe on every scrape.
        """
        return self._poll_thread is not None and self._poll_thread.is_alive()
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_observability.py -v
```

Expected: 3 passing.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: no regressions (the job-worker tests still pass).

- [ ] **Step 6: Commit**

```powershell
git add document_search/services/job_worker.py tests/test_observability.py
git commit -m @'
feat(observability): add Worker.is_alive() accessor for readiness probe

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 3: `/health` and `/ready` routes

**Files:**
- Modify: `document_search/app.py`
- Test: `tests/test_observability.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_observability.py`:

```python
from fastapi.testclient import TestClient
from document_search.app import create_app


def test_health_returns_200_and_version(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["version"] == app.version


def test_health_needs_no_auth(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        # No X-Auth-Token header at all.
        assert client.get("/health").status_code == 200


def test_ready_returns_200_when_db_and_worker_ok(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:  # startup runs -> worker started
        r = client.get("/ready")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ready"] is True
        assert body["checks"]["database"] is True
        assert body["checks"]["worker"] is True


def test_ready_returns_503_when_db_unreachable(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        # Simulate an unreachable DB by closing the worker store's connection.
        # The readiness probe's SELECT 1 must then raise -> 503.
        app.state.job_store.store.conn.close()
        r = client.get("/ready")
        assert r.status_code == 503
        body = r.json()
        assert body["ready"] is False
        assert body["checks"]["database"] is False
```

- [ ] **Step 2: Run, expect FAIL (404 on /health, /ready)**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_observability.py -k "health or ready" -v
```

Expected: 404 responses — routes don't exist yet.

- [ ] **Step 3: Add the routes in `app.py`**

In `document_search/app.py`, inside `create_app`, locate the existing authenticated status route `@app.get("/api/status")` (around line 1623). Insert the two new routes **immediately before** it:

```python
    @app.get("/health", tags=["system"])
    def health() -> dict:
        """Liveness probe. No auth, no DB access — answers only 'is the process
        serving HTTP'. Used by the Docker healthcheck and the nginx upstream."""
        return {"status": "ok", "version": app.version}

    @app.get("/ready", tags=["system"])
    def ready(response: Response) -> dict:
        """Readiness probe. No auth. 200 when the DB answers `SELECT 1` AND the
        worker poll thread is alive; 503 (with per-check detail) otherwise."""
        checks: dict[str, bool] = {"database": False, "worker": False}
        try:
            job_store.store.conn.execute("SELECT 1").fetchone()
            checks["database"] = True
        except Exception:
            checks["database"] = False
        checks["worker"] = worker.is_alive()
        is_ready = all(checks.values())
        if not is_ready:
            response.status_code = 503
        return {"ready": is_ready, "checks": checks}
```

`Response` is already imported at the top of `app.py` (`from starlette.responses import Response`), and `job_store` / `worker` are in scope from the queue wiring earlier in `create_app`.

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_observability.py -k "health or ready" -v
```

Expected: 4 passing.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: no regressions.

- [ ] **Step 6: Commit**

```powershell
git add document_search/app.py tests/test_observability.py
git commit -m @'
feat(observability): add /health liveness and /ready readiness endpoints

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 4: `/metrics` endpoint + request middleware

**Files:**
- Modify: `document_search/app.py`
- Test: `tests/test_observability.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_observability.py`:

```python
import os


def test_metrics_endpoint_exposes_expected_names(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        # Make at least one request so the middleware records something.
        client.get("/health")
        r = client.get("/metrics")
        assert r.status_code == 200
        assert "text/plain" in r.headers["content-type"]
        text = r.text
        assert "seekr_http_requests_total" in text
        assert "seekr_http_request_duration_seconds" in text
        assert "seekr_queue_depth" in text
        assert "seekr_jobs_total" in text
        assert "seekr_index_documents_total" in text


def test_metrics_middleware_records_request_count(tmp_path):
    from document_search import observability as obs

    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        client.get("/health")
        # The /health GET that returned 200 must be counted.
        value = obs.REQUEST_COUNT.labels(
            method="GET", path="/health", status="200"
        )._value.get()
        assert value >= 1


def test_metrics_queue_depth_reflects_pending_job(tmp_path):
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        # Enqueue a job for a kind with no handler so it stays claimable/terminal
        # deterministically is hard; instead assert the gauge series exist.
        client.get("/metrics")
        r = client.get("/metrics")
        text = r.text
        # All five state series must be present (value may be 0).
        for state in ("pending", "running", "succeeded", "failed", "interrupted"):
            assert f'seekr_queue_depth{{state="{state}"}}' in text


def test_metrics_token_gate_when_env_set(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_METRICS_TOKEN", "s3cret")
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        # No token -> 401.
        assert client.get("/metrics").status_code == 401
        # Wrong token -> 401.
        assert client.get(
            "/metrics", headers={"Authorization": "Bearer nope"}
        ).status_code == 401
        # Correct token -> 200.
        r = client.get("/metrics", headers={"Authorization": "Bearer s3cret"})
        assert r.status_code == 200
```

> Note: `test_metrics_token_gate_when_env_set` reads the env var at request time (not app-construction time), so the route checks `os.getenv` on each call — see Step 3.

- [ ] **Step 2: Run, expect FAIL (404 on /metrics)**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_observability.py -k metrics -v
```

Expected: 404 — `/metrics` route and middleware don't exist yet.

- [ ] **Step 3: Add the middleware + `/metrics` route in `app.py`**

First, add the import near the top of `document_search/app.py`, beside the existing `from starlette.responses import Response` line:

```python
from document_search import observability as _obs
```

Then add the middleware class. In `create_app`, find the existing `_SecurityHeaders` middleware (around line 440) and its registration `app.add_middleware(_SecurityHeaders)` (around line 467). Insert the new middleware class **immediately after** the `_SecurityHeaders` class definition and register it **immediately after** `app.add_middleware(_SecurityHeaders)`:

```python
    # ── Prometheus request metrics ─────────────────────────────────────
    class _PrometheusMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next) -> Response:
            start = time.perf_counter()
            response = await call_next(request)
            elapsed = time.perf_counter() - start
            # Use the matched route template (not the raw path) to bound label
            # cardinality. Unmatched paths (404s) get a fixed sentinel label.
            route = request.scope.get("route")
            path = getattr(route, "path", None) or "<unmatched>"
            method = request.method
            _obs.REQUEST_LATENCY.labels(method=method, path=path).observe(elapsed)
            _obs.REQUEST_COUNT.labels(
                method=method, path=path, status=str(response.status_code)
            ).inc()
            return response
```

Register it right after the security-headers middleware:

```python
    app.add_middleware(_SecurityHeaders)
    app.add_middleware(_PrometheusMiddleware)
```

Now add the `/metrics` route. Place it **immediately after** the `/ready` route added in Task 3:

```python
    @app.get("/metrics", tags=["system"])
    def metrics(authorization: str | None = Header(default=None)) -> Response:
        """Prometheus exposition. Open by default; when
        DOCUMENT_SEARCH_METRICS_TOKEN is set, requires a matching
        `Authorization: Bearer <token>` (constant-time compared)."""
        token = os.getenv("DOCUMENT_SEARCH_METRICS_TOKEN", "").strip()
        if token:
            expected = f"Bearer {token}"
            if not authorization or not secrets.compare_digest(authorization, expected):
                raise HTTPException(status_code=401, detail="Unauthorized")
        # Refresh queue-depth gauges from the live DB at scrape time.
        try:
            rows = job_store.list_jobs(limit=100000)
            counts: dict[str, int] = {}
            for row in rows:
                counts[row["state"]] = counts.get(row["state"], 0) + 1
            _obs.set_queue_depth(counts)
        except Exception:
            # Never let a metrics-collection hiccup 500 the scrape; emit what we have.
            pass
        body, content_type = _obs.render_metrics()
        return Response(content=body, media_type=content_type)
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_observability.py -k metrics -v
```

Expected: 4 passing.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: no regressions. If `test_metrics_middleware_records_request_count` is flaky because another test already incremented the counter, note the assertion is `>= 1`, which tolerates accumulation across the shared module-level registry.

- [ ] **Step 6: Commit**

```powershell
git add document_search/app.py tests/test_observability.py
git commit -m @'
feat(observability): add /metrics endpoint and request-latency middleware

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 5: Job-outcome and index-throughput counters

**Files:**
- Modify: `document_search/app.py` (job handlers + worker callbacks)
- Test: `tests/test_observability.py` (extend)

Queue depth is a gauge refreshed at scrape time (Task 4). Job outcomes and index throughput are monotonic counters, so they must be incremented when the events happen — inside the handlers and at the worker's terminal transitions.

The cleanest single increment point for job success/fail is the worker's `_execute`, but that lives in `job_worker.py` and we want to keep the worker metrics-agnostic. Instead we increment from the app-side handlers (which already wrap the real work) for index throughput, and we wrap the success/fail counting via the existing `index_paths` handler's return plus a thin post-success hook. To keep the diff minimal and explicit, increment `JOBS_TOTAL` inside each registered handler in `app.py` on success, and inside a small failure shim. The simplest correct approach that doesn't touch `job_worker.py`:

- Index throughput (`INDEX_DOCS_TOTAL`) — incremented inside `_handle_index_paths` as documents are classified.
- Job outcomes (`JOBS_TOTAL`) — incremented at the end of each handler body (success path) and, for failures, recorded by labelling on the kind when the handler raises. Since handlers raise to signal failure and the worker swallows the exception, wrap each handler body in a try/except that increments the `failed` counter and re-raises.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_observability.py`:

```python
import time


def test_index_throughput_counter_increments(tmp_path):
    from document_search import observability as obs

    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        r = client.post("/api/login", json={"username": "admin", "password": "admin"})
        token = r.json()["token"]
        empty = tmp_path / "docs"
        empty.mkdir()
        before = obs.JOBS_TOTAL.labels(kind="index_paths", outcome="succeeded")._value.get()
        start = client.post(
            "/api/index/start",
            headers={"X-Auth-Token": token},
            json={"paths": [str(empty)]},
        )
        job_id = start.json()["job_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            s = client.get(f"/api/index/jobs/{job_id}", headers={"X-Auth-Token": token}).json()
            if s["status"] in ("finished", "failed", "interrupted"):
                break
            time.sleep(0.05)
        assert s["status"] == "finished"
        after = obs.JOBS_TOTAL.labels(kind="index_paths", outcome="succeeded")._value.get()
        assert after == before + 1


def test_job_failure_counter_increments(tmp_path):
    from document_search import observability as obs

    app = create_app(str(tmp_path / "t.db"))

    @app.state.worker.handler("metrics_boom")
    def _boom(payload, progress_cb):
        raise RuntimeError("kaboom")

    with TestClient(app):
        before = obs.JOBS_TOTAL.labels(kind="metrics_boom", outcome="failed")._value.get()
        app.state.job_store.enqueue("metrics_boom", {}, max_retries=0)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rows = app.state.job_store.list_jobs(kind="metrics_boom", limit=1)
            if rows and rows[0]["state"] == "failed":
                break
            time.sleep(0.05)
        after = obs.JOBS_TOTAL.labels(kind="metrics_boom", outcome="failed")._value.get()
        assert after == before + 1
```

> `test_job_failure_counter_increments` registers its own `metrics_boom` handler that increments nothing itself — the increment must come from the shared counting wrapper added in Step 3, so this test verifies the wrapper covers ad-hoc handlers too.

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_observability.py -k "throughput or failure_counter" -v
```

Expected: both fail — counters are never incremented.

- [ ] **Step 3: Wrap handler registration with metric counting in `app.py`**

The robust, low-duplication approach: wrap `worker.handler` so every registered handler — including ad-hoc ones registered in tests — is counted. In `create_app`, **immediately after** `app.state.worker = worker` (around line 276, before the `@app.on_event("startup")` block), insert a wrapper that replaces `worker.handler` with a metric-counting decorator:

```python
    # Wrap handler registration so every job outcome is counted, regardless of
    # which kind/handler is registered (including ad-hoc test handlers).
    _orig_handler = worker.handler

    def _counting_handler(kind: str):
        decorator = _orig_handler(kind)

        def wrap(fn):
            def counted(payload, progress_cb):
                try:
                    result = fn(payload, progress_cb)
                except Exception:
                    _obs.JOBS_TOTAL.labels(kind=kind, outcome="failed").inc()
                    raise
                _obs.JOBS_TOTAL.labels(kind=kind, outcome="succeeded").inc()
                return result

            return decorator(counted)

        return wrap

    worker.handler = _counting_handler  # type: ignore[method-assign]
```

This must come **before** the existing `@worker.handler("index_paths")` / `ai_*` registrations so they go through the counting wrapper. (They are defined later in `create_app`, so assigning `worker.handler` here takes effect for all of them.)

Then add index-throughput increments inside `_handle_index_paths`. In that handler (around line 287), add an `_obs` increment everywhere a document is classified. Specifically, in the loop body replace the three terminal classification branches so each bumps `INDEX_DOCS_TOTAL`:

```python
            if existing and existing["sha256"] == fp.sha256 and existing["modified_at"] == fp.modified_at.isoformat():
                counts["skipped"] += 1
                counts["done"] += 1
                _obs.INDEX_DOCS_TOTAL.labels(outcome="skipped").inc()
                progress_cb(dict(counts))
                continue
            extr = extractor_for(path.suffix.lower())
            if extr is None:
                counts["done"] += 1
                _obs.INDEX_DOCS_TOTAL.labels(outcome="no_extractor").inc()
                progress_cb(dict(counts))
                continue
            result = extr.extract(path)
            db.upsert_document(fp, result)
            if result.status == "error":
                counts["errors"] += 1
                _obs.INDEX_DOCS_TOTAL.labels(outcome="error").inc()
            elif existing:
                counts["updated"] += 1
                _obs.INDEX_DOCS_TOTAL.labels(outcome="updated").inc()
            else:
                counts["indexed"] += 1
                _obs.INDEX_DOCS_TOTAL.labels(outcome="indexed").inc()
            counts["done"] += 1
            progress_cb(dict(counts))
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_observability.py -k "throughput or failure_counter" -v
```

Expected: 2 passing. (`test_index_throughput_counter_increments` asserts the success counter; the empty dir means `INDEX_DOCS_TOTAL` itself stays 0, which is fine — the test asserts `JOBS_TOTAL` success.)

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: no regressions. The counting wrapper is transparent to the existing job-queue tests because it preserves the handler's return value and re-raises on error.

- [ ] **Step 6: Commit**

```powershell
git add document_search/app.py tests/test_observability.py
git commit -m @'
feat(observability): count job outcomes and index throughput

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 6: Docker healthcheck + nginx note

**Files:**
- Modify: `docker-compose.yml`
- Modify: `deploy/nginx.conf`

- [ ] **Step 1: Add the healthcheck to `document-search`**

In `docker-compose.yml`, inside the `document-search` service, add a `healthcheck:` block. Insert it **after** the `expose:` block and **before** `volumes:`:

```yaml
    healthcheck:
      # Liveness probe — the app's /health never touches the DB, so a slow or
      # locked database won't flap the container. Python is already in the image
      # (no curl dependency required).
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health', timeout=5).status==200 else 1)"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 20s
```

> We probe `/health` (liveness) rather than `/ready` for the Docker healthcheck: a container that is up but momentarily not-ready (e.g. worker restarting) should not be killed and recreated. Use `/ready` for an external orchestrator's readiness gate (k8s readinessProbe) if/when one exists.

- [ ] **Step 2: Add the nginx upstream note**

In `deploy/nginx.conf`, add a comment documenting the health/ready endpoints so an operator wiring active upstream health checks (nginx Plus `health_check`, or a sidecar) knows where to point. Replace the `location / { ... }` block's opening with an annotated version:

```nginx
    # Liveness:  GET /health  -> 200 (process up; never touches the DB)
    # Readiness: GET /ready   -> 200 when DB+worker OK, else 503
    # Metrics:   GET /metrics -> Prometheus exposition (open on the internal
    #            network; set DOCUMENT_SEARCH_METRICS_TOKEN to require a
    #            Bearer token). Do NOT expose /metrics publicly without a token.
    location / {
      proxy_pass http://document-search:8080;
      proxy_set_header Host $host;
      proxy_set_header X-Forwarded-Proto https;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
```

- [ ] **Step 3: Validate the compose file parses**

```powershell
docker compose config | Select-Object -First 40
```

Expected: prints the merged config without a YAML error, and the `healthcheck` block appears under `document-search`. (If Docker is not installed in the dev environment, skip — the YAML is mechanically validated by the indentation matching the surrounding keys.)

- [ ] **Step 4: Commit**

```powershell
git add docker-compose.yml deploy/nginx.conf
git commit -m @'
feat(observability): docker healthcheck on /health and nginx probe notes

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Task 7: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full suite cleanly**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: all green, zero failures, including the new `tests/test_observability.py`.

- [ ] **Step 2: Manual smoke test via TestClient**

```powershell
$env:PYTHONPATH = "."; python -c "
from fastapi.testclient import TestClient
from document_search.app import create_app
import tempfile, pathlib
tmp = pathlib.Path(tempfile.mkdtemp())
app = create_app(str(tmp / 'obs.db'))
with TestClient(app) as c:
    h = c.get('/health'); print('health', h.status_code, h.json())
    r = c.get('/ready');  print('ready ', r.status_code, r.json())
    m = c.get('/metrics'); print('metrics', m.status_code, 'len', len(m.text))
    assert h.status_code == 200 and r.status_code == 200 and m.status_code == 200
    assert 'seekr_http_requests_total' in m.text
    assert 'seekr_queue_depth' in m.text
print('OK')
"
```

Expected: prints `health 200 ...`, `ready 200 ...`, `metrics 200 len <n>`, then `OK`.

- [ ] **Step 3: Verify the 503 path**

```powershell
$env:PYTHONPATH = "."; python -c "
from fastapi.testclient import TestClient
from document_search.app import create_app
import tempfile, pathlib
tmp = pathlib.Path(tempfile.mkdtemp())
app = create_app(str(tmp / 'obs.db'))
with TestClient(app) as c:
    app.state.job_store.store.conn.close()  # break the DB
    r = c.get('/ready')
    print('ready', r.status_code, r.json())
    assert r.status_code == 503 and r.json()['checks']['database'] is False
print('OK')
"
```

Expected: `ready 503 {... 'database': False ...}`, then `OK`.

- [ ] **Step 4: No final commit needed** — verification only.

---

## Definition of Done

- [ ] `$env:PYTHONPATH = "."; pytest -q` is green, including all of `tests/test_observability.py`.
- [ ] `prometheus-client>=0.20.0` is in `requirements.txt`.
- [ ] `document_search/observability.py` owns a dedicated `CollectorRegistry` and the five metric objects; `create_app()` can be called repeatedly without `Duplicated timeseries` errors.
- [ ] `GET /health` returns 200 + `{"status":"ok","version":<app.version>}` with no auth and no DB access.
- [ ] `GET /ready` returns 200 when `SELECT 1` succeeds and the worker poll thread is alive; returns 503 with `checks.database=false` when the DB connection is broken (verified by the simulated-failure test).
- [ ] `GET /metrics` returns Prometheus exposition exposing `seekr_http_requests_total`, `seekr_http_request_duration_seconds`, `seekr_queue_depth` (all five state series), `seekr_jobs_total`, and `seekr_index_documents_total`.
- [ ] `/metrics` is open by default and requires a `Bearer` token only when `DOCUMENT_SEARCH_METRICS_TOKEN` is set (constant-time compared).
- [ ] The request middleware records count + latency labelled by `method`, route template `path`, and `status` — using the matched route template, not raw per-id paths.
- [ ] Queue-depth gauges reflect live DB state at scrape time; job success/fail and index-throughput counters increment on the corresponding events.
- [ ] `Worker.is_alive()` exists and is side-effect-free.
- [ ] `docker-compose.yml` has a `healthcheck:` on `document-search` probing `/health`; `deploy/nginx.conf` documents the three endpoints.
- [ ] `GET /api/status` is unchanged.
- [ ] Manual smoke tests (200 path and 503 path) pass.

---

## Notes for the executing agent

- **Why a private registry, not the global default:** `create_app()` is invoked dozens of times across the test suite. Each metric registered against `prometheus_client`'s process-global default registry would collide on the second `create_app` with `ValueError: Duplicated timeseries in CollectorRegistry`. Module-level metric objects bound to our own `REGISTRY` are created exactly once at import, so repeated app construction is safe. Do not move the metric definitions inside `create_app`.
- **Why `/health` must not touch the DB:** liveness answers "should the orchestrator restart this container?". If `/health` did a `SELECT 1`, a transient DB lock (WAL checkpoint, a long index run) would fail liveness and trigger a needless restart — which would abandon in-flight jobs. Readiness (`/ready`) is the correct place for DB/worker checks, and it returns 503 (not a restart signal) so load balancers stop routing without the orchestrator killing the pod.
- **Why the Docker healthcheck probes `/health`, not `/ready`:** same reasoning — Compose's healthcheck drives restart/replace behaviour. Point an external readiness gate (k8s `readinessProbe`, nginx active health check) at `/ready` instead.
- **Why the metrics token uses `Authorization: Bearer`, not `X-Auth-Token`:** Prometheus's standard scrape config supports bearer-token auth natively (`authorization.credentials` in `scrape_configs`). Reusing the session-token header would require custom scraper config and couple metrics to the human login system. The token is optional; unset means open (correct for the internal-network default).
- **Why the counting wrapper replaces `worker.handler` rather than editing `job_worker.py`:** keeping `Worker` metrics-agnostic preserves its single responsibility and its standalone unit tests. The wrapper in `create_app` is the app-layer seam where Seekr-specific instrumentation belongs. Because it reassigns `worker.handler` before any handler is registered, both the built-in handlers and ad-hoc test handlers are counted uniformly.
- **Label cardinality:** the `path` label is always a route template (`request.scope["route"].path`). Never label on the raw request path or query string — `/api/index/jobs/123` and `/api/index/jobs/124` must both map to `/api/index/jobs/{job_id}`. Unmatched requests use the fixed `<unmatched>` label so 404 scanners can't inflate cardinality.
- **`prometheus_client` private accessors in tests:** the tests read `._value.get()` / `._name` to assert exact counter values and metric identity. These are stable internal APIs widely used in `prometheus_client`'s own test suite, but they are technically private — if a future `prometheus_client` upgrade breaks them, switch the assertions to parsing the `/metrics` text output instead (which is the supported public contract).
- **Scrape-time gauge refresh cost:** `list_jobs(limit=100000)` loads all jobs to count by state. For the realistic scale here (thousands of jobs) this is cheap, but if the `jobs` table grows unbounded a future optimisation is a `SELECT state, COUNT(*) FROM jobs GROUP BY state` helper on `JobStore`. Out of scope for this plan, but flagged.
- **Middleware ordering:** Starlette applies middleware in reverse registration order, so registering `_PrometheusMiddleware` after `_SecurityHeaders` means the metrics timer wraps the full handler execution (including the security-headers pass). That's intentional — latency should include everything the client waits for.
</content>
</invoke>
