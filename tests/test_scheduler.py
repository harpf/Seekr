import time

import pytest

from document_search.index.sqlite_store import SqliteStore
from document_search.services.job_store import JobStore
from document_search.services.job_worker import Scheduler


@pytest.fixture
def js(tmp_path):
    store = SqliteStore(tmp_path / "test.db")
    return JobStore(store)


def test_scheduler_tick_enqueues_index_paths_job(js):
    def paths_provider():
        return ["/docs/a", "/docs/b"]
    sched = Scheduler(js, paths_provider, interval_s=999, owner_user_id=None)
    enqueued = sched.tick()
    assert enqueued is not None
    job = js.get(enqueued)
    assert job["kind"] == "index_paths"
    import json
    assert json.loads(job["payload_json"])["paths"] == ["/docs/a", "/docs/b"]
    assert job["state"] == "pending"


def test_scheduler_tick_skips_when_no_paths(js):
    sched = Scheduler(js, lambda: [], interval_s=999)
    assert sched.tick() is None
    rows = js.list_jobs(kind="index_paths")
    assert rows == []


def test_scheduler_start_stop_is_clean(js):
    sched = Scheduler(js, lambda: ["/docs/a"], interval_s=0.05, owner_user_id=None)
    sched.start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if js.list_jobs(kind="index_paths"):
            break
        time.sleep(0.02)
    sched.stop()
    assert js.list_jobs(kind="index_paths"), "scheduler should have enqueued at least one job"


def test_app_enables_scheduler_when_configured(tmp_path):
    pytest.importorskip("fastapi")
    import json

    from fastapi.testclient import TestClient

    from document_search.app import create_app

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "scheduled_reindex": 1,
        "source_paths": [{"path": str(tmp_path / "src")}],
    }), encoding="utf-8")
    (tmp_path / "src").mkdir()

    import os
    os.environ["DOCUMENT_SEARCH_CONFIG_PATH"] = str(cfg)
    try:
        app = create_app(str(tmp_path / "t.db"))
        with TestClient(app):
            assert app.state.scheduler is not None
    finally:
        os.environ.pop("DOCUMENT_SEARCH_CONFIG_PATH", None)


def test_app_scheduler_disabled_by_default(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from document_search.app import create_app

    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app):
        assert app.state.scheduler is None
