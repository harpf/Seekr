# REST API Versioning + OpenAPI Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce REST API versioning (ROADMAP P4 "REST API versioning + OpenAPI polish") so that every existing `/api/*` endpoint is *also* reachable under a stable `/api/v1/*` prefix, with **zero breaking changes** for current clients (the wiki Swagger embed, the web UI, and Home-Assistant-style integrations on `/api/ha/*`). Additionally, polish the OpenAPI contract so `/openapi.json`, `/docs`, and the wiki Swagger iframe present a coherent, tagged, versioned reference that documents `/api/v1` as the contract going forward.

**Architecture:** The ~58 `/api/*` routes are declared as inline decorators (`@app.get(...)`, `@app.post(...)`, …) directly on the `FastAPI` instance inside `create_app` (`document_search/app.py`). Rewriting all 58 decorators onto an `APIRouter` and including it twice would be a large, error-prone diff and risks **double-registration** of operation IDs and duplicate startup hooks. Instead we add versioning with a **pure-ASGI path-rewrite middleware**: an inbound request to `/api/v1/<rest>` has its `scope["path"]` and `scope["raw_path"]` rewritten to `/api/<rest>` *before* Starlette routing runs, so the existing route table handles both prefixes with **one** set of registered routes. The legacy unprefixed `/api/*` paths continue to work untouched. To make `/api/v1` a first-class citizen in the generated OpenAPI document (so Swagger/ReDoc and the wiki iframe show the versioned contract), we wrap `app.openapi` with a custom generator that **duplicates every `/api/...` path entry under `/api/v1/...`** in the schema and stamps a deprecation note on the legacy entries. This keeps a single source of truth for the route handlers while presenting both prefixes in the spec.

**Tech Stack:** Python 3.11, FastAPI + Starlette (`BaseHTTPMiddleware` / raw ASGI middleware already used in `app.py` via `_SecurityHeaders`), pytest with `fastapi.testclient.TestClient`. No new third-party dependencies. Windows/PowerShell dev environment; tests run with `$env:PYTHONPATH = "."; pytest -q`.

**Scope boundaries:**

In scope:
- A `_VersionRewrite` ASGI middleware that maps `/api/v1/<rest>` → `/api/<rest>` at the scope level, before routing. The legacy `/api/<rest>` paths keep working unchanged.
- An `API_VERSION` constant (`"v1"`) and a single source for the version prefix string.
- Custom `app.openapi` generator that mirrors every `/api/*` path into `/api/v1/*` in the served schema and adds a `Deprecated`/sunset note to the legacy `/api/*` entries (without marking handlers deprecated in code, so no runtime behaviour changes).
- OpenAPI metadata polish: confirm/extend `title`, `description`, `version`, and `openapi_tags`; add the two currently-untagged route families (`auth`, `me`, `documents`) to coherent tags where trivial and low-risk (decorator `tags=[...]` only, no logic change).
- A documented public-API-contract note for Home-Assistant-style integrations stating they should target `/api/v1/ha/*` going forward while `/api/ha/*` remains supported through the deprecation window.
- Tests: a representative POST (`/api/login`) and a representative GET (`/api/status`) respond identically at both the legacy and `/api/v1` paths; `/openapi.json` carries the new `title`/`version` and contains both a legacy and a `/api/v1` path entry; the wiki page still renders the Swagger iframe.

Out of scope (deferred / explicitly NOT done here):
- Removing or breaking any legacy `/api/*` path (that is the *sunset* step, scheduled by date in the Notes — not executed here).
- A `/api/v2` or any behavioural change to a v1 endpoint.
- Per-endpoint request/response Pydantic model coverage for every one of the 58 routes (only the trivially-safe tagging is in scope; full response-model annotation is a separate, larger effort).
- Content negotiation / header-based versioning (`Accept: application/vnd.seekr.v1+json`). We use URL-path versioning, which is what the wiki and HA clients already speak.
- Versioning the **non-`/api` UI page routes** (`/`, `/config`, `/search`, `/ingest`, `/wiki`) — those serve HTML to the browser, not the public contract.
- Touching `/docs`, `/redoc`, `/openapi.json`, `/static/*` (the rewrite middleware must explicitly *not* touch these).

---

## File Structure

**Create:**
- `tests/test_api_versioning.py` — integration tests for dual-prefix routing and the polished OpenAPI document.

**Modify:**
- `document_search/app.py` — add `API_VERSION` / `_API_V1_PREFIX` constants, the `_VersionRewrite` middleware, the custom `openapi()` generator, and trivial `tags=[...]` additions on a small set of decorators.
- `document_search/web/templates/wiki.html` — add a short "API versioning & contract" note (documenting `/api/v1` as the stable contract and the HA target version). The Swagger iframe (`src="/docs"`) is unchanged and keeps working.

**Untouched (must remain exactly as-is):**
- Every existing `/api/*` route handler body and signature.
- The `_SecurityHeaders` middleware and its skip-list for `/docs`, `/redoc`, `/openapi.json`.
- The persistent job queue (`JobStore`, `Worker`) wiring.
- `app.mount("/static", ...)` and the Jinja templates other than `wiki.html`.

---

## Key design decisions (locked)

- **URL-path versioning via scope rewrite, not router duplication.** Routes are inline decorators on `app`, not on an `APIRouter`. Re-homing 58 decorators onto a router and `include_router`-ing it twice would (a) be a huge diff, (b) double-register operation IDs (FastAPI warns/collides), and (c) risk duplicate startup/shutdown hooks if done carelessly. A scope-level path rewrite gives both prefixes from one route table with a ~15-line middleware — the smallest, most reviewable change that cannot double-register a route.
- **Rewrite happens before routing, at the ASGI layer.** `_VersionRewrite` is a plain ASGI middleware (not `BaseHTTPMiddleware`) added *outermost* so the path is already normalised before `_SecurityHeaders` and the router see it. It only rewrites when `scope["type"] == "http"` and `scope["path"]` starts with `"/api/v1/"` (or equals `"/api/v1"`). It rewrites both `scope["path"]` and `scope["raw_path"]` (bytes) so downstream code that reads either is consistent.
- **The rewrite is prefix-stripping, not a redirect.** A 307 redirect would force clients to follow `Location`, change the URL they see, and would break non-redirect-following automations. Stripping the prefix in-process means `/api/v1/login` is served by the exact same handler as `/api/login`, returning an identical body and status with no extra round-trip.
- **`/api/v1` is *also* shown in OpenAPI, legacy is *also* kept.** We override `app.openapi` to emit, for every `paths` entry beginning `/api/`, a duplicate entry under `/api/v1/` (copied operation objects) and append a one-line "**Deprecated:** prefer `/api/v1/...`" sentence to the legacy entries' `description`. We deliberately do **not** set `deprecated: true` on the handlers in code, because that would visually strike them through and could trip client lint rules before the sunset date — the contract note is informational only during the deprecation window.
- **No new dependency.** Everything uses FastAPI/Starlette primitives already imported in `app.py`.
- **HA contract target = `/api/v1/ha/*`.** New HA integrations should target `/api/v1/ha/*`; the existing `/api/ha/*` keeps working through the deprecation window. The `X-Api-Key` auth, request bodies, and responses are byte-identical across both prefixes (same handler).
- **Idempotent / repeatable.** `create_app` runs many times per test session. The middleware and `openapi` override hold no process-global mutable state, so repeated `create_app()` calls are safe (mirrors the existing pattern where middleware classes are defined inside `create_app`).

---

## Task 1: Version constant + path-rewrite middleware

**Files:**
- Modify: `document_search/app.py` (add constants near the other module-level constants; add the middleware inside `create_app`)
- Test: `tests/test_api_versioning.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_versioning.py`:

```python
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from document_search.app import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "index.db"))
    return TestClient(app)


def _login(client, path="/api/login"):
    return client.post(path, json={"username": "admin", "password": "admin"})


def test_login_works_on_legacy_path(client):
    r = _login(client, "/api/login")
    assert r.status_code == 200
    assert "token" in r.json()


def test_login_works_on_v1_path(client):
    r = _login(client, "/api/v1/login")
    assert r.status_code == 200
    assert "token" in r.json()


def test_login_v1_and_legacy_return_same_shape(client):
    legacy = _login(client, "/api/login").json()
    v1 = _login(client, "/api/v1/login").json()
    assert set(legacy.keys()) == set(v1.keys())
    assert legacy["username"] == v1["username"] == "admin"


def test_status_get_works_on_both_prefixes(client):
    token = _login(client, "/api/v1/login").json()["token"]
    headers = {"X-Auth-Token": token}
    legacy = client.get("/api/status", headers=headers)
    v1 = client.get("/api/v1/status", headers=headers)
    assert legacy.status_code == 200
    assert v1.status_code == 200
    assert set(legacy.json().keys()) == set(v1.json().keys())


def test_unknown_v1_path_404s_not_500(client):
    # Rewrite must not crash on a path that has no legacy counterpart.
    r = client.get("/api/v1/does-not-exist")
    assert r.status_code == 404
```

Run it (expect failures on the `/api/v1/...` cases):

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_api_versioning.py
```

- [ ] **Step 2: Add the version constants**

In `document_search/app.py`, near the other module-level constants (after the `_API_KEY` / `_update_job` block, around line 80), add:

```python
# REST API versioning. URL-path strategy: every legacy `/api/<rest>` route is
# ALSO served at `/api/v1/<rest>`. `/api/v1` is the stable contract going forward;
# the unprefixed `/api/*` paths remain supported through the deprecation window
# (see docs/superpowers/plans/2026-06-05-api-versioning.md → "Notes").
API_VERSION = "v1"
_API_PREFIX = "/api"
_API_V1_PREFIX = f"{_API_PREFIX}/{API_VERSION}"  # "/api/v1"
```

- [ ] **Step 3: Add the `_VersionRewrite` ASGI middleware inside `create_app`**

In `create_app`, *before* `app.add_middleware(_SecurityHeaders)` (so this one ends up outermost — Starlette applies middleware in reverse registration order, so the **last** `add_middleware` call wraps the others; we want the rewrite to run first, so register it *after* `_SecurityHeaders`). Define the class and register it immediately after the existing `app.add_middleware(_SecurityHeaders)` line:

```python
    # ── API version rewrite ────────────────────────────────────────────
    # Maps `/api/v1/<rest>` → `/api/<rest>` at the ASGI scope level, before
    # routing, so both prefixes are served by the SAME handler with no
    # duplicate route registration and no redirect round-trip. Registered
    # AFTER _SecurityHeaders so it wraps it and runs first (Starlette wraps
    # in reverse registration order).
    class _VersionRewrite:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                path = scope.get("path", "")
                if path == _API_V1_PREFIX or path.startswith(_API_V1_PREFIX + "/"):
                    stripped = path[len(_API_V1_PREFIX):] or "/"
                    new_path = _API_PREFIX + stripped if stripped != "/" else _API_PREFIX
                    scope = dict(scope)
                    scope["path"] = new_path
                    scope["raw_path"] = new_path.encode("ascii")
            await self.app(scope, receive, send)

    app.add_middleware(_VersionRewrite)
```

> Note: `_VersionRewrite` is a raw ASGI middleware (positional `app` constructor, `__call__(scope, receive, send)`), which is what `app.add_middleware` expects when the class is not a `BaseHTTPMiddleware`. We copy `scope` with `dict(scope)` before mutating so we never mutate a scope another middleware may hold a reference to.

- [ ] **Step 4: Run the tests — they pass**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_api_versioning.py
```

All five tests should pass. The `test_unknown_v1_path_404s_not_500` case confirms the rewrite degrades gracefully (`/api/v1/does-not-exist` → `/api/does-not-exist` → normal FastAPI 404).

- [ ] **Step 5: Confirm the existing suite is unaffected**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

The legacy `/api/*` tests (`test_app_status.py`, `test_app_search.py`, …) must still pass — the rewrite never touches a path lacking the `/api/v1` prefix.

---

## Task 2: OpenAPI polish — versioned schema + coherent tags

**Files:**
- Modify: `document_search/app.py` (custom `openapi()` generator; trivial `tags=[...]` additions)
- Test: `tests/test_api_versioning.py` (extend)

- [ ] **Step 1: Write the failing OpenAPI tests**

Append to `tests/test_api_versioning.py`:

```python
def test_openapi_has_polished_metadata(client):
    spec = client.get("/openapi.json").json()
    info = spec["info"]
    assert info["title"] == "Seekr"
    assert info["version"]  # non-empty semantic version
    assert "Home Assistant" in info["description"]


def test_openapi_lists_both_legacy_and_v1_paths(client):
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    # Representative endpoint present under both prefixes.
    assert "/api/login" in paths
    assert "/api/v1/login" in paths
    assert "/api/status" in paths
    assert "/api/v1/status" in paths


def test_openapi_v1_path_mirrors_legacy_operation(client):
    spec = client.get("/openapi.json").json()
    legacy = spec["paths"]["/api/login"]["post"]
    v1 = spec["paths"]["/api/v1/login"]["post"]
    # Same operation surface (params/request body), independent operationId.
    assert legacy.get("requestBody") == v1.get("requestBody")
    assert v1["operationId"] != legacy["operationId"]


def test_openapi_legacy_paths_carry_deprecation_note(client):
    spec = client.get("/openapi.json").json()
    desc = spec["paths"]["/api/login"]["post"].get("description", "")
    assert "/api/v1" in desc  # legacy entry points clients at the v1 prefix


def test_openapi_tags_are_declared(client):
    spec = client.get("/openapi.json").json()
    tag_names = {t["name"] for t in spec.get("tags", [])}
    for expected in {"auth", "search", "index", "ha", "ai"}:
        assert expected in tag_names
```

Run (the v1-path and deprecation-note assertions fail until Step 3):

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_api_versioning.py
```

- [ ] **Step 2: Add the two trivial tags (low-risk, decorator-only)**

The FastAPI metadata (`title`, `description`, `version="1.5.0"`, `openapi_tags=_OPENAPI_TAGS`) already exists at the `FastAPI(...)` call. `auth`, `search`, `index`, `ha`, `ai`, `users`, `config`, `system`, `update`, `ssl`, `files` are already declared in `_OPENAPI_TAGS`. Add `documents` and `me`-style routes to coherent existing tags by editing only the decorator's `tags=` argument (no body change). Example edits:

```python
    @app.post("/api/login", tags=["auth"])
    def api_login(req: LoginRequest, request: Request):
        ...

    @app.get("/api/me", tags=["auth"])
    def api_me(x_auth_token: str | None = Header(default=None)):
        ...
```

And add a `documents` tag entry to `_OPENAPI_TAGS` plus tag the document routes:

```python
_OPENAPI_TAGS = [
    {"name": "auth",      "description": "Login and session"},
    {"name": "search",    "description": "Full-text document search"},
    {"name": "index",     "description": "Crawl and indexing jobs"},
    {"name": "documents", "description": "Per-document marks, tags, reindex"},
    {"name": "ha",        "description": "Home Assistant integration — authenticate with `X-Api-Key` header"},
    {"name": "ai",        "description": "Ollama AI operations"},
    {"name": "users",     "description": "User management (admin only)"},
    {"name": "config",    "description": "Application configuration"},
    {"name": "system",    "description": "System diagnostics and maintenance"},
    {"name": "update",    "description": "Application update via git + Docker"},
    {"name": "ssl",       "description": "TLS certificate management"},
    {"name": "files",     "description": "File serving"},
]
```

Then add `tags=["documents"]` to `@app.post("/api/documents/mark")`, `@app.post("/api/documents/tags")`, `@app.get("/api/tags")`, and `@app.post("/api/documents/{document_id}/reindex")`. **Do not** change any handler body. Keep this set small — tagging is cosmetic and must not alter routing.

- [ ] **Step 3: Add the custom `openapi()` generator (mirror to `/api/v1`)**

Just before `return app` at the end of `create_app`, add:

```python
    # ── OpenAPI: mirror every /api/* path under /api/v1/* and flag legacy ──
    from fastapi.openapi.utils import get_openapi

    _BASE_DESCRIPTION = app.description

    def _custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=(
                f"{_BASE_DESCRIPTION}\n\n"
                f"**API versioning:** `{_API_V1_PREFIX}/*` is the stable contract. "
                f"Unprefixed `{_API_PREFIX}/*` paths are retained for backward "
                f"compatibility and will be sunset in a future major release — "
                f"prefer `{_API_V1_PREFIX}/*` for all new integrations."
            ),
            routes=app.routes,
            tags=_OPENAPI_TAGS,
        )
        paths = schema.get("paths", {})
        v1_paths: dict = {}
        for path, item in list(paths.items()):
            if not path.startswith(_API_PREFIX + "/") and path != _API_PREFIX:
                continue  # leave non-/api paths (/, /docs assets, etc.) alone
            v1_path = _API_V1_PREFIX + path[len(_API_PREFIX):]
            mirrored: dict = {}
            for method, op in item.items():
                if not isinstance(op, dict):
                    mirrored[method] = op
                    continue
                v1_op = json.loads(json.dumps(op))  # deep copy
                if "operationId" in v1_op:
                    v1_op["operationId"] = f"{v1_op['operationId']}_v1"
                mirrored[method] = v1_op
                # Annotate the legacy operation in-place (informational only).
                note = f"\n\n**Deprecated path prefix.** Prefer `{v1_path}`."
                op["description"] = (op.get("description") or op.get("summary") or "") + note
            v1_paths[v1_path] = mirrored
        paths.update(v1_paths)
        schema["paths"] = paths
        app.openapi_schema = schema
        return schema

    app.openapi = _custom_openapi
```

> `json` is already imported at the top of `app.py`. The deep-copy via `json.dumps`/`loads` avoids aliasing the legacy and v1 operation objects (so the legacy-only deprecation note doesn't leak into the v1 copy). `app.openapi_schema` is cached by FastAPI; setting `app.openapi = _custom_openapi` is the documented override hook.

- [ ] **Step 4: Run the OpenAPI tests — they pass**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_api_versioning.py
```

- [ ] **Step 5: Manually eyeball the spec once**

```powershell
$env:PYTHONPATH = "."; python -c "from document_search.app import create_app; import json; s=create_app('./_tmp_ver.db').openapi(); print(s['info']['title'], s['info']['version']); print('/api/login' in s['paths'], '/api/v1/login' in s['paths'])"
```

Expect: `Seekr 1.5.0` then `True True`. Delete `_tmp_ver.db` afterward.

---

## Task 3: Wiki contract note + HA target version

**Files:**
- Modify: `document_search/web/templates/wiki.html`
- Test: `tests/test_api_versioning.py` (extend with a render check)

- [ ] **Step 1: Write the failing render test**

Append to `tests/test_api_versioning.py`:

```python
def test_wiki_page_renders_and_mentions_v1_contract(client):
    r = client.get("/wiki")
    assert r.status_code == 200
    body = r.text
    # Swagger iframe still embedded.
    assert 'src="/docs"' in body
    # Versioning contract note present.
    assert "/api/v1" in body
```

Run (the `/api/v1` assertion fails until Step 2):

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_api_versioning.py
```

- [ ] **Step 2: Add the contract note to `wiki.html`**

The wiki already documents endpoints in a "Reference" section and notes that `/api/ha/*` use `X-Api-Key`. Locate the existing paragraph that explains auth (the one containing `All UI endpoints require an <code>X-Auth-Token</code> header obtained from <code>POST /api/login</code>.`) and insert immediately after it a short versioning block:

```html
            <div class="wiki-note" style="margin-top:.75rem;">
              <strong>API versioning &amp; contract.</strong>
              <code>/api/v1/*</code> is the stable, supported contract — point new
              integrations at it. The unprefixed <code>/api/*</code> paths still work
              and return identical responses (same handlers), but are retained only for
              backward compatibility and will be sunset in a future major release.
              <strong>Home Assistant integrations</strong> should call
              <code>/api/v1/ha/search</code>, <code>/api/v1/ha/status</code>, and
              <code>/api/v1/ha/index</code> (the <code>/api/ha/*</code> equivalents
              remain available during the deprecation window).
            </div>
```

If a `.wiki-note` style does not exist, the inline `style` keeps it readable without new CSS; this is a documentation-only change and must not alter any script or iframe in the page.

- [ ] **Step 3: Run the render test — it passes**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_api_versioning.py
```

- [ ] **Step 4: Full suite green**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

---

## Task 4: Commit

- [ ] **Step 1: Stage and commit on a feature branch**

```powershell
git checkout -b feat/api-versioning
git add document_search/app.py document_search/web/templates/wiki.html tests/test_api_versioning.py docs/superpowers/plans/2026-06-05-api-versioning.md
git status
```

- [ ] **Step 2: Commit with a Conventional-Commit message**

```powershell
git commit -m @'
feat(api): version routes under /api/v1 and polish OpenAPI contract

Serve every /api/* endpoint additionally under /api/v1/* via an ASGI
scope-rewrite middleware (no route duplication, no redirect). Mirror all
/api/* paths into /api/v1/* in the generated OpenAPI schema, flag legacy
paths as deprecated-prefix, and tag the previously-untagged auth/documents
routes. Document /api/v1 as the stable contract (incl. HA integrations) in
the wiki Swagger page. Backward compatible: /api/* paths unchanged.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
'@
```

---

## Definition of Done

- [ ] `POST /api/login` and `POST /api/v1/login` return the same status and the same JSON keys.
- [ ] `GET /api/status` and `GET /api/v1/status` return the same status and the same JSON keys.
- [ ] An unknown `/api/v1/...` path returns a normal `404` (rewrite never raises `500`).
- [ ] `/openapi.json` `info.title == "Seekr"`, `info.version` is non-empty, and `info.description` mentions Home Assistant **and** the `/api/v1` contract.
- [ ] `/openapi.json` `paths` contains both `/api/login` and `/api/v1/login` (and `/api/status` + `/api/v1/status`); the v1 operation has a distinct `operationId` and an identical `requestBody`.
- [ ] Legacy `/api/*` path operations carry a `**Deprecated path prefix.**` note pointing at the `/api/v1` equivalent; handlers are **not** marked `deprecated: true` in code.
- [ ] `/openapi.json` declares the `auth`, `search`, `index`, `ha`, `ai`, and `documents` tags.
- [ ] `GET /wiki` returns 200, still embeds `src="/docs"`, and mentions `/api/v1`.
- [ ] No existing route handler body or signature changed (only `tags=[...]` added on a small set).
- [ ] `$env:PYTHONPATH = "."; pytest -q` is fully green (new file + all pre-existing tests).
- [ ] Conventional-commit `feat(api): ...` created on a feature branch.

---

## Notes for the executing agent

- **Middleware ordering is load-bearing.** Starlette applies middleware in reverse registration order, so the **last** `app.add_middleware(...)` call is the **outermost** wrapper and runs first. Register `_VersionRewrite` *after* `_SecurityHeaders` so the path is normalised before security headers and routing see it. If you reverse the order, security headers still apply (they run on the response) but you should keep the rewrite outermost for clarity. Verify with the `test_status_get_works_on_both_prefixes` test.
- **Do not rewrite `/docs`, `/redoc`, `/openapi.json`, `/static`.** None of these start with `/api/v1/`, so the guard naturally skips them — but if you ever broaden the prefix match, re-check this. The `_SecurityHeaders` skip-list for the docs routes is unrelated and must stay.
- **`raw_path` matters.** Some Starlette internals and any future middleware may read `scope["raw_path"]`. We rewrite both `path` and `raw_path`; do not drop the `raw_path` line.
- **OpenAPI cache.** FastAPI caches `app.openapi_schema`. Because `create_app` builds a fresh `FastAPI` each call, each app instance gets its own cache — safe for the test suite. Do not memoise the schema at module scope.
- **Maintenance cost of dual-mounting.** Every new `/api/*` route added in the future is *automatically* available at `/api/v1/*` (the rewrite and the schema-mirror are generic). The cost is: (1) the OpenAPI document is roughly twice as long (every operation appears under both prefixes) — acceptable for a single-version window; (2) operation-IDs are suffixed `_v1` for the mirror, so client-codegen sees two operations per endpoint. When `/api/v2` arrives, the generic mirror approach will need revisiting (you would not want v1 *and* v2 to both be auto-mirrors of the same handlers — at that point routes that genuinely differ between versions must diverge, which is the moment to extract real `APIRouter`s).
- **Sunset plan for the unprefixed `/api/*` paths.** Keep both prefixes for at least one minor-release cycle after this ships. The deprecation is currently **documentation-only** (wiki note + OpenAPI description). Concrete sunset steps for a later plan: (1) bump to a new major version; (2) set `deprecated: true` on the legacy operations and emit a `Deprecation`/`Sunset` response header (RFC 8594) from a small response middleware for any request whose original path lacked the `/api/v1` prefix; (3) after the announced sunset date, change `_VersionRewrite` to *only* accept `/api/v1/*` and let bare `/api/*` 404 — or return `410 Gone` with a pointer to `/api/v1`. Do **not** perform any of these removal steps in this plan.
- **Windows/PowerShell.** All commands above use PowerShell syntax (`$env:PYTHONPATH = "."`, single-quoted here-strings `@'...'@` with the closing `'@` at column 0). Do not use bash `export` or `&&`-only chains in the shell tool unless on the Bash tool.
- **Verification before completion.** Run the full suite (`pytest -q`) and the manual `python -c` spec check in Task 2 Step 5 before claiming done. Evidence (green output) before assertions.
