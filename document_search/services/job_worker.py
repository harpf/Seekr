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
from typing import Callable

from document_search.services.job_store import JobStore

log = logging.getLogger(__name__)

Handler = Callable[[dict, Callable[[dict], None]], dict | None]


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
        # Serialises all JobStore interactions originating in the worker
        # (claim_next, mark_succeeded, mark_failed, update_progress, and the
        # direct unknown-kind UPDATE). The underlying sqlite3.Connection is
        # configured with check_same_thread=False, but Python's sqlite3 module
        # does not guarantee concurrent `.execute()` calls on the same
        # connection from different threads are safe — they must be serialised.
        self._store_lock = threading.Lock()

    def handler(self, kind: str):
        """Decorator: register a handler for a job kind."""
        def decorator(fn: Handler) -> Handler:
            self._handlers[kind] = fn
            return fn
        return decorator

    def register(self, kind: str, fn: Handler) -> None:
        self._handlers[kind] = fn

    def start(self) -> None:
        if self._poll_thread is not None:
            return
        self._stop_event.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, name="JobWorker", daemon=True)
        self._poll_thread.start()

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
            with self._store_lock:
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
            handler = self._handlers.get(kind)
            if handler is None:
                # Unknown kind is a permanent failure: a worker without the
                # handler will never succeed, so skip the retry path. We write
                # directly rather than calling mark_failed (which would retry
                # while max_retries > retry_count).
                from datetime import UTC, datetime
                msg = f"no handler registered for kind '{kind}'"
                with self._store_lock:
                    self.job_store.conn.execute(
                        "UPDATE jobs SET state='failed', error_message=?, "
                        "finished_at=? WHERE id=?",
                        (msg, datetime.now(tz=UTC).isoformat(), job_id),
                    )
                    self.job_store.conn.commit()
                return
            import json
            payload = json.loads(job["payload_json"])

            def progress_cb(p: dict) -> None:
                with self._store_lock:
                    self.job_store.update_progress(job_id, p)

            try:
                result = handler(payload, progress_cb)
            except Exception as exc:
                log.exception("Handler %s failed for job %s", kind, job_id)
                with self._store_lock:
                    self.job_store.mark_failed(job_id, f"{type(exc).__name__}: {exc}")
                return
            with self._store_lock:
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
