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

    from fastapi.testclient import TestClient

    from document_search.app import create_app

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

    from fastapi.testclient import TestClient

    from document_search.app import create_app

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


def test_expired_session_is_rejected_after_backdating_row(tmp_path):
    """An expired session row is purged on read and rejected with 401.

    Session state now lives in the shared `sessions` table (externalised store),
    so eviction is lazy-on-read rather than an in-memory sweep on login.
    """
    import time as _time

    from fastapi.testclient import TestClient

    from document_search.app import create_app
    from document_search.index.sqlite_store import SqliteStore

    db = tmp_path / "t.db"
    app = create_app(str(db))
    with TestClient(app) as client:
        token = client.post(
            "/api/login", json={"username": "admin", "password": "admin"}
        ).json()["token"]
        # Backdate the session's expiry past now -> expired in the shared table.
        s = SqliteStore(db)
        s.conn.execute(
            "UPDATE sessions SET expires_at=? WHERE token=?",
            (_time.time() - 1, token),
        )
        s.conn.commit()
        r = client.get("/api/me", headers={"X-Auth-Token": token})
        assert r.status_code == 401
        # Lazy delete on read: the dead row should be gone.
        row = s.conn.execute(
            "SELECT 1 FROM sessions WHERE token=?", (token,)
        ).fetchone()
        assert row is None


def test_rate_limit_prunes_aged_out_attempts(tmp_path):
    """login_attempts rows older than the window are pruned on the next check.

    The rate-limiter store opportunistically deletes rows beyond the window so
    the table stays small without a separate sweeper.
    """
    import time as _time

    from fastapi.testclient import TestClient

    from document_search.app import _RATE_LIMIT_WINDOW, create_app
    from document_search.index.sqlite_store import SqliteStore

    db = tmp_path / "t.db"
    app = create_app(str(db))
    with TestClient(app) as client:
        # Seed an aged-out failure for a fake IP directly in the shared table.
        s = SqliteStore(db)
        old = _time.time() - (_RATE_LIMIT_WINDOW + 10)
        s.conn.execute(
            "INSERT INTO login_attempts(ip, attempted_at) VALUES(?, ?)",
            ("10.0.0.99", old),
        )
        s.conn.commit()
        # Any login attempt runs _check_rate_limit, whose is_blocked() globally
        # prunes rows older than the window.
        client.post("/api/login", json={"username": "admin", "password": "wrong"})
        remaining = s.conn.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE ip=?", ("10.0.0.99",)
        ).fetchone()[0]
        assert remaining == 0, "aged-out attempt should have been pruned"
