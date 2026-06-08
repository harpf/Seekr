"""Central extractor registry and versioned public plugin contract.

This package is the single entry point for resolving a :class:`TextExtractor`
for a given file suffix. It re-exports the public contract (the
:class:`TextExtractor` ABC plus the :class:`ExtractionResult` / :class:`ContentBlock`
data carriers), seeds the registry with the built-in extractors, and discovers
third-party extractor plugins via two mechanisms:

* Python *entry points* in the ``document_search.extractors`` group.
* Drop-in ``*.py`` files in the local ``plugins`` directory next to this package.

The contract is versioned via :data:`EXTRACTOR_API_VERSION`. A plugin may
expose a ``register(register_extractor)`` hook, or one or more
:class:`TextExtractor` subclasses carrying a ``suffixes`` attribute.

Discovery is best-effort and isolated: a broken plugin is logged and skipped,
never raised, and a plugin may not silently hijack a built-in suffix.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
from collections.abc import Iterable
from pathlib import Path

from document_search.extractors.base import TextExtractor
from document_search.extractors.docx_extractor import DocxTextExtractor
from document_search.extractors.legacy_office_extractor import LegacyOfficeTextExtractor
from document_search.extractors.md_extractor import MdTextExtractor
from document_search.extractors.pdf_extractor import PdfTextExtractor
from document_search.extractors.pptx_extractor import PptxTextExtractor
from document_search.extractors.txt_extractor import TxtTextExtractor
from document_search.models import ContentBlock, ExtractionResult

__all__ = [
    "BUILTIN_EXTENSIONS",
    "EXTRACTOR_API_VERSION",
    "ContentBlock",
    "ExtractionResult",
    "TextExtractor",
    "extractor_for",
    "load_plugins",
    "register_extractor",
    "supported_extensions",
]

logger = logging.getLogger(__name__)

#: Public, stable version of the extractor plugin contract. Bump on breaking
#: changes to the :class:`TextExtractor` / :class:`ExtractionResult` shape.
EXTRACTOR_API_VERSION = 1

#: Entry-point group third-party packages advertise their extractors under.
ENTRY_POINT_GROUP = "document_search.extractors"

# Suffix -> extractor instance. Lower-cased suffixes including the leading dot.
_REGISTRY: dict[str, TextExtractor] = {}

# Guards against discovering plugins more than once per process.
_plugins_loaded = False


def _seed_builtins() -> frozenset[str]:
    """Populate the registry with the built-in extractors and return their suffixes."""
    builtins: dict[str, TextExtractor] = {
        ".pdf": PdfTextExtractor(),
        ".docx": DocxTextExtractor(),
        ".pptx": PptxTextExtractor(),
        ".txt": TxtTextExtractor(),
        ".md": MdTextExtractor(),
        ".doc": LegacyOfficeTextExtractor(),
        ".ppt": LegacyOfficeTextExtractor(),
    }
    _REGISTRY.update(builtins)
    return frozenset(builtins)


#: The set of suffixes provided by the built-in extractors. These may not be
#: hijacked by a plugin unless it passes ``override=True`` explicitly.
BUILTIN_EXTENSIONS = _seed_builtins()


def register_extractor(
    suffix: str,
    extractor: TextExtractor,
    *,
    override: bool = False,
) -> None:
    """Register *extractor* for *suffix* (e.g. ``".pdf"``).

    The suffix is normalised to lower-case and forced to start with a dot. A
    built-in suffix may only be replaced when ``override=True``; otherwise a
    :class:`ValueError` is raised so a plugin cannot silently hijack a built-in.
    """
    if not isinstance(extractor, TextExtractor):
        raise TypeError(
            f"extractor for {suffix!r} must be a TextExtractor, got {type(extractor)!r}"
        )
    norm = suffix.lower()
    if not norm.startswith("."):
        norm = "." + norm
    if not override and norm in BUILTIN_EXTENSIONS:
        raise ValueError(
            f"Refusing to override built-in suffix {norm!r} without override=True"
        )
    _REGISTRY[norm] = extractor


def extractor_for(suffix: str) -> TextExtractor | None:
    """Return the registered extractor for *suffix*, or ``None`` if unknown.

    Matching is case-insensitive. Plugins are discovered lazily on first call.
    """
    load_plugins()
    norm = suffix.lower()
    if not norm.startswith("."):
        norm = "." + norm
    return _REGISTRY.get(norm)


def supported_extensions() -> frozenset[str]:
    """Return the set of currently registered suffixes (after plugin discovery)."""
    load_plugins()
    return frozenset(_REGISTRY)


def _register_from_object(obj: object) -> None:
    """Register a single plugin *obj* discovered via entry point or drop-in.

    Accepted shapes:

    * A callable ``register(register_extractor)`` hook (a module-level function
      or a module exposing ``register``).
    * A :class:`TextExtractor` subclass (or instance) carrying a ``suffixes``
      iterable attribute.
    """
    # A TextExtractor subclass with a `suffixes` attribute.
    #
    # Checked before the register() hook because TextExtractor is an ABC and
    # ``Class.register`` would otherwise resolve to ``ABCMeta.register`` (the
    # virtual-subclass registration classmethod), not a plugin hook.
    if isinstance(obj, type) and issubclass(obj, TextExtractor):
        suffixes = getattr(obj, "suffixes", None)
        if not suffixes:
            raise ValueError(
                f"Extractor {obj!r} declares no `suffixes` and no register() hook"
            )
        instance = obj()
        _register_suffixes(instance, suffixes)
        return

    # An already-instantiated TextExtractor with a `suffixes` attribute.
    if isinstance(obj, TextExtractor):
        suffixes = getattr(obj, "suffixes", None)
        if not suffixes:
            raise ValueError(
                f"Extractor {obj!r} declares no `suffixes` and no register() hook"
            )
        _register_suffixes(obj, suffixes)
        return

    # Module or plain object exposing a register(register_extractor) hook.
    register_hook = getattr(obj, "register", None)
    if callable(register_hook):
        register_hook(register_extractor)
        return

    raise TypeError(f"Cannot register plugin object {obj!r}: unsupported shape")


def _register_suffixes(extractor: TextExtractor, suffixes: Iterable[str]) -> None:
    for suffix in suffixes:
        register_extractor(suffix, extractor)


def _load_entry_point_plugins() -> None:
    """Discover and register extractors advertised via entry points.

    Each broken entry point is logged and skipped so one bad plugin cannot take
    down discovery of the others.
    """
    from importlib.metadata import entry_points

    try:
        eps = entry_points(group=ENTRY_POINT_GROUP)
    except TypeError:
        # Older importlib.metadata returns a dict-like mapping.
        eps = entry_points().get(ENTRY_POINT_GROUP, [])

    for ep in eps:
        try:
            loaded = ep.load()
            _register_from_object(loaded)
        except Exception:
            logger.warning(
                "Skipping broken extractor plugin %r from entry point %r",
                getattr(ep, "name", ep),
                getattr(ep, "value", ep),
                exc_info=True,
            )


def _plugin_dir() -> Path:
    """Return the local drop-in plugin directory (``<package>/plugins``)."""
    return Path(__file__).resolve().parent / "plugins"


def _load_dropin_plugins() -> None:
    """Discover and register extractors from drop-in ``*.py`` files.

    Files whose name starts with an underscore are ignored. Each broken file is
    logged and skipped rather than raised.
    """
    plugin_dir = _plugin_dir()
    if not plugin_dir.is_dir():
        return

    for path in sorted(plugin_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        mod_name = f"document_search.extractors.plugins.{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            if spec is None or spec.loader is None:
                raise ImportError(f"Cannot build import spec for {path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _register_from_object(module)
        except Exception:
            logger.warning(
                "Skipping broken drop-in extractor plugin %s",
                path.name,
                exc_info=True,
            )


def load_plugins(*, force: bool = False) -> None:
    """Run entry-point and drop-in discovery once per process.

    Pass ``force=True`` to re-run discovery (used by tests). Discovery is
    idempotent for already-registered suffixes.
    """
    global _plugins_loaded
    if _plugins_loaded and not force:
        return
    _plugins_loaded = True
    _load_entry_point_plugins()
    _load_dropin_plugins()
