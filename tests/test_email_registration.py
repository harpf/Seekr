"""The .eml/.msg extractors must be wired into the registry, config default and
upload allow-list so they flow through search, the worker and the CLI."""

from document_search.config import AppConfig
from document_search.extractors import (
    BUILTIN_EXTENSIONS,
    extractor_for,
    supported_extensions,
)
from document_search.extractors.eml_extractor import EmlTextExtractor
from document_search.extractors.msg_extractor import MsgTextExtractor

EXPECTED = {".eml": EmlTextExtractor, ".msg": MsgTextExtractor}


def test_email_extensions_resolve_from_registry():
    for ext, cls in EXPECTED.items():
        resolved = extractor_for(ext)
        assert isinstance(resolved, cls), f"{ext} did not resolve to {cls.__name__}"


def test_email_extensions_are_builtins():
    for ext in EXPECTED:
        assert ext in BUILTIN_EXTENSIONS
        assert ext in supported_extensions()


def test_email_extensions_in_default_config():
    cfg = AppConfig()
    for ext in EXPECTED:
        assert ext in cfg.supported_extensions
