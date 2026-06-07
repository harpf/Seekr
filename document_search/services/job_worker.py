"""Background worker for the persistent job queue.

Lifecycle:
  worker = Worker(job_store, max_concurrent=4)
  worker.handler("kind")(callable)        # register handlers
  worker.start()                          # spawn poll thread
  ...
  worker.stop()                           # stop poll thread, wait for in-flight

For tests, use `worker.tick()` to manually advance one poll cycle without
running the background thread.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from document_search.services.job_store import JobStore

log = logging.getLogger(__name__)

Handler = Callable[[dict, Callable[[dict], None]], dict | None]


class JobCancelled(Exception):
    """Raised by a handler (or the worker) to signal cooperative cancellation.

    Translated by the worker into the terminal `cancelled` state — never retried.
    """


class Worker:
    def __init__(
        self,
        job_store: JobStore,
        max_concurrent: int = 4,
        poll_interval_s: float = 1.0,
    ):
        self.job_store = job_store
        self.max_concurrent = max_concurrent
        self.poll_interval_s = poll_interval_s
        self._handlers: dict[str, Handler] = {}
        self._semaphore = threading.BoundedSemaphore(max_concurrent)
        self._inflight: set[threading.Thread] = set()
        self._inflight_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._poll_thread: threading.Thread | None = None

    def handler(self, kind: str):
        """Decorator: register a handler for a job kind."""
        def decorator(fn: Handler) -> Handler:
            self._handlers[kind] = fn
            return fn
        return decorator

    def register(self, kind: str, fn: Handler) -> None:
        self._handlers[kind] = fn

    def is_cancelled(self, job_id: int) -> bool:
        """Handlers poll this at checkpoints to support cooperative cancellation."""
        return self.job_store.is_cancel_requested(job_id)

    def start(self) -> None:
        if self._poll_thread is not None:
            return
        self._stop_event.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, name="JobWorker", daemon=True)
        self._poll_thread.start()

    def is_alive(self) -> bool:
        """True if the poll thread exists and is still running.

        Side-effect-free; safe to call from a readiness probe on every scrape.
        """
        return self._poll_thread is not None and self._poll_thread.is_alive()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=timeout)
            self._poll_thread = None
        self.wait_until_idle(timeout=timeout)

    def wait_until_idle(self, timeout: float = 5.0) -> None:
        """Block until all in-flight handlers have finished or timeout."""
        deadline = time.monotonic() + timeout
        while True:
            with self._inflight_lock:
                threads = [t for t in self._inflight if t.is_alive()]
            if not threads:
                return
            if time.monotonic() >= deadline:
                return
            for t in threads:
                t.join(timeout=min(0.1, max(0.0, deadline - time.monotonic())))

    def tick(self) -> int:
        """Process one poll cycle: claim as many jobs as `max_concurrent` allows
        and dispatch each in its own thread. Returns the number dispatched.

        Intended for tests; production code calls `start()` which runs `tick`
        in a loop.
        """
        dispatched = 0
        while True:
            if not self._semaphore.acquire(blocking=False):
                return dispatched
            job = self.job_store.claim_next()
            if job is None:
                self._semaphore.release()
                return dispatched
            t = threading.Thread(target=self._execute, args=(job,), name=f"Job-{job['id']}")
            with self._inflight_lock:
                self._inflight.add(t)
            t.start()
            dispatched += 1

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("Worker poll loop error")
            self._stop_event.wait(self.poll_interval_s)

    def _execute(self, job: dict) -> None:
        try:
            job_id = job["id"]
            kind = job["kind"]
            if self.job_store.is_cancel_requested(job_id):
                self.job_store.mark_cancelled(job_id)
                return
            handler = self._handlers.get(kind)
            if handler is None:
                # Unknown kind is a permanent failure: a worker without the
                # handler will never succeed, so skip the retry path.
                self.job_store.mark_failed_permanent(
                    job_id, f"no handler registered for kind '{kind}'"
                )
                return
            import json
            payload = json.loads(job["payload_json"])

            def progress_cb(p: dict) -> None:
                self.job_store.update_progress(job_id, p)

            try:
                result = handler(payload, progress_cb)
            except JobCancelled:
                log.info("Job %s (%s) cancelled cooperatively", job_id, kind)
                self.job_store.mark_cancelled(job_id)
                return
            except Exception as exc:
                if self.job_store.is_cancel_requested(job_id):
                    log.info(
                        "Job %s (%s) failed after cancel request -> cancelled",
                        job_id,
                        kind,
                    )
                    self.job_store.mark_cancelled(job_id)
                    return
                log.exception("Handler %s failed for job %s", kind, job_id)
                self.job_store.mark_failed(job_id, f"{type(exc).__name__}: {exc}")
                return

            if self.job_store.is_cancel_requested(job_id):
                self.job_store.mark_cancelled(job_id)
                return
            self.job_store.mark_succeeded(job_id, result if isinstance(result, dict) else None)
        finally:
            with self._inflight_lock:
                self._inflight.discard(threading.current_thread())
            self._semaphore.release()
            # After freeing our slot, try to pick up any waiting jobs. This
            # keeps `wait_until_idle` honest when more pending work exists
            # than `max_concurrent` slots — otherwise queued jobs would only
            # be drained on the next `tick()` or poll-loop iteration.
            try:
                self.tick()
            except Exception:
                log.exception("Worker follow-up tick failed")


class Scheduler:
    """Opt-in periodic enqueuer. Reuses the existing `index_paths` job kind, so
    the already-registered worker handler runs scheduled re-indexes — no new job
    kind, no new infrastructure.

    `paths_provider` is called at each tick so config edits (source_paths) take
    effect without a restart. If it returns an empty list, the tick is a no-op.
    """

    def __init__(
        self,
        job_store: JobStore,
        paths_provider: Callable[[], list[str]],
        interval_s: float,
        owner_user_id: int | None = None,
    ):
        self.job_store = job_store
        self.paths_provider = paths_provider
        self.interval_s = interval_s
        self.owner_user_id = owner_user_id
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> int | None:
        """Enqueue one `index_paths` job covering all configured paths.
        Returns the job id, or None if there are no paths."""
        try:
            paths = [p for p in (self.paths_provider() or []) if p and p.strip()]
        except Exception:
            log.exception("Scheduler paths_provider failed")
            return None
        if not paths:
            return None
        job_id = self.job_store.enqueue(
            "index_paths",
            payload={"paths": paths, "config_path": None},
            owner_user_id=self.owner_user_id,
            max_retries=0,
        )
        log.info("Scheduler enqueued index_paths job %s for %d path(s)", job_id, len(paths))
        return job_id

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="ReindexScheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _loop(self) -> None:
        # Wait one interval before the first run (don't index on every boot).
        while not self._stop_event.wait(self.interval_s):
            try:
                self.tick()
            except Exception:
                log.exception("Scheduler tick error")
