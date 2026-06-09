from __future__ import annotations

import logging
from pathlib import Path

import pytest

import document_search.extractors as ext
from document_search.extractors import (
    ContentBlock,
    ExtractionResult,
    TextExtractor,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    """Snapshot and restore the registry + discovery flag around each test."""
    saved_registry = dict(ext._REGISTRY)
    saved_loaded = ext._plugins_loaded
    try:
        yield
    finally:
        ext._REGISTRY.clear()
        ext._REGISTRY.update(saved_registry)
        ext._plugins_loaded = saved_loaded


class _GoodCsvExtractor(TextExtractor):
    suffixes = (".csv",)

    def extract(self, file_path: Path) -> ExtractionResult:
        return ExtractionResult(
            file_path=file_path,
            status="ok",
            blocks=[
                ContentBlock(
                    block_type="text",
                    block_number=0,
                    text="a,b,c",
                    extractor="csv",
                )
            ],
        )


class _PdfHijackExtractor(TextExtractor):
    suffixes = (".pdf",)

    def extract(self, file_path: Path) -> ExtractionResult:
        return ExtractionResult(file_path=file_path, status="ok", blocks=[])


class _FakeEntryPoint:
    def __init__(self, name: str, value: str, loaded):
        self.name = name
        self.value = value
        self._loaded = loaded

    def load(self):
        if isinstance(self._loaded, Exception):
            raise self._loaded
        return self._loaded


def _patch_entry_points(monkeypatch, eps):
    """Make _load_entry_point_plugins see exactly *eps* and no drop-ins."""

    def fake_entry_points(*, group=None):
        if group == ext.ENTRY_POINT_GROUP:
            return list(eps)
        return []

    monkeypatch.setattr(
        "importlib.metadata.entry_points", fake_entry_points, raising=True
    )
    # Keep drop-in discovery out of entry-point tests.
    monkeypatch.setattr(ext, "_load_dropin_plugins", lambda: None)


def test_entrypoint_plugin_is_discovered_and_used(monkeypatch):
    ep = _FakeEntryPoint("csv", "pkg:_GoodCsvExtractor", _GoodCsvExtractor)
    _patch_entry_points(monkeypatch, [ep])
    ext._plugins_loaded = False

    resolved = ext.extractor_for(".csv")
    assert isinstance(resolved, _GoodCsvExtractor)
    assert ".csv" in ext.supported_extensions()


def test_broken_entrypoint_is_skipped_with_warning(monkeypatch, caplog):
    bad = _FakeEntryPoint("boom", "pkg:boom", RuntimeError("kaboom"))
    good = _FakeEntryPoint("csv", "pkg:_GoodCsvExtractor", _GoodCsvExtractor)
    _patch_entry_points(monkeypatch, [bad, good])
    ext._plugins_loaded = False

    with caplog.at_level(logging.WARNING):
        ext.load_plugins(force=True)

    assert "Skipping broken extractor plugin" in caplog.text
    # The good one is still discovered despite the broken sibling.
    assert isinstance(ext.extractor_for(".csv"), _GoodCsvExtractor)


def test_plugin_cannot_hijack_builtin_suffix(monkeypatch, caplog):
    builtin = ext.extractor_for(".pdf")
    ep = _FakeEntryPoint("hijack", "pkg:_PdfHijackExtractor", _PdfHijackExtractor)
    _patch_entry_points(monkeypatch, [ep])
    ext._plugins_loaded = False

    with caplog.at_level(logging.WARNING):
        ext.load_plugins(force=True)

    # The built-in still owns .pdf; the hijacker was rejected and logged.
    assert ext.extractor_for(".pdf") is builtin
    assert not isinstance(ext.extractor_for(".pdf"), _PdfHijackExtractor)
    assert "Skipping broken extractor plugin" in caplog.text


def test_plugin_register_hook_shape(monkeypatch):
    captured = {}

    def register(register_extractor):
        captured["called"] = True
        register_extractor(".log", _GoodCsvExtractor())

    class _Module:
        pass

    module = _Module()
    module.register = register

    ep = _FakeEntryPoint("hooky", "pkg:module", module)
    _patch_entry_points(monkeypatch, [ep])
    ext._plugins_loaded = False

    ext.load_plugins(force=True)
    assert captured.get("called") is True
    assert isinstance(ext.extractor_for(".log"), _GoodCsvExtractor)


def test_dropin_directory_plugin_is_discovered(monkeypatch, tmp_path):
    plugin_file = tmp_path / "csv_plugin.py"
    plugin_file.write_text(
        "from pathlib import Path\n"
        "from document_search.extractors import (\n"
        "    ContentBlock, ExtractionResult, TextExtractor,\n"
        ")\n"
        "\n"
        "class CsvDropin(TextExtractor):\n"
        "    suffixes = ('.csv',)\n"
        "    def extract(self, file_path: Path) -> ExtractionResult:\n"
        "        return ExtractionResult(file_path=file_path, status='ok', blocks=[])\n"
        "\n"
        "def register(register_extractor):\n"
        "    register_extractor('.csv', CsvDropin())\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ext, "_plugin_dir", lambda: tmp_path)
    monkeypatch.setattr(ext, "_load_entry_point_plugins", lambda: None)
    ext._plugins_loaded = False

    assert ".csv" in ext.supported_extensions()
    assert ext.extractor_for(".csv") is not None


def test_broken_dropin_file_is_skipped_with_warning(monkeypatch, tmp_path, caplog):
    (tmp_path / "broken.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    (tmp_path / "good.py").write_text(
        "from pathlib import Path\n"
        "from document_search.extractors import ExtractionResult, TextExtractor\n"
        "\n"
        "class GoodDropin(TextExtractor):\n"
        "    suffixes = ('.csv',)\n"
        "    def extract(self, file_path: Path) -> ExtractionResult:\n"
        "        return ExtractionResult(file_path=file_path, status='ok', blocks=[])\n"
        "\n"
        "def register(register_extractor):\n"
        "    register_extractor('.csv', GoodDropin())\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ext, "_plugin_dir", lambda: tmp_path)
    monkeypatch.setattr(ext, "_load_entry_point_plugins", lambda: None)
    ext._plugins_loaded = False

    with caplog.at_level(logging.WARNING):
        ext.load_plugins(force=True)

    assert "Skipping broken drop-in" in caplog.text
    assert ext.extractor_for(".csv") is not None


def test_underscore_dropin_files_are_ignored(monkeypatch, tmp_path):
    (tmp_path / "_private.py").write_text(
        "raise RuntimeError('should not be imported')\n", encoding="utf-8"
    )
    monkeypatch.setattr(ext, "_plugin_dir", lambda: tmp_path)
    monkeypatch.setattr(ext, "_load_entry_point_plugins", lambda: None)
    ext._plugins_loaded = False

    # Must not raise: underscore files are skipped entirely.
    ext.load_plugins(force=True)


def test_load_plugins_is_idempotent(monkeypatch):
    calls = {"entry": 0, "dropin": 0}

    def fake_entry():
        calls["entry"] += 1

    def fake_dropin():
        calls["dropin"] += 1

    monkeypatch.setattr(ext, "_load_entry_point_plugins", fake_entry)
    monkeypatch.setattr(ext, "_load_dropin_plugins", fake_dropin)
    ext._plugins_loaded = False

    ext.load_plugins()
    ext.load_plugins()  # no-op, already loaded
    assert calls == {"entry": 1, "dropin": 1}

    ext.load_plugins(force=True)  # explicit re-run
    assert calls == {"entry": 2, "dropin": 2}


def test_app_extractor_for_resolves_plugin(monkeypatch):
    """document_search.app.extractor_for resolves a plugin suffix, proving app.py
    routes through the shared registry rather than a private hard-coded dict."""
    import document_search.app as app_mod

    ep = _FakeEntryPoint("csv", "pkg:_GoodCsvExtractor", _GoodCsvExtractor)
    _patch_entry_points(monkeypatch, [ep])
    ext._plugins_loaded = False

    assert isinstance(app_mod.extractor_for(".csv"), _GoodCsvExtractor)
    # app.py must NOT carry a private hard-coded extractor dict anymore.
    assert not hasattr(app_mod, "_EXTRACTORS")


def test_main_extractor_for_resolves_plugin(monkeypatch):
    """document_search.main.extractor_for resolves a plugin suffix too, proving
    the CLI path shares the same registry."""
    import document_search.main as main_mod

    ep = _FakeEntryPoint("csv", "pkg:_GoodCsvExtractor", _GoodCsvExtractor)
    _patch_entry_points(monkeypatch, [ep])
    ext._plugins_loaded = False

    assert isinstance(main_mod.extractor_for(".csv"), _GoodCsvExtractor)


def test_real_example_plugin_loads_and_extracts(monkeypatch, tmp_path):
    """The shipped example plugin under document_search/extractors/plugins/ is
    discovered from the real drop-in dir and actually extracts a .csv file."""
    monkeypatch.setattr(ext, "_load_entry_point_plugins", lambda: None)
    ext._plugins_loaded = False
    ext.load_plugins(force=True)

    assert ".csv" in ext.supported_extensions()

    csv_path = tmp_path / "data.csv"
    csv_path.write_text("name,score\nAlice,10\nBob,7\n", encoding="utf-8")
    extractor = ext.extractor_for(".csv")
    result = extractor.extract(csv_path)
    assert result.status == "ok"
    assert result.blocks, "expected at least one content block"
    joined = " ".join(b.text for b in result.blocks)
    assert "Alice" in joined and "Bob" in joined
    assert "score" in joined
