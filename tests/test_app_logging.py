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
