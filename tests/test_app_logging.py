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
