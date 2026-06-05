# Plugin / Extractor API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let third parties add new file-type extractors to Seekr **without forking the package** (docs/ROADMAP.md P4 "Plugin/extractor API"). Two registration channels: (1) installed Python packages that declare an `importlib.metadata` entry point in the group `seekr.extractors`, and (2) a drop-in local plugins directory (`plugins/extractors/`, overridable via `$SEEKR_PLUGIN_DIR`) whose `*.py` files are auto-imported at startup. Both channels feed a single in-process registry that `extractor_for(suffix)` resolves and that extends the set of `supported_extensions`.

**Architecture:** Today there is **no** central extractor registry — `extractor_for()` is duplicated as a hard-coded `dict` in both `document_search/app.py:50` and `document_search/main.py:23`, and the `TextExtractor` ABC lives in `document_search/extractors/base.py` with no `__init__.py` re-export. This plan creates the missing package façade `document_search/extractors/__init__.py` that:

- Re-exports the public plugin contract (`TextExtractor`, `ExtractionResult`, `ContentBlock`) and a stable `EXTRACTOR_API_VERSION` so plugins can pin against it.
- Holds the one true `_REGISTRY: dict[str, TextExtractor]`, seeded with the built-in extractors.
- Exposes `extractor_for(suffix)`, `supported_extensions()`, `register_extractor(...)`, and `load_plugins()`.
- Discovers plugins via `importlib.metadata.entry_points(group="seekr.extractors")` **and** the drop-in directory, merging them with conflict handling (built-ins win unless the plugin sets `override=True`) and full error isolation (a broken plugin logs a warning and is skipped — it never crashes import or startup).

Both `app.py` and `main.py` are then refactored to import `extractor_for` from the new package instead of their private dicts, so plugin extractors flow through every existing code path (`/api/index/start`, `/api/ha/index`, the `index_paths` worker handler, CLI `cmd_index`). `load_plugins()` is called once at app startup (in `create_app`) and once at CLI startup (in `cmd_index`).

**Tech Stack:** Python 3.11 (`importlib.metadata.entry_points(group=...)` keyword form is stable on 3.10+; `importlib.util.spec_from_file_location` for the drop-in dir). No new third-party dependencies. pytest with `monkeypatch` to inject a fake entry point (tests never install a real distribution). Windows/PowerShell test invocation: `$env:PYTHONPATH = "."; pytest -q`.

**Scope boundaries:**

In scope:
- `document_search/extractors/__init__.py`: public contract re-export + versioned API + central registry + `extractor_for` + `supported_extensions` + `register_extractor` + `load_plugins` (entry points + drop-in dir) + conflict + error isolation.
- Refactor `document_search/app.py` and `document_search/main.py` to consume the package registry instead of their inline dicts.
- One worked example plugin under `plugins/extractors/csv_extractor_example.py` (a real, tiny `.csv` extractor) that is auto-discovered from the drop-in directory.
- Developer docs: `docs/PLUGINS.md` — how to write, package (entry point), or drop in an extractor.
- Tests: discovery via monkeypatched `entry_points`, broken-plugin isolation (caplog), drop-in directory discovery, `supported_extensions` includes the plugin suffix, conflict handling, and that `app.extractor_for`/`main.extractor_for` resolve a plugin suffix end-to-end.

Out of scope (deferred):
- Hot-reload / live re-scan of plugins after startup (discovery runs once).
- Sandboxing or process isolation of plugin code — third-party extractors run **in-process** with full trust (see **Notes for the executing agent**).
- A plugin-management UI or `/api/plugins` endpoint.
- Versioned capability negotiation beyond a single integer `EXTRACTOR_API_VERSION`.
- Plugins for anything other than text extraction (no search/storage/connector plugin hooks here).

---

## File Structure

**Create:**
- `document_search/extractors/__init__.py` — public contract re-export, registry, discovery. The heart of this plan.
- `plugins/extractors/__init__.py` — empty package marker so the drop-in dir is a real (importable) namespace base.
- `plugins/extractors/csv_extractor_example.py` — worked example extractor for `.csv`.
- `docs/PLUGINS.md` — developer documentation.
- `tests/test_extractor_registry.py` — registry + built-in resolution unit tests.
- `tests/test_extractor_plugins.py` — discovery, isolation, conflict, drop-in, end-to-end tests.

**Modify:**
- `document_search/app.py` — delete the inline `_EXTRACTORS` dict + private `extractor_for`; import from `document_search.extractors`; call `load_plugins()` once in `create_app`.
- `document_search/main.py` — delete the inline `extractor_for`; import from `document_search.extractors`; call `load_plugins()` once in `cmd_index`.

**Untouched:**
- `document_search/extractors/base.py` — the ABC is already correct; we re-export it, we do not edit it.
- `document_search/models.py` — `ExtractionResult` / `ContentBlock` re-exported as-is.
- The six built-in extractor modules (`*_extractor.py`) — unchanged.

---

## Key design decisions (locked)

- **Single registry, two consumers.** The duplicated inline dicts in `app.py` and `main.py` are replaced by one registry in the package. This is a prerequisite — without it, a plugin discovered at startup could only reach one of the two call sites. The refactor is intentionally part of this plan, not a separate one, because the plugin API is meaningless if half the pipeline ignores it.
- **Suffix keys are lower-case with a leading dot** (`".csv"`), matching every existing call site (`path.suffix.lower()`). `register_extractor` normalises (`.lstrip(".").lower()` then re-prefixes) so a plugin author who writes `"csv"` or `".CSV"` still works.
- **Conflict policy: built-ins win by default.** If a plugin claims a suffix already owned by a *built-in*, it is skipped with a logged warning **unless** the plugin's entry-point object / drop-in `register()` passes `override=True`. Two plugins claiming the same non-built-in suffix: last writer wins, with a logged warning. This makes a malicious/buggy plugin unable to silently hijack `.pdf`.
- **Error isolation is absolute.** Any exception while importing an entry-point module, loading its object, calling its `register` hook, or importing a drop-in file is caught per-plugin, logged at WARNING with the plugin name, and that plugin is skipped. One broken plugin never prevents the others (or startup) from loading.
- **Idempotent discovery.** `load_plugins()` may be called more than once (app + CLI in the same process during tests); a module-level `_plugins_loaded` flag makes the second call a no-op unless `force=True` is passed (tests use `force=True`).
- **Two plugin shapes are accepted** from both channels:
  1. An object that is a `TextExtractor` **subclass** (the registry instantiates it) or **instance** (used directly). Its claimed suffixes come from an optional `extensions: tuple[str, ...]` / `EXTENSIONS` attribute, or from a `for_suffixes` argument.
  2. A module-level `register(registry)` callable that calls `registry.register_extractor(...)` itself (full control, e.g. one module registering several suffixes). The drop-in example uses shape (1) via a module-level `EXTENSIONS` + class.
- **`EXTRACTOR_API_VERSION = 1`.** Re-exported so a plugin can `from document_search.extractors import EXTRACTOR_API_VERSION` and assert compatibility. We never break v1 within this plan.
- **Drop-in directory default is `<repo>/plugins/extractors`,** resolved relative to the `document_search` package parent, overridable by the `SEEKR_PLUGIN_DIR` env var. Files starting with `_` are ignored (so `__init__.py` and private helpers are skipped).

---

## Task 1: Public contract + central registry (no discovery yet)

**Files:**
- Create: `document_search/extractors/__init__.py`
- Test: `tests/test_extractor_registry.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_extractor_registry.py`:

```python
from pathlib import Path

import pytest

from document_search.extractors import (
    EXTRACTOR_API_VERSION,
    TextExtractor,
    ExtractionResult,
    ContentBlock,
    extractor_for,
    supported_extensions,
    register_extractor,
    BUILTIN_EXTENSIONS,
)


def test_public_contract_is_reexported():
    # The ABC and result types are the public plugin contract.
    assert issubclass(TextExtractor, object)
    assert hasattr(TextExtractor, "extract")
    assert ExtractionResult is not None
    assert ContentBlock is not None
    assert isinstance(EXTRACTOR_API_VERSION, int)
    assert EXTRACTOR_API_VERSION >= 1


@pytest.mark.parametrize(
    "suffix",
    [".pdf", ".docx", ".pptx", ".txt", ".md", ".doc", ".ppt"],
)
def test_builtin_extractor_resolves(suffix):
    ex = extractor_for(suffix)
    assert ex is not None
    assert isinstance(ex, TextExtractor)


def test_extractor_for_is_case_insensitive():
    assert extractor_for(".PDF") is not None
    assert extractor_for(".Md") is not None


def test_extractor_for_unknown_returns_none():
    assert extractor_for(".nope") is None


def test_supported_extensions_lists_builtins():
    exts = supported_extensions()
    assert set(BUILTIN_EXTENSIONS).issubset(set(exts))
    # All entries are lower-case dotted suffixes.
    assert all(e.startswith(".") and e == e.lower() for e in exts)


def test_register_extractor_adds_a_new_suffix():
    class FakeExtractor(TextExtractor):
        def extract(self, file_path: Path) -> ExtractionResult:
            return ExtractionResult(file_path=file_path, status="ok", blocks=[])

    register_extractor(FakeExtractor, for_suffixes=[".fake1"])
    assert ".fake1" in supported_extensions()
    assert isinstance(extractor_for(".fake1"), FakeExtractor)
    # Normalisation: undotted / upper input still resolves to ".fake2"
    register_extractor(FakeExtractor(), for_suffixes=["FAKE2"])
    assert ".fake2" in supported_extensions()
    assert extractor_for(".fake2") is not None
```

- [ ] **Step 2: Run, expect ImportError**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_extractor_registry.py -v
```

Expected: `ImportError` — `document_search.extractors` has no `__init__.py` re-exporting these names.

- [ ] **Step 3: Create the package façade + registry**

Create `document_search/extractors/__init__.py`. This step adds the contract + registry only; discovery (`load_plugins`) is appended in Task 2 but include the full discovery code now so the file is written once — the discovery tests in Task 2 will then pass without re-editing. Write the complete file:

```python
"""Public extractor plugin API + central extractor registry.

Third parties extend Seekr's file-type support **without forking** by providing
a :class:`TextExtractor` subclass and registering it through one of two channels:

1. An installed package that declares an ``importlib.metadata`` entry point in
   the group ``seekr.extractors``::

       # pyproject.toml of the third-party plugin package
       [project.entry-points."seekr.extractors"]
       my_csv = "my_pkg.csv_extractor:CsvExtractor"

2. A drop-in file placed in the local plugins directory
   (``<repo>/plugins/extractors/*.py``, override with ``$SEEKR_PLUGIN_DIR``).

Either channel may expose:

* a :class:`TextExtractor` **subclass** (instantiated by the registry) or
  **instance**, optionally carrying an ``EXTENSIONS`` / ``extensions`` tuple, or
* a module-level ``register(registry)`` callable that calls
  ``registry.register_extractor(...)`` itself.

The public contract (``TextExtractor``, ``ExtractionResult``, ``ContentBlock``)
is re-exported here and versioned via ``EXTRACTOR_API_VERSION`` so a plugin can
pin compatibility.

SECURITY: discovered plugin code runs **in-process with full trust**. Only
install plugins / drop in files you trust. See docs/PLUGINS.md.
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import os
from importlib.metadata import entry_points
from pathlib import Path
from typing import Iterable

from document_search.extractors.base import TextExtractor
from document_search.models import ContentBlock, ExtractionResult

# Built-in extractors.
from document_search.extractors.docx_extractor import DocxTextExtractor
from document_search.extractors.legacy_office_extractor import LegacyOfficeTextExtractor
from document_search.extractors.md_extractor import MdTextExtractor
from document_search.extractors.pdf_extractor import PdfTextExtractor
from document_search.extractors.pptx_extractor import PptxTextExtractor
from document_search.extractors.txt_extractor import TxtTextExtractor

__all__ = [
    "EXTRACTOR_API_VERSION",
    "ENTRY_POINT_GROUP",
    "TextExtractor",
    "ExtractionResult",
    "ContentBlock",
    "extractor_for",
    "supported_extensions",
    "register_extractor",
    "load_plugins",
    "BUILTIN_EXTENSIONS",
]

log = logging.getLogger(__name__)

#: Public, stable contract version. Plugins may assert against this.
EXTRACTOR_API_VERSION = 1

#: importlib.metadata entry-point group third-party packages declare.
ENTRY_POINT_GROUP = "seekr.extractors"

# --- Central registry -------------------------------------------------------

# The one true mapping of dotted-lower suffix -> TextExtractor instance.
_REGISTRY: dict[str, TextExtractor] = {}

# Suffixes owned by a built-in extractor. Built-ins win conflicts unless a
# plugin explicitly passes override=True.
_BUILTIN_SUFFIXES: set[str] = set()

# Guards against re-running discovery on every call.
_plugins_loaded = False


def _normalise_suffix(suffix: str) -> str:
    """'.CSV' / 'csv' / '.csv' -> '.csv'."""
    return "." + suffix.strip().lstrip(".").lower()


def _seed_builtins() -> None:
    builtins: dict[str, TextExtractor] = {
        ".pdf": PdfTextExtractor(),
        ".docx": DocxTextExtractor(),
        ".pptx": PptxTextExtractor(),
        ".txt": TxtTextExtractor(),
        ".md": MdTextExtractor(),
        ".doc": LegacyOfficeTextExtractor(),
        ".ppt": LegacyOfficeTextExtractor(),
    }
    for suffix, instance in builtins.items():
        _REGISTRY[suffix] = instance
        _BUILTIN_SUFFIXES.add(suffix)


_seed_builtins()

#: Snapshot of built-in suffixes for callers/tests.
BUILTIN_EXTENSIONS: tuple[str, ...] = tuple(sorted(_BUILTIN_SUFFIXES))


def register_extractor(
    extractor: TextExtractor | type[TextExtractor],
    for_suffixes: Iterable[str],
    *,
    override: bool = False,
) -> None:
    """Register ``extractor`` for each suffix in ``for_suffixes``.

    ``extractor`` may be a :class:`TextExtractor` subclass (instantiated here)
    or an already-constructed instance. Suffixes are normalised to dotted-lower
    form. A plugin may not replace a built-in suffix unless ``override=True``.
    """
    if isinstance(extractor, type):
        if not issubclass(extractor, TextExtractor):
            raise TypeError(f"{extractor!r} is not a TextExtractor subclass")
        instance = extractor()
    else:
        if not isinstance(extractor, TextExtractor):
            raise TypeError(f"{extractor!r} is not a TextExtractor instance")
        instance = extractor

    for raw in for_suffixes:
        suffix = _normalise_suffix(raw)
        if suffix in _BUILTIN_SUFFIXES and not override:
            log.warning(
                "Plugin extractor %s tried to claim built-in suffix '%s'; "
                "skipped (pass override=True to force).",
                type(instance).__name__,
                suffix,
            )
            continue
        if suffix in _REGISTRY and _REGISTRY[suffix] is not instance:
            log.warning(
                "Suffix '%s' already registered to %s; overwriting with %s.",
                suffix,
                type(_REGISTRY[suffix]).__name__,
                type(instance).__name__,
            )
        _REGISTRY[suffix] = instance


def extractor_for(suffix: str):
    """Return the extractor instance for ``suffix`` (dotted, any case), or None."""
    if not suffix:
        return None
    return _REGISTRY.get(_normalise_suffix(suffix))


def supported_extensions() -> list[str]:
    """All registered suffixes (built-ins + plugins), sorted, dotted-lower."""
    return sorted(_REGISTRY)


# --- Discovery --------------------------------------------------------------


def _suffixes_from_object(obj) -> tuple[str, ...]:
    """Pull declared extensions off an extractor object/class.

    Looks for ``EXTENSIONS`` then ``extensions`` (tuple/list of suffix strings).
    Returns an empty tuple if none declared.
    """
    for attr in ("EXTENSIONS", "extensions"):
        value = getattr(obj, attr, None)
        if value:
            return tuple(value)
    return ()


def _register_from_object(obj, *, source: str) -> None:
    """Register a plugin-provided object loaded from an entry point or drop-in.

    Accepts a module exposing ``register(registry)``, a TextExtractor subclass,
    or a TextExtractor instance.
    """
    register_hook = getattr(obj, "register", None)
    is_extractor = (isinstance(obj, type) and issubclass(obj, TextExtractor)) or isinstance(
        obj, TextExtractor
    )
    # A TextExtractor subclass/instance takes precedence over a stray
    # ``register`` attribute (subclasses inherit no register hook by default).
    if is_extractor:
        suffixes = _suffixes_from_object(obj)
        if not suffixes:
            log.warning(
                "Plugin %s exposes an extractor with no EXTENSIONS declared; skipped.",
                source,
            )
            return
        override = bool(getattr(obj, "OVERRIDE_BUILTIN", False))
        register_extractor(obj, for_suffixes=suffixes, override=override)
        log.info("Registered plugin extractor from %s for %s", source, list(suffixes))
        return
    if callable(register_hook):
        register_hook(_registry_facade)
        log.info("Ran plugin register() hook from %s", source)
        return
    log.warning(
        "Plugin %s is neither a TextExtractor nor exposes register(registry); skipped.",
        source,
    )


class _RegistryFacade:
    """The object passed to a plugin's ``register(registry)`` hook."""

    register_extractor = staticmethod(register_extractor)
    extractor_for = staticmethod(extractor_for)
    supported_extensions = staticmethod(supported_extensions)
    EXTRACTOR_API_VERSION = EXTRACTOR_API_VERSION


_registry_facade = _RegistryFacade()


def _load_entry_point_plugins() -> None:
    try:
        eps = entry_points(group=ENTRY_POINT_GROUP)
    except Exception:  # pragma: no cover - defensive; entry_points is robust
        log.exception("Failed to enumerate '%s' entry points", ENTRY_POINT_GROUP)
        return
    for ep in eps:
        try:
            obj = ep.load()
            _register_from_object(obj, source=f"entry-point '{ep.name}'")
        except Exception:
            log.warning(
                "Skipping broken extractor plugin entry-point '%s'", ep.name, exc_info=True
            )


def _plugin_dir() -> Path:
    override = os.environ.get("SEEKR_PLUGIN_DIR")
    if override:
        return Path(override)
    # <repo>/plugins/extractors — document_search/__init__.py's parent's parent.
    return Path(__file__).resolve().parents[2] / "plugins" / "extractors"


def _load_dropin_plugins() -> None:
    plugin_dir = _plugin_dir()
    if not plugin_dir.is_dir():
        return
    for file in sorted(plugin_dir.glob("*.py")):
        if file.name.startswith("_"):
            continue
        mod_name = f"seekr_plugin_{file.stem}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, file)
            if spec is None or spec.loader is None:
                log.warning("Could not build import spec for drop-in plugin %s", file)
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception:
            log.warning(
                "Skipping broken drop-in extractor plugin '%s'", file.name, exc_info=True
            )
            continue
        # A drop-in may expose a register() hook, an EXTENSIONS+class, or one or
        # more TextExtractor subclasses with their own EXTENSIONS.
        if callable(getattr(module, "register", None)):
            try:
                module.register(_registry_facade)
                log.info("Ran register() from drop-in plugin %s", file.name)
            except Exception:
                log.warning(
                    "register() failed in drop-in plugin '%s'", file.name, exc_info=True
                )
            continue
        registered_any = False
        for attr in vars(module).values():
            if isinstance(attr, type) and issubclass(attr, TextExtractor) and attr is not TextExtractor:
                suffixes = _suffixes_from_object(attr)
                if not suffixes:
                    continue
                try:
                    override = bool(getattr(attr, "OVERRIDE_BUILTIN", False))
                    register_extractor(attr, for_suffixes=suffixes, override=override)
                    registered_any = True
                    log.info(
                        "Registered drop-in extractor %s for %s",
                        attr.__name__,
                        list(suffixes),
                    )
                except Exception:
                    log.warning(
                        "Failed registering %s from drop-in '%s'",
                        attr.__name__,
                        file.name,
                        exc_info=True,
                    )
        if not registered_any:
            log.warning(
                "Drop-in plugin '%s' registered no extractors "
                "(no register() and no TextExtractor subclass with EXTENSIONS).",
                file.name,
            )


def load_plugins(*, force: bool = False) -> list[str]:
    """Discover + register all plugin extractors. Idempotent unless ``force``.

    Returns the full sorted list of supported suffixes after discovery.
    Never raises: each plugin is isolated; failures are logged and skipped.
    """
    global _plugins_loaded
    if _plugins_loaded and not force:
        return supported_extensions()
    _load_entry_point_plugins()
    _load_dropin_plugins()
    _plugins_loaded = True
    return supported_extensions()
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_extractor_registry.py -v
```

Expected: all tests pass (7 cases incl. the parametrized built-ins).

- [ ] **Step 5: Full-suite sanity (nothing else should break yet)**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: previous baseline + new registry tests, zero failures. (`app.py`/`main.py` still use their own inline dicts at this point — refactored in Task 3.)

- [ ] **Step 6: Commit**

```powershell
git add document_search/extractors/__init__.py tests/test_extractor_registry.py
git commit -m "feat(extractors): central registry + versioned public plugin contract"
```

---

## Task 2: Plugin discovery — entry points, drop-in dir, conflicts, isolation

**Files:**
- (Discovery code was written in Task 1 Step 3 — no source change here.)
- Test: `tests/test_extractor_plugins.py` (new)

- [ ] **Step 1: Write the failing/behaviour tests**

Create `tests/test_extractor_plugins.py`. These tests build a **fake entry point** and monkeypatch `document_search.extractors.entry_points` so no real distribution is installed, exercise the drop-in directory via `tmp_path` + `$SEEKR_PLUGIN_DIR`, and assert broken plugins are skipped with a logged warning (caplog).

```python
import logging
import textwrap
from pathlib import Path

import pytest

import document_search.extractors as ext
from document_search.extractors import (
    TextExtractor,
    ExtractionResult,
    ContentBlock,
    extractor_for,
    supported_extensions,
    load_plugins,
)


@pytest.fixture(autouse=True)
def _reset_registry(monkeypatch):
    """Each test starts from a clean registry of only the built-ins.

    We snapshot and restore the module-level registry state so tests don't
    leak plugin registrations into one another.
    """
    saved_registry = dict(ext._REGISTRY)
    saved_loaded = ext._plugins_loaded
    yield
    ext._REGISTRY.clear()
    ext._REGISTRY.update(saved_registry)
    ext._plugins_loaded = saved_loaded


# --- Fakes ------------------------------------------------------------------


class _GoodCsvExtractor(TextExtractor):
    EXTENSIONS = (".csv",)

    def extract(self, file_path: Path) -> ExtractionResult:
        text = file_path.read_text(encoding="utf-8", errors="ignore").strip()
        block = ContentBlock("csv", 1, text, self.__class__.__name__, {})
        return ExtractionResult(file_path=file_path, status="ok", blocks=[block])


class _PdfHijackExtractor(TextExtractor):
    EXTENSIONS = (".pdf",)  # tries to steal a built-in suffix

    def extract(self, file_path: Path) -> ExtractionResult:  # pragma: no cover
        return ExtractionResult(file_path=file_path, status="ok", blocks=[])


class _FakeEntryPoint:
    """Mimics importlib.metadata.EntryPoint's .name + .load()."""

    def __init__(self, name, loaded_obj=None, load_error=None):
        self.name = name
        self._obj = loaded_obj
        self._error = load_error

    def load(self):
        if self._error is not None:
            raise self._error
        return self._obj


def _patch_entry_points(monkeypatch, eps):
    def fake_entry_points(*, group):
        assert group == "seekr.extractors"
        return list(eps)

    monkeypatch.setattr(ext, "entry_points", fake_entry_points)


# --- Entry-point discovery --------------------------------------------------


def test_entrypoint_plugin_is_discovered_and_used(monkeypatch):
    _patch_entry_points(
        monkeypatch, [_FakeEntryPoint("good_csv", loaded_obj=_GoodCsvExtractor)]
    )
    # No drop-in dir for this test.
    monkeypatch.setattr(ext, "_load_dropin_plugins", lambda: None)

    suffixes = load_plugins(force=True)
    assert ".csv" in suffixes
    assert ".csv" in supported_extensions()
    resolved = extractor_for(".csv")
    assert isinstance(resolved, _GoodCsvExtractor)


def test_broken_entrypoint_is_skipped_with_warning(monkeypatch, caplog):
    good = _FakeEntryPoint("good_csv", loaded_obj=_GoodCsvExtractor)
    broken = _FakeEntryPoint("boom", load_error=RuntimeError("kaboom"))
    _patch_entry_points(monkeypatch, [broken, good])
    monkeypatch.setattr(ext, "_load_dropin_plugins", lambda: None)

    with caplog.at_level(logging.WARNING, logger="document_search.extractors"):
        load_plugins(force=True)

    # The good plugin still loaded despite the broken one.
    assert extractor_for(".csv") is not None
    # The broken one was logged and skipped.
    assert any("boom" in rec.getMessage() for rec in caplog.records)
    assert any("Skipping broken extractor plugin" in rec.getMessage() for rec in caplog.records)


def test_plugin_cannot_hijack_builtin_suffix(monkeypatch, caplog):
    _patch_entry_points(
        monkeypatch, [_FakeEntryPoint("hijack", loaded_obj=_PdfHijackExtractor)]
    )
    monkeypatch.setattr(ext, "_load_dropin_plugins", lambda: None)

    builtin_pdf = extractor_for(".pdf")
    with caplog.at_level(logging.WARNING, logger="document_search.extractors"):
        load_plugins(force=True)

    # .pdf still resolves to the original built-in, not the hijacker.
    assert extractor_for(".pdf") is builtin_pdf
    assert not isinstance(extractor_for(".pdf"), _PdfHijackExtractor)
    assert any("built-in suffix" in rec.getMessage() for rec in caplog.records)


def test_plugin_register_hook_shape(monkeypatch):
    """A plugin module exposing register(registry) controls its own registration."""

    class _Module:
        @staticmethod
        def register(registry):
            registry.register_extractor(_GoodCsvExtractor, for_suffixes=[".csv2"])

    _patch_entry_points(
        monkeypatch, [_FakeEntryPoint("via_hook", loaded_obj=_Module())]
    )
    monkeypatch.setattr(ext, "_load_dropin_plugins", lambda: None)

    load_plugins(force=True)
    assert ".csv2" in supported_extensions()


# --- Drop-in directory discovery -------------------------------------------


def _write_plugin(dir_path: Path, name: str, body: str) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    file = dir_path / name
    file.write_text(textwrap.dedent(body), encoding="utf-8")
    return file


def test_dropin_directory_plugin_is_discovered(monkeypatch, tmp_path):
    plugin_dir = tmp_path / "extractors"
    _write_plugin(
        plugin_dir,
        "myrtf.py",
        """
        from pathlib import Path
        from document_search.extractors import TextExtractor, ExtractionResult, ContentBlock

        class RtfExtractor(TextExtractor):
            EXTENSIONS = (".rtf",)
            def extract(self, file_path: Path) -> ExtractionResult:
                text = file_path.read_text(encoding="utf-8", errors="ignore")
                return ExtractionResult(
                    file_path=file_path, status="ok",
                    blocks=[ContentBlock("rtf", 1, text, "RtfExtractor", {})],
                )
        """,
    )
    monkeypatch.setenv("SEEKR_PLUGIN_DIR", str(plugin_dir))
    # Skip entry points for an isolated drop-in test.
    monkeypatch.setattr(ext, "_load_entry_point_plugins", lambda: None)

    load_plugins(force=True)
    assert ".rtf" in supported_extensions()
    assert extractor_for(".rtf") is not None


def test_broken_dropin_file_is_skipped_with_warning(monkeypatch, tmp_path, caplog):
    plugin_dir = tmp_path / "extractors"
    _write_plugin(plugin_dir, "broken.py", "this is not valid python :::\n")
    _write_plugin(
        plugin_dir,
        "ok.py",
        """
        from pathlib import Path
        from document_search.extractors import TextExtractor, ExtractionResult

        class OkExtractor(TextExtractor):
            EXTENSIONS = (".okx",)
            def extract(self, file_path: Path) -> ExtractionResult:
                return ExtractionResult(file_path=file_path, status="ok", blocks=[])
        """,
    )
    monkeypatch.setenv("SEEKR_PLUGIN_DIR", str(plugin_dir))
    monkeypatch.setattr(ext, "_load_entry_point_plugins", lambda: None)

    with caplog.at_level(logging.WARNING, logger="document_search.extractors"):
        load_plugins(force=True)

    # The valid file still registered.
    assert ".okx" in supported_extensions()
    # The broken one was logged + skipped.
    assert any("Skipping broken drop-in" in rec.getMessage() for rec in caplog.records)


def test_underscore_dropin_files_are_ignored(monkeypatch, tmp_path):
    plugin_dir = tmp_path / "extractors"
    _write_plugin(plugin_dir, "__init__.py", "")
    _write_plugin(
        plugin_dir,
        "_private.py",
        "raise RuntimeError('should never be imported')\n",
    )
    monkeypatch.setenv("SEEKR_PLUGIN_DIR", str(plugin_dir))
    monkeypatch.setattr(ext, "_load_entry_point_plugins", lambda: None)

    # Must not raise — the _private.py file is skipped.
    load_plugins(force=True)


def test_load_plugins_is_idempotent(monkeypatch):
    calls = {"n": 0}

    def counting_eps(*, group):
        calls["n"] += 1
        return []

    monkeypatch.setattr(ext, "entry_points", counting_eps)
    monkeypatch.setattr(ext, "_load_dropin_plugins", lambda: None)

    ext._plugins_loaded = False
    load_plugins()  # first call runs discovery
    load_plugins()  # second call is a no-op
    assert calls["n"] == 1
    load_plugins(force=True)  # force re-runs
    assert calls["n"] == 2
```

- [ ] **Step 2: Run, expect PASS** (discovery code already exists from Task 1)

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_extractor_plugins.py -v
```

Expected: all discovery, conflict, isolation, drop-in, and idempotency tests pass.

If a test that uses `$SEEKR_PLUGIN_DIR` unexpectedly also picks up the real `plugins/extractors/csv_extractor_example.py`, that's fine for assertions about `.csv`, but these tests override `SEEKR_PLUGIN_DIR` to `tmp_path`, so the real dir isn't scanned. Confirm the override path is honoured by `_plugin_dir()`.

- [ ] **Step 3: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: zero failures.

- [ ] **Step 4: Commit**

```powershell
git add tests/test_extractor_plugins.py
git commit -m "feat(extractors): plugin discovery via entry points + drop-in dir with isolation"
```

---

## Task 3: Route the whole pipeline through the registry (refactor app.py + main.py)

**Files:**
- Modify: `document_search/app.py` (remove inline `_EXTRACTORS`/`extractor_for`; import from package; call `load_plugins()` in `create_app`)
- Modify: `document_search/main.py` (remove inline `extractor_for`; import from package; call `load_plugins()` in `cmd_index`)
- Test: `tests/test_extractor_plugins.py` (extend with end-to-end resolution through both modules)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_extractor_plugins.py`:

```python
def test_app_extractor_for_resolves_plugin(monkeypatch):
    """After load_plugins, document_search.app.extractor_for resolves a plugin
    suffix (proves app.py routes through the shared registry, not a private dict)."""
    import document_search.app as app_mod

    _patch_entry_points(
        monkeypatch, [_FakeEntryPoint("good_csv", loaded_obj=_GoodCsvExtractor)]
    )
    monkeypatch.setattr(ext, "_load_dropin_plugins", lambda: None)
    load_plugins(force=True)

    assert isinstance(app_mod.extractor_for(".csv"), _GoodCsvExtractor)
    # And app.py must NOT carry a private hard-coded dict anymore.
    assert not hasattr(app_mod, "_EXTRACTORS")


def test_main_extractor_for_resolves_plugin(monkeypatch):
    import document_search.main as main_mod

    _patch_entry_points(
        monkeypatch, [_FakeEntryPoint("good_csv", loaded_obj=_GoodCsvExtractor)]
    )
    monkeypatch.setattr(ext, "_load_dropin_plugins", lambda: None)
    load_plugins(force=True)

    assert isinstance(main_mod.extractor_for(".csv"), _GoodCsvExtractor)
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_extractor_plugins.py::test_app_extractor_for_resolves_plugin tests/test_extractor_plugins.py::test_main_extractor_for_resolves_plugin -v
```

Expected: FAIL — `app.py` still defines its own `_EXTRACTORS` dict + `extractor_for` that never sees the plugin; `main.py` likewise.

- [ ] **Step 3: Refactor `app.py`**

In `document_search/app.py`, **replace** the six per-extractor imports and the inline registry. Delete these lines (currently at `app.py:37-42`):

```python
from document_search.extractors.docx_extractor import DocxTextExtractor
from document_search.extractors.md_extractor import MdTextExtractor
from document_search.extractors.legacy_office_extractor import LegacyOfficeTextExtractor
from document_search.extractors.pdf_extractor import PdfTextExtractor
from document_search.extractors.pptx_extractor import PptxTextExtractor
from document_search.extractors.txt_extractor import TxtTextExtractor
```

and replace them with a single import:

```python
from document_search.extractors import extractor_for, load_plugins, supported_extensions
```

Then **delete** the inline registry + private function (currently `app.py:49-61`):

```python
# Singletons — instantiated once at import time, not on every request.
_EXTRACTORS: dict[str, object] = {
    ".pdf":  PdfTextExtractor(),
    ".docx": DocxTextExtractor(),
    ".pptx": PptxTextExtractor(),
    ".txt":  TxtTextExtractor(),
    ".md":   MdTextExtractor(),
    ".doc":  LegacyOfficeTextExtractor(),
    ".ppt":  LegacyOfficeTextExtractor(),
}

def extractor_for(ext: str):
    return _EXTRACTORS.get(ext)
```

(The module-level `extractor_for` is now imported from the package, so the rest of `app.py` — `app.py:313`, `:632`, `:700` — keeps calling `extractor_for(...)` unchanged.)

Now call discovery once at startup. In `create_app` (defined at `app.py:248`), immediately after the function's opening lines that build `app = FastAPI(...)`, add the discovery call. Find the existing `organizer = AiOrganizer()` line (the same anchor the job-queue plan used) and insert **before** it:

```python
    # Discover third-party + drop-in extractor plugins once per process.
    load_plugins()
```

`load_plugins()` is idempotent, so calling it from `create_app` (possibly several times across tests) is safe.

- [ ] **Step 4: Refactor `main.py`**

In `document_search/main.py`, delete the six per-extractor imports (`main.py:10-15`) and replace with:

```python
from document_search.extractors import extractor_for, load_plugins
```

Then **delete** the inline `extractor_for` function (`main.py:23-32`):

```python
def extractor_for(ext: str):
    return {
        ".pdf": PdfTextExtractor(),
        ".docx": DocxTextExtractor(),
        ".pptx": PptxTextExtractor(),
        ".txt": TxtTextExtractor(),
        ".md": MdTextExtractor(),
        ".doc": LegacyOfficeTextExtractor(),
        ".ppt": LegacyOfficeTextExtractor(),
    }.get(ext)
```

Now call discovery at the start of `cmd_index` (`main.py:35`). Insert as the **first** statement inside `cmd_index`:

```python
def cmd_index(args):
    load_plugins()
    cfg = load_config(Path(args.config) if args.config else None)
```

(Leave the rest of `cmd_index` unchanged — it already calls `extractor_for(path.suffix.lower())`.)

- [ ] **Step 5: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_extractor_plugins.py -v
```

Expected: the two new end-to-end tests pass; all earlier ones still pass.

- [ ] **Step 6: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: zero failures. The existing `tests/test_app_search.py` (which drives `/api/index/start`) must still pass because `extractor_for` behaves identically for built-in suffixes. If any test imported `document_search.app._EXTRACTORS` directly, update it to use `document_search.extractors.supported_extensions()` — grep first:

```powershell
$env:PYTHONPATH = "."; Select-String -Path tests/*.py -Pattern "_EXTRACTORS"
```

Expected: no matches (nothing referenced the private dict).

- [ ] **Step 7: Commit**

```powershell
git add document_search/app.py document_search/main.py tests/test_extractor_plugins.py
git commit -m "feat(extractors): route app + CLI through the shared plugin registry"
```

---

## Task 4: Worked example plugin (drop-in `.csv` extractor)

**Files:**
- Create: `plugins/extractors/__init__.py`
- Create: `plugins/extractors/csv_extractor_example.py`
- Test: `tests/test_extractor_plugins.py` (extend — load the *real* example via `SEEKR_PLUGIN_DIR`)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_extractor_plugins.py`:

```python
def test_real_example_plugin_loads_and_extracts(monkeypatch, tmp_path):
    """The shipped example plugin under plugins/extractors/ is discovered from
    the default drop-in dir and actually extracts a .csv file."""
    repo_root = Path(ext.__file__).resolve().parents[2]
    real_plugin_dir = repo_root / "plugins" / "extractors"
    monkeypatch.setenv("SEEKR_PLUGIN_DIR", str(real_plugin_dir))
    monkeypatch.setattr(ext, "_load_entry_point_plugins", lambda: None)

    load_plugins(force=True)
    assert ".csv" in supported_extensions()

    csv_path = tmp_path / "data.csv"
    csv_path.write_text("name,score\nAlice,10\nBob,7\n", encoding="utf-8")
    extractor = extractor_for(".csv")
    result = extractor.extract(csv_path)
    assert result.status == "ok"
    assert result.blocks, "expected at least one content block"
    joined = " ".join(b.text for b in result.blocks)
    assert "Alice" in joined and "Bob" in joined
    assert "score" in joined
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_extractor_plugins.py::test_real_example_plugin_loads_and_extracts -v
```

Expected: FAIL — `plugins/extractors/csv_extractor_example.py` doesn't exist; `.csv` not in `supported_extensions()`.

- [ ] **Step 3: Create the example plugin**

Create `plugins/extractors/__init__.py` (empty marker):

```python
"""Local drop-in extractor plugins. Files here (except those starting with '_')
are auto-imported at Seekr startup. See docs/PLUGINS.md."""
```

Create `plugins/extractors/csv_extractor_example.py`:

```python
"""Example Seekr extractor plugin: index `.csv` files as searchable text.

This is a *drop-in* plugin: it lives under ``plugins/extractors/`` and is
auto-discovered at startup. To ship the same extractor as an installable
package instead, declare an entry point (see docs/PLUGINS.md)::

    [project.entry-points."seekr.extractors"]
    csv = "your_package.csv_extractor:CsvExtractor"

The registry reads the module-level ``EXTENSIONS`` (or the class's
``EXTENSIONS``) to know which suffixes this extractor claims.
"""
from __future__ import annotations

import csv
from pathlib import Path

# Import the public, versioned contract — never a private internal.
from document_search.extractors import (
    EXTRACTOR_API_VERSION,
    ContentBlock,
    ExtractionResult,
    TextExtractor,
)

assert EXTRACTOR_API_VERSION >= 1, "This plugin requires Seekr extractor API v1+"


class CsvExtractor(TextExtractor):
    """Flatten a CSV into one text block per row (header-prefixed)."""

    EXTENSIONS = (".csv",)

    def extract(self, file_path: Path) -> ExtractionResult:
        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            return ExtractionResult(
                file_path=file_path, status="error", error_message=str(exc)
            )

        reader = csv.reader(text.splitlines())
        rows = list(reader)
        if not rows:
            return ExtractionResult(file_path=file_path, status="ok", blocks=[])

        header = rows[0]
        blocks: list[ContentBlock] = []
        # Block 0: the header line, useful for matching column names.
        blocks.append(
            ContentBlock("csv_header", 1, ", ".join(header), self.__class__.__name__, {})
        )
        for i, row in enumerate(rows[1:], start=2):
            # Render each row as "col: value" pairs so search hits are readable.
            pairs = []
            for col_idx, value in enumerate(row):
                col = header[col_idx] if col_idx < len(header) else f"col{col_idx + 1}"
                pairs.append(f"{col}: {value}")
            line = " | ".join(pairs)
            if line.strip():
                blocks.append(
                    ContentBlock("csv_row", i, line, self.__class__.__name__, {"row": i})
                )

        return ExtractionResult(
            file_path=file_path,
            status="ok",
            document_metadata={"row_count": max(len(rows) - 1, 0), "columns": header},
            blocks=blocks,
        )
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_extractor_plugins.py::test_real_example_plugin_loads_and_extracts -v
```

Expected: PASS — `.csv` registered, `Alice`/`Bob`/`score` present in extracted blocks.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: zero failures. NOTE: because `plugins/extractors/csv_extractor_example.py` now exists in the **default** drop-in dir, any test that calls `load_plugins()` without overriding `SEEKR_PLUGIN_DIR` will register `.csv`. The `_reset_registry` fixture restores the registry after each test, so no cross-test leakage. Confirm `tests/test_extractor_registry.py::test_extractor_for_unknown_returns_none` still passes (it asserts on `.nope`, not `.csv`).

- [ ] **Step 6: Commit**

```powershell
git add plugins/extractors/__init__.py plugins/extractors/csv_extractor_example.py tests/test_extractor_plugins.py
git commit -m "feat(extractors): worked example .csv drop-in plugin"
```

---

## Task 5: Developer documentation

**Files:**
- Create: `docs/PLUGINS.md`

- [ ] **Step 1: Write the docs**

Create `docs/PLUGINS.md`:

````markdown
# Writing Seekr Extractor Plugins

Seekr can index new file types through third-party **extractor plugins** — no
fork required. A plugin provides a `TextExtractor` subclass and declares which
file suffixes it handles. Seekr discovers it at startup and routes matching
files through it automatically (web indexing, the `index_paths` job, and the
CLI `index` command all use the same registry).

> **Security warning:** plugin code runs **in-process with full trust** — same
> privileges as Seekr itself. Only install or drop in extractors you trust.
> There is no sandboxing.

## The contract

Import the public, versioned contract from `document_search.extractors`:

```python
from document_search.extractors import (
    EXTRACTOR_API_VERSION,   # int; pin compatibility (currently 1)
    TextExtractor,           # the ABC you subclass
    ExtractionResult,        # what extract() returns
    ContentBlock,            # one searchable chunk of text
)
```

Implement one method:

```python
from pathlib import Path

class MyExtractor(TextExtractor):
    EXTENSIONS = (".myext",)              # suffixes this plugin claims

    def extract(self, file_path: Path) -> ExtractionResult:
        text = file_path.read_text(encoding="utf-8", errors="ignore")
        block = ContentBlock(
            block_type="myext",           # free-form label
            block_number=1,               # 1-based ordering
            text=text,                    # the searchable text
            extractor=self.__class__.__name__,
            metadata={},                  # optional per-block dict
        )
        return ExtractionResult(file_path=file_path, status="ok", blocks=[block])
```

On failure, return `ExtractionResult(file_path=..., status="error",
error_message="...")` instead of raising — Seekr records the error and moves on.

`EXTENSIONS` may also be a list; suffixes are normalised (`"csv"`, `".CSV"`,
`".csv"` all mean `.csv`).

## Registration channel A — installed package (entry point)

If you ship your extractor as a pip-installable package, declare an entry point
in the `seekr.extractors` group:

```toml
# pyproject.toml of your plugin package
[project]
name = "seekr-csv-extractor"
version = "0.1.0"
dependencies = []           # plus anything your extractor imports

[project.entry-points."seekr.extractors"]
csv = "seekr_csv_extractor.extractor:CsvExtractor"
```

The entry-point value points at either:
- a `TextExtractor` subclass (with `EXTENSIONS`), **or**
- a module exposing `register(registry)`:

```python
def register(registry):
    registry.register_extractor(CsvExtractor, for_suffixes=[".csv"])
    # registry also exposes extractor_for(), supported_extensions(),
    # and EXTRACTOR_API_VERSION.
```

Install it into the same environment as Seekr (`pip install seekr-csv-extractor`).
Seekr enumerates `entry_points(group="seekr.extractors")` at startup.

## Registration channel B — drop-in directory

For local / quick plugins, drop a `.py` file into `plugins/extractors/`
(override the location with the `SEEKR_PLUGIN_DIR` environment variable). Files
whose names start with `_` are ignored.

```
plugins/extractors/
  __init__.py
  csv_extractor_example.py     # <- auto-imported at startup
```

A drop-in file may expose a `TextExtractor` subclass with `EXTENSIONS`, several
such subclasses, or a module-level `register(registry)`. See the shipped
`plugins/extractors/csv_extractor_example.py` for a complete `.csv` example.

## Conflicts and precedence

- **Built-in suffixes win.** A plugin claiming a built-in suffix
  (`.pdf .docx .pptx .txt .md .doc .ppt`) is **skipped with a logged warning**
  unless it sets `OVERRIDE_BUILTIN = True` on the class (or passes
  `override=True` to `register_extractor`). This stops a buggy plugin from
  silently hijacking core formats.
- **Two plugins, same new suffix:** last one wins, with a logged warning.

## Error isolation

A plugin that fails to import, load, or register is **logged at WARNING and
skipped** — it never crashes Seekr's startup, and other plugins still load. Run
Seekr with logging at INFO to see which plugins registered which suffixes.

## Verifying your plugin

```python
from document_search.extractors import load_plugins, supported_extensions, extractor_for
load_plugins(force=True)
print(supported_extensions())          # your suffix should appear
print(extractor_for(".myext"))         # your extractor instance
```
````

- [ ] **Step 2: Sanity-check the doc renders / no broken fences**

```powershell
$env:PYTHONPATH = "."; python -c "import pathlib; t = pathlib.Path('docs/PLUGINS.md').read_text(encoding='utf-8'); assert t.count('```') % 2 == 0, 'unbalanced code fences'; print('PLUGINS.md OK', len(t), 'chars')"
```

Expected: `PLUGINS.md OK <n> chars`.

- [ ] **Step 3: Cross-reference from the roadmap (optional but tidy)**

If `docs/ROADMAP.md:125` lists "Plugin/extractor API" as pending, append a short pointer. Read the surrounding lines first, then change the bullet to reference the docs:

```powershell
$env:PYTHONPATH = "."; Select-String -Path docs/ROADMAP.md -Pattern "Plugin/extractor API"
```

If present, edit that line to: `- **[L] Plugin/extractor API** — implemented; see docs/PLUGINS.md.` (Only if it makes sense in context; skip if the roadmap has a different format.)

- [ ] **Step 4: Commit**

```powershell
git add docs/PLUGINS.md docs/ROADMAP.md
git commit -m "feat(extractors): developer docs for the plugin API"
```

---

## Task 6: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full suite cleanly**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: zero failures, zero errors. Note the final passing count.

- [ ] **Step 2: Smoke test — drop-in `.csv` flows through a real index run**

```powershell
$env:PYTHONPATH = "."; python -c "
from fastapi.testclient import TestClient
from document_search.app import create_app
import tempfile, time, pathlib
tmp = pathlib.Path(tempfile.mkdtemp())
docs = tmp / 'docs'; docs.mkdir()
(docs / 'people.csv').write_text('name,city\nAda,London\nGrace,NYC\n', encoding='utf-8')
app = create_app(str(tmp / 'smoke.db'))
with TestClient(app) as c:
    tok = c.post('/api/login', json={'username':'admin','password':'admin'}).json()['token']
    j = c.post('/api/index/start', headers={'X-Auth-Token': tok}, json={'paths':[str(docs)]}).json()
    for _ in range(60):
        s = c.get(f\"/api/index/jobs/{j['job_id']}\", headers={'X-Auth-Token': tok}).json()
        if s['status'] in ('finished','failed','interrupted'): break
        time.sleep(0.05)
    print('index result =', s)
    assert s['status'] == 'finished', s
    # The .csv was indexed because the example plugin claims .csv.
    assert s['indexed'] >= 1 or s['found'] >= 1, s
print('OK: csv plugin indexed end-to-end')
"
```

Expected: prints an index result with `status=finished` and `found>=1` (the `.csv` was crawled and extracted because the example plugin registered `.csv`). `crawler.iter_documents` only yields suffixes in BOTH `supported_extensions` config AND `DOC_TYPE_MAP`; see the note in **Notes for the executing agent** about why `.csv` may be skipped by the crawler unless config includes it — if `found == 0`, that's the crawler config gate, not the registry, and is expected with default config.

- [ ] **Step 3: Confirm CLI path also sees plugins**

```powershell
$env:PYTHONPATH = "."; python -c "
import document_search.main as m
from document_search.extractors import supported_extensions, load_plugins
load_plugins(force=True)
print('.csv registered:', '.csv' in supported_extensions())
print('main.extractor_for(.csv):', m.extractor_for('.csv'))
assert m.extractor_for('.csv') is not None
print('OK')
"
```

Expected: `.csv registered: True`, a `CsvExtractor` instance, `OK`.

- [ ] **Step 4: No source changes here — no commit**

---

## Definition of Done

- [ ] `$env:PYTHONPATH = "."; pytest -q` is green (registry + plugin tests added, no regressions).
- [ ] `document_search/extractors/__init__.py` exists and re-exports the public contract (`TextExtractor`, `ExtractionResult`, `ContentBlock`) plus `EXTRACTOR_API_VERSION = 1`.
- [ ] A single central `_REGISTRY` is the only extractor map; the inline dicts in `app.py` and `main.py` are removed (grep: no `_EXTRACTORS` anywhere; `extractor_for` defined exactly once, in the package).
- [ ] `load_plugins()` discovers extractors via `importlib.metadata.entry_points(group="seekr.extractors")` AND the `plugins/extractors/` drop-in dir (`$SEEKR_PLUGIN_DIR` override), and is called once at app startup and once at CLI `cmd_index`.
- [ ] Conflict handling: a plugin cannot hijack a built-in suffix without `override=True`; the attempt is logged.
- [ ] Error isolation: a broken entry-point plugin AND a broken drop-in file are each skipped with a logged WARNING; the rest still load; startup never crashes.
- [ ] `supported_extensions()` includes plugin-registered suffixes.
- [ ] A real example plugin (`plugins/extractors/csv_extractor_example.py`) is discovered and extracts `.csv` content end-to-end.
- [ ] `docs/PLUGINS.md` documents both registration channels, the contract, conflicts, isolation, and the trust caveat.
- [ ] Tests prove: monkeypatched-`entry_points` fake plugin is discovered and used by `extractor_for`; broken plugin skipped with caplog warning; `supported_extensions` includes the plugin suffix; `app.extractor_for` and `main.extractor_for` both resolve the plugin.

---

## Notes for the executing agent

- **SECURITY — third-party code runs in-process with full trust.** Both registration channels `exec` plugin code inside the Seekr process: an entry-point plugin runs at `ep.load()`, a drop-in runs at `spec.loader.exec_module(module)`. There is **no** sandbox, no capability restriction, no separate process. A malicious plugin can read the DB, the filesystem, and secrets in `config.json`. This is acceptable because installing a plugin (pip install, or copying a file into `plugins/extractors/`) is already a privileged, deliberate operator action — but it MUST be documented (it is, in `docs/PLUGINS.md` and the module docstring). Do not "improve" this into a false sense of safety with shallow input filtering; real isolation would need a subprocess/RPC boundary, which is explicitly out of scope.
- **Why this plan also refactors `app.py`/`main.py`:** there was no shared registry to begin with — `extractor_for` was copy-pasted into two modules. A plugin API that only one copy honoured would be a bug magnet. Collapsing both onto the package registry is the minimal correct foundation; it's behaviour-preserving for all built-in suffixes (same instances, same dispatch).
- **Crawler gate vs registry gate (important).** Registering an extractor for `.csv` makes `extractor_for(".csv")` return it, but `crawler.iter_documents` only yields files whose suffix is in BOTH `config.supported_extensions` AND the hard-coded `crawler.DOC_TYPE_MAP`. So a brand-new suffix won't be *crawled* from the filesystem until the operator adds it to `supported_extensions` in config (and, today, until `DOC_TYPE_MAP` knows it). That second gate (`DOC_TYPE_MAP`) is a separate hard-coded map and is **out of scope** for this plan — wiring the crawler to ask the registry for its supported suffixes is a natural follow-up. The registry itself, `/api/index/file` single-file paths, and the worked-example test do not depend on the crawler gate. Call this out if a reviewer asks why dropping in a `.csv` plugin doesn't immediately index every `.csv` on disk.
- **`entry_points(group=...)` keyword form** is the modern, stable API on Python 3.10+ (the project targets 3.11). Do not use the deprecated `entry_points()["seekr.extractors"]` dict form — it warns on 3.12. The tests monkeypatch the module-level `entry_points` name, so keep importing it as `from importlib.metadata import entry_points` (not `import importlib.metadata` + attribute access), or the monkeypatch seam in the tests will miss.
- **Idempotency matters for tests.** `create_app` may run many times in one pytest process; `load_plugins()` guards with `_plugins_loaded`. Tests that need a clean slate pass `force=True` and use the `_reset_registry` fixture to snapshot/restore `_REGISTRY`. Don't remove that guard — without it, repeated `create_app` calls would re-scan entry points on every request-less startup and spam logs.
- **Registry mutation is import-time + startup-time only.** There is no lock around `_REGISTRY` because discovery completes before the worker/uvicorn threads start serving, and `extractor_for` is read-only afterwards. If a future plan adds runtime hot-reload, add a lock then.
- **Don't touch `base.py`.** The ABC is already the right contract; this plan only re-exports it. Editing it would risk breaking the existing built-in extractors that import `from document_search.extractors.base import TextExtractor`.
- **Drop-in module names are namespaced** (`seekr_plugin_<stem>`) to avoid colliding with real top-level modules in `sys.modules`. Keep that prefix.
