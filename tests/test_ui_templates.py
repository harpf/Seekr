from pathlib import Path

STATIC = Path("document_search/web/static")
TEMPLATES = Path("document_search/web/templates")


def _css() -> str:
    return (STATIC / "styles.css").read_text(encoding="utf-8")


def test_dark_theme_token_block_exists():
    css = _css()
    assert '[data-theme="dark"]' in css, "dark theme token block missing"
    # Dark theme must redefine the core background + text tokens
    block = css.split('[data-theme="dark"]', 1)[1].split("}", 1)[0]
    for token in ("--bg:", "--surface:", "--txt-1:", "--b-lo:"):
        assert token in block, f"{token} not overridden in dark theme"


def test_focus_visible_ring_exists():
    css = _css()
    assert ":focus-visible" in css, "no :focus-visible focus ring defined"


def test_sr_only_helper_exists():
    css = _css()
    assert ".sr-only" in css, ".sr-only screen-reader helper missing"


def test_reduced_motion_block_exists():
    css = _css()
    assert "prefers-reduced-motion" in css, "no reduced-motion guard"
