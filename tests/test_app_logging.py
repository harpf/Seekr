import logging
import pytest

pytest.importorskip("fastapi")


def test_app_module_has_logger():
    from document_search import app as app_mod
    assert isinstance(getattr(app_mod, "log", None), logging.Logger)
    assert app_mod.log.name == "document_search.app"


def test_create_app_configures_logging(tmp_path, monkeypatch):
    """Creating the app must call configure_logging (root level reflects env)."""
    monkeypatch.setenv("DOCUMENT_SEARCH_LOG_LEVEL", "WARNING")
    # Clear handlers so basicConfig + setLevel take effect deterministically.
    logging.getLogger().handlers.clear()
    from document_search.app import create_app
    create_app(str(tmp_path / "t.db"))
    assert logging.getLogger().level == logging.WARNING


def test_load_ha_keys_logs_warning_on_malformed_config(tmp_path, monkeypatch, caplog):
    """A corrupt config.json must log a warning, not silently return []."""
    bad_config = tmp_path / "config.json"
    bad_config.write_text("{ this is not valid json", encoding="utf-8")
    monkeypatch.setenv("DOCUMENT_SEARCH_CONFIG_PATH", str(bad_config))

    from document_search.app import create_app
    from fastapi.testclient import TestClient

    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        r = client.post("/api/login", json={"username": "admin", "password": "admin"})
        token = r.json()["token"]
        with caplog.at_level(logging.WARNING, logger="document_search.app"):
            # /api/source-folders triggers the same malformed-config read path
            resp = client.get("/api/source-folders", headers={"X-Auth-Token": token})
        assert resp.status_code == 200          # fallback preserved
        assert resp.json() == []                # empty, as before
    assert any(
        "config" in r.message.lower() for r in caplog.records
    ), f"expected a config-parse warning, got: {[r.message for r in caplog.records]}"


def test_nvidia_smi_absence_logs_warning(tmp_path, monkeypatch, caplog):
    """When nvidia-smi raises (not installed), the block must log a warning and
    still return gpu_info=None — i.e. the system endpoint must not 500."""
    import subprocess as _sp
    from document_search.app import create_app
    from fastapi.testclient import TestClient

    def _boom(*args, **kwargs):
        raise FileNotFoundError("nvidia-smi not found")

    monkeypatch.setattr(_sp, "run", _boom)

    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        token = client.post(
            "/api/login", json={"username": "admin", "password": "admin"}
        ).json()["token"]
        with caplog.at_level(logging.WARNING, logger="document_search.app"):
            r = client.get("/api/ai/system-info", headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        assert r.json().get("gpu") is None
    assert any(
        "nvidia" in rec.message.lower() or "gpu" in rec.message.lower()
        for rec in caplog.records
    ), f"expected a GPU warning, got: {[rec.message for rec in caplog.records]}"


def test_expired_sessions_are_evicted_on_login(tmp_path, monkeypatch):
    """Logging in prunes other expired tokens from the in-memory sessions dict."""
    import time as _time
    from fastapi.testclient import TestClient
    from document_search.app import create_app

    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        # First login -> token A
        a = client.post("/api/login", json={"username": "admin", "password": "admin"}).json()["token"]
        sessions = app.state.sessions  # exposed for testability (added in Step 3)
        assert a in sessions
        # Backdate token A's issued-at to 9 hours ago (TTL is 8h) -> expired
        user_id, _issued, role = sessions[a]
        sessions[a] = (user_id, _time.time() - 9 * 3600, role)
        # Second login -> token B; eviction during login must drop the stale A
        b = client.post("/api/login", json={"username": "admin", "password": "admin"}).json()["token"]
        assert b in sessions
        assert a not in sessions, "expired session A should have been evicted"


def test_sessions_hard_cap_evicts_oldest(tmp_path):
    import time as _time
    from fastapi.testclient import TestClient
    from document_search.app import create_app
    from document_search.app import _SESSION_MAX

    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        sessions = app.state.sessions
        # Fill past the cap with synthetic, non-expired entries
        now = _time.time()
        for i in range(_SESSION_MAX + 5):
            sessions[f"tok{i}"] = (1, now + i, "user")  # ascending issued-at
        # A real login triggers the cap check
        client.post("/api/login", json={"username": "admin", "password": "admin"})
        assert len(sessions) <= _SESSION_MAX + 1  # +1 for the just-issued real token


def test_rate_limit_dict_drops_empty_ip_keys(tmp_path, monkeypatch):
    """An IP whose failures have all aged out must be removed from the tracker."""
    import time as _time
    from fastapi.testclient import TestClient
    from document_search.app import create_app, _login_failures, _RATE_LIMIT_WINDOW

    _login_failures.clear()
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as client:
        # Seed an aged-out failure for a fake IP
        old = _time.time() - (_RATE_LIMIT_WINDOW + 10)
        _login_failures["10.0.0.99"] = [old]
        # Any login attempt runs _check_rate_limit for the *real* client IP, but
        # the eviction sweep must also drop the aged-out fake IP.
        client.post("/api/login", json={"username": "admin", "password": "wrong"})
        assert "10.0.0.99" not in _login_failures, "aged-out IP should be evicted"
