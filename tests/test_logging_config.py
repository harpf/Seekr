import logging
import pytest
from document_search.logging_config import configure_logging


@pytest.fixture(autouse=True)
def _reset_root_logger():
    """Snapshot and restore the root logger so tests don't leak handlers/levels."""
    root = logging.getLogger()
    old_handlers = root.handlers[:]
    old_level = root.level
    yield
    root.handlers[:] = old_handlers
    root.setLevel(old_level)


def test_default_level_is_info(monkeypatch):
    monkeypatch.delenv("DOCUMENT_SEARCH_LOG_LEVEL", raising=False)
    logging.getLogger().handlers.clear()
    configure_logging()
    assert logging.getLogger().level == logging.INFO


def test_env_var_sets_level(monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_LOG_LEVEL", "DEBUG")
    logging.getLogger().handlers.clear()
    configure_logging()
    assert logging.getLogger().level == logging.DEBUG


def test_invalid_level_falls_back_to_info_and_warns(monkeypatch, caplog):
    monkeypatch.setenv("DOCUMENT_SEARCH_LOG_LEVEL", "NOTALEVEL")
    # NB: do NOT clear root handlers here — pytest's caplog relies on its
    # LogCaptureHandler being attached to the root logger, and clearing it
    # would leave caplog.records empty. The autouse fixture restores handlers.
    with caplog.at_level(logging.WARNING):
        configure_logging()
        # Assert inside the context: caplog.at_level() restores the root
        # logger's pre-existing level (WARNING by Python default) on exit,
        # which would mask the INFO level configure_logging() just applied.
        assert logging.getLogger().level == logging.INFO
        assert any("NOTALEVEL" in r.message for r in caplog.records)


def test_idempotent_does_not_add_duplicate_handlers(monkeypatch):
    monkeypatch.delenv("DOCUMENT_SEARCH_LOG_LEVEL", raising=False)
    logging.getLogger().handlers.clear()
    configure_logging()
    n_after_first = len(logging.getLogger().handlers)
    configure_logging()
    n_after_second = len(logging.getLogger().handlers)
    assert n_after_first == n_after_second
