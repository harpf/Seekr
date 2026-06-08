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


GATED = ("index.html", "search.html", "ingest.html", "config.html")
ALL_PAGES = GATED + ("wiki.html",)


def test_auth_gate_partial_exists():
    assert (TEMPLATES / "_partials" / "auth_gate.html").exists()


def test_no_template_inlines_the_duplicated_auth_gate():
    # The duplicated literal was the sign-in card head subtitle text.
    for name in GATED:
        html = (TEMPLATES / name).read_text(encoding="utf-8")
        assert 'include "_partials/auth_gate.html"' in html, f"{name} must include the partial"
        # The old duplicated markers must be gone from the page body.
        assert html.count('id="authGate"') == 0, f"{name} still inlines the auth gate"


def test_every_page_has_main_landmark_and_skip_link():
    for name in ALL_PAGES:
        html = (TEMPLATES / name).read_text(encoding="utf-8")
        assert 'id="main"' in html, f"{name} missing main landmark id"
        assert "skip-link" in html, f"{name} missing skip link"


def test_every_page_has_theme_toggle():
    for name in ALL_PAGES:
        html = (TEMPLATES / name).read_text(encoding="utf-8")
        assert 'id="themeToggle"' in html, f"{name} missing theme toggle"


def test_toast_wrap_is_live_region_in_templates():
    for name in GATED:
        html = (TEMPLATES / name).read_text(encoding="utf-8")
        assert 'id="toastWrap"' in html, f"{name} missing toast wrap"
        assert 'aria-live="polite"' in html, f"{name} toast wrap not a live region"
