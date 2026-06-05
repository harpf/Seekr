# Per-User UI Preferences Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist per-user UI preferences (theme, results-per-page, default search filters) server-side so settings follow the user across devices. Today these live only in browser `localStorage` (theme via the dark-mode toggle, search `limit` hard-coded to `25`, filters not remembered at all). Add a `user_preferences` table, a `SqliteStore` get/set helper pair with sane defaults, a `require_user`-scoped `GET`/`PUT /api/preferences`, fold prefs into `/api/me`, and hydrate/write-through from the frontend with `localStorage` kept as an offline cache.

**Architecture:** One new SQLite table `user_preferences` (one row per user, JSON blob in a `prefs_json` column) added inside the existing `executescript` block in `SqliteStore._init_schema` — the same place `principals`/`user_groups`/`document_acl` and `jobs` were added. Two `SqliteStore` helpers, `get_preferences(user_id)` and `set_preferences(user_id, prefs)`, merge stored values over a canonical `DEFAULT_PREFERENCES` dict so missing/new keys always have a sane value. A Pydantic `PreferencesRequest` model plus two endpoints (`GET`/`PUT /api/preferences`), both scoped with `require_user` to the caller's own `user_id` — mirroring the existing per-user `/api/documents/tags` + `/api/tags` pattern. `/api/me` gains a `preferences` field so a single round-trip on login hydrates the UI. The frontend reads prefs from the server after login, applies theme (coordinating with the dark-mode toggle from the UI accessibility/theming plan), results-per-page, and default filters, and writes through on change while keeping `localStorage` as an offline fallback.

**Tech Stack:** Python 3.11, SQLite, FastAPI, Pydantic, pytest, vanilla JS (no build step). No new third-party dependencies.

**Scope boundaries (out of scope for this plan, picked up by later plans):**
- No admin UI to view or edit another user's preferences — strictly caller-scoped.
- No server-side validation of arbitrary nested preference shapes beyond the three known keys (`theme`, `results_per_page`, `default_filters`); unknown keys are dropped on write, not rejected.
- The dark-mode `[data-theme="dark"]` token block + toggle button markup is owned by `2026-06-05-ui-accessibility-and-theming.md`. This plan only persists the chosen theme and applies it on hydration; if that plan has not landed, the theme value is still stored and applied to `document.documentElement` harmlessly.
- No migration of the `seekr_recent` recent-searches list to the server (that is `2026-06-05-saved-searches-and-history.md`).

---

## File Structure

**Create:**
- `tests/test_user_preferences.py` — unit tests for the store helpers + endpoint integration tests (defaults, per-user persistence, isolation between users).

**Modify:**
- `document_search/index/sqlite_store.py` — add `user_preferences` table to the `executescript` block in `_init_schema`; add module-level `DEFAULT_PREFERENCES`; add `get_preferences` / `set_preferences` helpers.
- `document_search/app.py` — add `PreferencesRequest` Pydantic model; add `GET`/`PUT /api/preferences`; extend `/api/me` response with a `preferences` field.
- `document_search/web/static/app.js` — hydrate theme/results-per-page/default-filters from `/api/me` on login + bootstrap; write through to `/api/preferences` on change; use the stored `results_per_page` in `runSearch` instead of the hard-coded `25`; keep `localStorage` as an offline cache.

**Read-only references:**
- `document_search/app.py:495-513` — `require_user` / `require_admin` shape.
- `document_search/app.py:584-620` — `/api/me`, per-user tags `GET`/`POST` pattern to mirror.
- `document_search/index/sqlite_store.py:328-356` — `create_user` (per-user row creation pattern).
- `docs/superpowers/plans/2026-06-05-ui-accessibility-and-theming.md` — dark-mode `[data-theme="dark"]` + toggle that this plan coordinates with.

---

## Task 1: `user_preferences` table + `SqliteStore` helpers

**Files:**
- Modify: `document_search/index/sqlite_store.py` (extend `_init_schema`, add `DEFAULT_PREFERENCES`, `get_preferences`, `set_preferences`)
- Test: `tests/test_user_preferences.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_user_preferences.py`:

```python
from pathlib import Path

import pytest

from document_search.index.sqlite_store import DEFAULT_PREFERENCES, SqliteStore


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "test.db")


def test_user_preferences_table_exists(store):
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='user_preferences'"
    ).fetchall()
    assert len(rows) == 1


def test_get_preferences_returns_defaults_for_new_user(store):
    user_id = store.create_user("alice", "alice-password")
    prefs = store.get_preferences(user_id)
    assert prefs == DEFAULT_PREFERENCES
    # Must be a copy — mutating the result must not corrupt the module default.
    prefs["theme"] = "mutated"
    assert DEFAULT_PREFERENCES["theme"] != "mutated"


def test_set_preferences_persists_and_merges_over_defaults(store):
    user_id = store.create_user("bob", "bob-password")
    store.set_preferences(user_id, {"theme": "dark", "results_per_page": 50})
    prefs = store.get_preferences(user_id)
    assert prefs["theme"] == "dark"
    assert prefs["results_per_page"] == 50
    # Untouched key falls back to its default.
    assert prefs["default_filters"] == DEFAULT_PREFERENCES["default_filters"]


def test_set_preferences_drops_unknown_keys(store):
    user_id = store.create_user("carol", "carol-password")
    store.set_preferences(user_id, {"theme": "light", "evil": "ignored"})
    prefs = store.get_preferences(user_id)
    assert "evil" not in prefs
    assert set(prefs.keys()) == set(DEFAULT_PREFERENCES.keys())


def test_set_preferences_is_idempotent_upsert(store):
    user_id = store.create_user("dave", "dave-password")
    store.set_preferences(user_id, {"theme": "dark"})
    store.set_preferences(user_id, {"theme": "light"})
    assert store.get_preferences(user_id)["theme"] == "light"
    # Exactly one row per user.
    count = store.conn.execute(
        "SELECT COUNT(*) FROM user_preferences WHERE user_id=?", (user_id,)
    ).fetchone()[0]
    assert count == 1


def test_preferences_isolated_between_users(store):
    alice = store.create_user("alice", "alice-password")
    bob = store.create_user("bob", "bob-password")
    store.set_preferences(alice, {"theme": "dark", "results_per_page": 100})
    store.set_preferences(bob, {"theme": "light", "results_per_page": 10})
    assert store.get_preferences(alice)["theme"] == "dark"
    assert store.get_preferences(alice)["results_per_page"] == 100
    assert store.get_preferences(bob)["theme"] == "light"
    assert store.get_preferences(bob)["results_per_page"] == 10
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_user_preferences.py -v
```

Expected: ImportError on `DEFAULT_PREFERENCES` (and, once that's added, `test_user_preferences_table_exists` fails — table doesn't exist).

- [ ] **Step 3: Add the schema, default, and helpers in `sqlite_store.py`**

In `document_search/index/sqlite_store.py`, add the module-level default just below the existing imports (after the `from document_search.models import ...` line, ~line 9):

```python
DEFAULT_PREFERENCES: dict = {
    "theme": "system",          # "system" | "light" | "dark"
    "results_per_page": 25,     # matches the previous hard-coded frontend limit
    "default_filters": {        # pre-applied on the search page
        "filetype": [],         # list[str] of extensions, e.g. [".pdf"]
        "tags": [],             # list[str] of tag names
        "path": "",             # path prefix filter
        "block_type": "",       # "" | "paragraph" | "table" | ...
    },
}
```

Then, inside the `self.conn.executescript("""...""")` block of `_init_schema`, **append** just before the closing `"""` (i.e. after the last `CREATE INDEX IF NOT EXISTS idx_jobs_owner ...` line, ~line 158):

```sql
CREATE TABLE IF NOT EXISTS user_preferences (
  user_id INTEGER PRIMARY KEY,
  prefs_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
```

Now add the two helper methods. Place them directly after `get_user_tags` (the method ending at ~line 453, just before `remove_missing`):

```python
    def get_preferences(self, user_id: int) -> dict:
        """Return this user's preferences merged over DEFAULT_PREFERENCES.

        Stored values override defaults key-by-key, so newly introduced
        preference keys always come back with a sane value even for rows
        written before the key existed. Always returns a fresh dict — callers
        may mutate the result without affecting the module-level default.
        """
        import copy
        import json

        prefs = copy.deepcopy(DEFAULT_PREFERENCES)
        row = self.conn.execute(
            "SELECT prefs_json FROM user_preferences WHERE user_id=?", (user_id,)
        ).fetchone()
        if row and row["prefs_json"]:
            try:
                stored = json.loads(row["prefs_json"])
            except (ValueError, TypeError):
                stored = {}
            if isinstance(stored, dict):
                for key in DEFAULT_PREFERENCES:
                    if key in stored:
                        prefs[key] = stored[key]
        return prefs

    def set_preferences(self, user_id: int, prefs: dict) -> dict:
        """Upsert this user's preferences. Unknown keys are dropped; known keys
        are merged over the current stored values. Returns the full, merged
        preferences dict (defaults + stored + this update)."""
        import json

        merged = self.get_preferences(user_id)
        for key in DEFAULT_PREFERENCES:
            if key in prefs:
                merged[key] = prefs[key]
        now = datetime.now(tz=UTC).isoformat()
        self.conn.execute(
            """
            INSERT INTO user_preferences(user_id, prefs_json, updated_at)
            VALUES(?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              prefs_json=excluded.prefs_json, updated_at=excluded.updated_at
            """,
            (user_id, json.dumps(merged), now),
        )
        self.conn.commit()
        return merged
```

(`datetime`, `UTC` are already imported at the top of the module — see line 5.)

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_user_preferences.py -v
```

Expected: all 6 tests pass.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: previously-passing tests still pass; +6 new.

- [ ] **Step 6: Commit**

```powershell
git add document_search/index/sqlite_store.py tests/test_user_preferences.py
git commit -m "feat(preferences): user_preferences table + get/set helpers with defaults"
```

---

## Task 2: `GET`/`PUT /api/preferences` endpoints + `/api/me` field

**Files:**
- Modify: `document_search/app.py` (add `PreferencesRequest`, two endpoints, `/api/me` field)
- Test: `tests/test_user_preferences.py` (extend)

- [ ] **Step 1: Write the failing integration tests**

Append to `tests/test_user_preferences.py`:

```python
import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from document_search.app import create_app  # noqa: E402


def _client(tmp_path):
    return TestClient(create_app(str(tmp_path / "app.db")))


def _login(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_get_preferences_returns_defaults(tmp_path):
    with _client(tmp_path) as client:
        token = _login(client)
        r = client.get("/api/preferences", headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["theme"] == "system"
        assert body["results_per_page"] == 25
        assert body["default_filters"]["filetype"] == []


def test_get_preferences_requires_auth(tmp_path):
    with _client(tmp_path) as client:
        r = client.get("/api/preferences")
        assert r.status_code == 401


def test_put_preferences_persists_and_returns_merged(tmp_path):
    with _client(tmp_path) as client:
        token = _login(client)
        r = client.put(
            "/api/preferences",
            headers={"X-Auth-Token": token},
            json={"theme": "dark", "results_per_page": 50},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["theme"] == "dark"
        assert body["results_per_page"] == 50
        # Round-trips: a fresh GET reflects the saved values.
        r2 = client.get("/api/preferences", headers={"X-Auth-Token": token})
        assert r2.json()["theme"] == "dark"


def test_put_preferences_requires_auth(tmp_path):
    with _client(tmp_path) as client:
        r = client.put("/api/preferences", json={"theme": "dark"})
        assert r.status_code == 401


def test_me_includes_preferences(tmp_path):
    with _client(tmp_path) as client:
        token = _login(client)
        client.put(
            "/api/preferences",
            headers={"X-Auth-Token": token},
            json={"theme": "light", "results_per_page": 10},
        )
        r = client.get("/api/me", headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        body = r.json()
        assert "preferences" in body
        assert body["preferences"]["theme"] == "light"
        assert body["preferences"]["results_per_page"] == 10


def test_preferences_isolated_between_users_over_api(tmp_path):
    with _client(tmp_path) as client:
        admin_token = _login(client)
        # admin creates a second user
        r = client.post(
            "/api/users",
            headers={"X-Auth-Token": admin_token},
            json={"username": "second", "password": "second-password", "role": "user"},
        )
        assert r.status_code == 200, r.text
        admin_put = client.put(
            "/api/preferences",
            headers={"X-Auth-Token": admin_token},
            json={"theme": "dark", "results_per_page": 100},
        )
        assert admin_put.status_code == 200

        second_token = _login(client, "second", "second-password")
        second_put = client.put(
            "/api/preferences",
            headers={"X-Auth-Token": second_token},
            json={"theme": "light", "results_per_page": 10},
        )
        assert second_put.status_code == 200

        admin_prefs = client.get(
            "/api/preferences", headers={"X-Auth-Token": admin_token}
        ).json()
        second_prefs = client.get(
            "/api/preferences", headers={"X-Auth-Token": second_token}
        ).json()
        assert admin_prefs["theme"] == "dark"
        assert admin_prefs["results_per_page"] == 100
        assert second_prefs["theme"] == "light"
        assert second_prefs["results_per_page"] == 10
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_user_preferences.py -v
```

Expected: the new API tests fail with 404/`KeyError` — endpoints and the `/api/me` field don't exist yet.

- [ ] **Step 3: Add the Pydantic model**

In `document_search/app.py`, add the model next to the other request models (after `TagsRequest`, ~line 206):

```python
class PreferencesRequest(BaseModel):
    theme: str | None = None
    results_per_page: int | None = None
    default_filters: dict | None = None
```

All fields are optional so a partial `PUT` updates only the supplied keys (the store merges over current values).

- [ ] **Step 4: Add the two endpoints**

In `document_search/app.py`, add the endpoints directly after the `/api/tags` handler (`api_list_tags`, ~line 620), mirroring the per-user tags pattern:

```python
    @app.get("/api/preferences")
    def api_get_preferences(x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        return store().get_preferences(user_id)

    @app.put("/api/preferences")
    def api_put_preferences(
        req: PreferencesRequest, x_auth_token: str | None = Header(default=None)
    ):
        user_id = require_user(x_auth_token)
        update = req.model_dump(exclude_none=True)
        return store().set_preferences(user_id, update)
```

`exclude_none=True` means a partial body (e.g. only `{"theme": "dark"}`) leaves the other stored keys untouched.

- [ ] **Step 5: Fold preferences into `/api/me`**

In `document_search/app.py`, update the return of `api_me` (~line 596) from:

```python
        return {"id": user_id, "username": user["username"], "role": role}
```

to:

```python
        return {
            "id": user_id,
            "username": user["username"],
            "role": role,
            "preferences": db.get_preferences(user_id),
        }
```

(`db = store()` is already in scope at that point — see line 592.)

- [ ] **Step 6: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_user_preferences.py -v
```

Expected: all 12 tests pass (6 store + 6 API).

- [ ] **Step 7: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: the existing `/api/me` test (if any) still passes — the response only gains a key, no existing key changes. Investigate any failure before proceeding.

- [ ] **Step 8: Commit**

```powershell
git add document_search/app.py tests/test_user_preferences.py
git commit -m "feat(preferences): GET/PUT /api/preferences (require_user) and /api/me prefs"
```

---

## Task 3: Frontend hydration + write-through

**Files:**
- Modify: `document_search/web/static/app.js`

There is no JS test runner in this repo (see `2026-06-05-ui-accessibility-and-theming.md`), so this task is verified manually (Step 6) plus the structural `grep` checks in Step 5. Do **not** add a JS harness here.

- [ ] **Step 1: Add a preferences cache + apply/hydrate helpers**

In `document_search/web/static/app.js`, just below the existing `let token = ...` declaration (~line 68), add a module-level prefs cache and the default shape (kept in sync with the server `DEFAULT_PREFERENCES`):

```javascript
const DEFAULT_PREFS = {
  theme: 'system',
  results_per_page: 25,
  default_filters: { filetype: [], tags: [], path: '', block_type: '' },
};
let userPrefs = (() => {
  try {
    const cached = JSON.parse(localStorage.getItem('seekr_prefs') || 'null');
    return cached ? { ...DEFAULT_PREFS, ...cached } : { ...DEFAULT_PREFS };
  } catch (_) {
    return { ...DEFAULT_PREFS };
  }
})();
```

Then add the apply + hydrate + persist helpers. Place them just above the `// ── Auth ──` comment block (~line 131):

```javascript
// ── Preferences ────────────────────────────────────────────────────
function applyTheme(theme) {
  // Coordinates with the dark-mode toggle (UI accessibility/theming plan):
  // 'system' clears the override so prefers-color-scheme decides.
  const root = document.documentElement;
  if (theme === 'light' || theme === 'dark') {
    root.setAttribute('data-theme', theme);
  } else {
    root.removeAttribute('data-theme');
  }
}

function applyPreferences(prefs) {
  userPrefs = { ...DEFAULT_PREFS, ...(prefs || {}) };
  // Offline cache so a reload paints the right theme before /api/me returns.
  localStorage.setItem('seekr_prefs', JSON.stringify(userPrefs));
  applyTheme(userPrefs.theme);
  // Pre-fill default search filters if the search page is mounted.
  const df = userPrefs.default_filters || {};
  if (document.body?.dataset?.page === 'search') {
    if (chipFiletype && Array.isArray(df.filetype)) chipFiletype.setValues(df.filetype);
    if (chipTagFilter && Array.isArray(df.tags)) chipTagFilter.setValues(df.tags);
    const pathEl = document.getElementById('pathFilter');
    if (pathEl && typeof df.path === 'string') pathEl.value = df.path;
    const blockEl = document.getElementById('blockType');
    if (blockEl && typeof df.block_type === 'string') blockEl.value = df.block_type;
  }
}

async function hydratePreferences() {
  // Paint the cached theme immediately, then reconcile with the server.
  applyTheme(userPrefs.theme);
  try {
    const me = await api('/api/me');
    if (me && me.preferences) applyPreferences(me.preferences);
  } catch (_) {
    /* offline / unauthorised — keep the localStorage cache */
  }
}

async function savePreferences(patch) {
  // Optimistic local update + write-through. localStorage stays the offline cache.
  applyPreferences({ ...userPrefs, ...patch });
  try {
    const saved = await api('/api/preferences', 'PUT', patch);
    applyPreferences(saved);
  } catch (_) {
    showToast('Could not save preferences — change kept locally', 'err');
  }
}
```

- [ ] **Step 2: Use the stored results-per-page in `runSearch`**

In `runSearch` (~line 257), replace the hard-coded limit:

```javascript
      query: query.value, limit: 25,
```

with the preference value:

```javascript
      query: query.value, limit: userPrefs.results_per_page || 25,
```

Also update the two `25`-literal result-count checks immediately below the `api('/api/search', ...)` call (~lines 271–273) so the "25+ results" hint tracks the actual page size:

```javascript
    const limit = userPrefs.results_per_page || 25;
    const metaEl = document.getElementById('resultsMeta');
    if (metaEl) {
      metaEl.textContent = !data.length
        ? ''
        : data.length === limit ? `${limit}+ results` : `${data.length} result${data.length !== 1 ? 's' : ''}`;
    }
```

- [ ] **Step 3: Hydrate on login and on bootstrap**

In `login()` (~line 176), immediately after `showAuthedPanels();` add:

```javascript
    await hydratePreferences();
```

In `bootstrap()`, inside the `if (token) { ... }` branch (after `showAuthedPanels();`, ~line 1725), add:

```javascript
    await hydratePreferences();
```

Because hydration pre-fills the search chips, ensure it runs **after** the `chipFiletype` / `chipTagFilter` `ChipInput` instances are constructed. In both `login()` and `bootstrap()` the chip construction for the search page already happens after `showAuthedPanels()`; move the `await hydratePreferences();` call to **after** `await loadFilterOptions();` in the search-page branch so the chips exist and have their option lists before `applyPreferences` sets values. Concretely, in the search-page block change the tail from:

```javascript
      await loadFilterOptions();
      await loadTagCloud();
```

to:

```javascript
      await loadFilterOptions();
      await loadTagCloud();
      await hydratePreferences();
```

and remove the standalone early `await hydratePreferences();` you added right after `showAuthedPanels()` **only on the search page path** (keep it for non-search pages so the theme still applies). The simplest robust form: call `await hydratePreferences();` once right after `showAuthedPanels()` for the theme, and a second time after `loadFilterOptions()` on the search page for the filters — `applyPreferences` is idempotent, so calling it twice is safe.

- [ ] **Step 4: Write through on change**

Wire the two write-through points. Theme: the dark-mode toggle button (id `themeToggle`, introduced by the UI accessibility/theming plan) should call `savePreferences`. Add an idempotent binder and call it from `initNav()` (~line 1717, the first line of `bootstrap`'s `initNav()` call site — bind inside `initNav` itself):

```javascript
function bindPreferenceControls() {
  const themeBtn = document.getElementById('themeToggle');
  if (themeBtn && !themeBtn.dataset.prefBound) {
    themeBtn.dataset.prefBound = '1';
    themeBtn.addEventListener('click', () => {
      const order = { system: 'light', light: 'dark', dark: 'system' };
      savePreferences({ theme: order[userPrefs.theme] || 'system' });
    });
  }
  const rpp = document.getElementById('resultsPerPage');
  if (rpp && !rpp.dataset.prefBound) {
    rpp.dataset.prefBound = '1';
    rpp.value = String(userPrefs.results_per_page || 25);
    rpp.addEventListener('change', () => {
      const n = parseInt(rpp.value, 10);
      if (!Number.isNaN(n) && n > 0) savePreferences({ results_per_page: n });
    });
  }
}
```

Call `bindPreferenceControls();` at the end of `initNav()` so it runs on every page load regardless of auth state (the controls simply may not exist yet on pages without them — the guards handle that).

> **Note for the executor:** if the `themeToggle` button or a `resultsPerPage` `<select>` does not yet exist in the templates (because the UI accessibility/theming plan has not landed), the `bindPreferenceControls` guards make this a harmless no-op. The persistence layer (Tasks 1–2) and theme application on hydration still work. Adding the `resultsPerPage` `<select>` to `search.html` is a small follow-up; this plan does not block on it.

Default-filters write-through: persist the *current* filter selection as the new default when the user runs a search. At the end of the successful branch in `runSearch` (right after `saveRecentSearch(payload.query);`, ~line 267), add:

```javascript
    savePreferences({
      default_filters: {
        filetype: chipFiletype?.values() ?? [],
        tags: chipTagFilter?.values() ?? [],
        path: pathFilter.value || '',
        block_type: blockType.value || '',
      },
    });
```

(Fire-and-forget — no `await`, so search rendering is never blocked by the prefs write.)

- [ ] **Step 5: Structural sanity checks**

```powershell
Select-String -Path document_search/web/static/app.js -Pattern "hydratePreferences|savePreferences|applyPreferences|userPrefs.results_per_page" | Select-Object -First 12
```

Expected: matches showing hydration is called from both `login` and `bootstrap`, write-through from `runSearch`/controls, and the search limit reads `userPrefs.results_per_page`. Confirm there is no remaining bare `limit: 25` in `runSearch`:

```powershell
Select-String -Path document_search/web/static/app.js -Pattern "limit: 25"
```

Expected: no matches inside `runSearch` (the only `25` left is the default fallback `|| 25`).

- [ ] **Step 6: Manual smoke test**

```powershell
$env:PYTHONPATH = "."; uvicorn document_search.app:app --port 8080
```

In a browser at `http://localhost:8080`:
1. Sign in (`admin` / `admin`). Confirm no console errors and the page paints.
2. If a results-per-page control exists, change it; run a search; confirm the request `limit` matches (Network tab → `/api/search` payload).
3. Run a search with a filetype filter set; reload the page; confirm the filter is pre-filled (hydrated from the server, not just `localStorage`).
4. Verify server-side persistence independent of the browser cache:

```powershell
$body = '{"username":"admin","password":"admin"}'
$resp = Invoke-RestMethod -Uri http://localhost:8080/api/login -Method POST -Body $body -ContentType 'application/json'
$h = @{ "X-Auth-Token" = $resp.token }
Invoke-RestMethod -Uri http://localhost:8080/api/preferences -Method PUT -Headers $h `
  -Body '{"theme":"dark","results_per_page":50}' -ContentType 'application/json'
Invoke-RestMethod -Uri http://localhost:8080/api/preferences -Headers $h
Invoke-RestMethod -Uri http://localhost:8080/api/me -Headers $h
```

Expected: the `PUT` echoes the merged prefs; the `GET` and `/api/me` both report `theme=dark`, `results_per_page=50`. Stop the server with `Ctrl+C`.

- [ ] **Step 7: Commit**

```powershell
git add document_search/web/static/app.js
git commit -m "feat(preferences): hydrate theme/results-per-page/filters from server, write through"
```

---

## Task 4: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full suite cleanly**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: green, zero failures. The 12 new preference tests pass; no previously-passing test regresses.

- [ ] **Step 2: Confirm isolation + defaults end-to-end (Python smoke)**

```powershell
$env:PYTHONPATH = "."; python -c "
from fastapi.testclient import TestClient
from document_search.app import create_app
import tempfile, pathlib
tmp = pathlib.Path(tempfile.mkdtemp())
app = create_app(str(tmp / 'prefs.db'))
with TestClient(app) as c:
    tok = c.post('/api/login', json={'username':'admin','password':'admin'}).json()['token']
    h = {'X-Auth-Token': tok}
    print('defaults =', c.get('/api/preferences', headers=h).json())
    print('put =', c.put('/api/preferences', headers=h, json={'theme':'dark','results_per_page':40}).json())
    print('me =', c.get('/api/me', headers=h).json()['preferences'])
print('OK')
"
```

Expected: defaults show `theme=system`/`results_per_page=25`; `put` and `me` both show `theme=dark`/`results_per_page=40`; prints `OK`.

- [ ] **Step 3: No final commit if no changes**

This task makes no changes; no commit required.

---

## Definition of Done

- [ ] `$env:PYTHONPATH = "."; pytest -q` is green on a clean clone; 12 new preference tests pass.
- [ ] `user_preferences` table exists (`user_id` PK, `prefs_json`, `updated_at`, FK → `users` with `ON DELETE CASCADE`).
- [ ] `SqliteStore.get_preferences(user_id)` returns `DEFAULT_PREFERENCES` for a user with no row, merges stored values over defaults key-by-key, and returns a fresh (mutable) copy.
- [ ] `SqliteStore.set_preferences(user_id, prefs)` upserts exactly one row per user, drops unknown keys, and merges over current values.
- [ ] `GET /api/preferences` and `PUT /api/preferences` exist, both scoped via `require_user` to the caller's own `user_id`; both return `401` without a valid session.
- [ ] Preferences are isolated between users (verified via TestClient with two distinct users).
- [ ] `/api/me` includes a `preferences` field.
- [ ] `app.js` hydrates theme/results-per-page/default-filters from `/api/me` on login and bootstrap, applies the theme via `document.documentElement` `data-theme` (coordinating with the dark-mode toggle), writes through to `/api/preferences` on change, and keeps `localStorage` (`seekr_prefs`) as an offline cache.
- [ ] `runSearch` uses `userPrefs.results_per_page` instead of a hard-coded `25`.
- [ ] Manual smoke test passes (cross-"device" persistence confirmed via direct API calls, independent of browser `localStorage`).

---

## Notes for the executing agent

- **Why a JSON blob, not one column per preference:** the three known keys include a nested `default_filters` object, and the roadmap anticipates more keys over time. A single `prefs_json` column with a merge-over-defaults read means adding a new preference is a one-line change to `DEFAULT_PREFERENCES` — no `ALTER TABLE`, no migration, and old rows automatically inherit the new default. The trade-off (no per-key SQL querying) is irrelevant here: preferences are always read as a whole for one user.
- **Merge-over-defaults is load-bearing.** Never return the raw stored blob. Always layer it over a deep copy of `DEFAULT_PREFERENCES` so a row written before a key existed still yields a complete object. This is what makes the schema forward-compatible without migrations.
- **Caller-scoping is the only authorization.** Both endpoints derive `user_id` strictly from `require_user(x_auth_token)` and never accept a `user_id` from the request body or path. There is intentionally no admin override — managing another user's UI prefs is out of scope. Do not add a `user_id` query/path parameter.
- **`exclude_none=True` enables partial updates.** A `PUT` with only `{"theme":"dark"}` must not wipe `results_per_page`. The Pydantic model makes every field optional and the endpoint drops `None`s, so the store's merge preserves untouched keys. Keep this contract.
- **Theme coordination with the dark-mode plan.** The `[data-theme="dark"]` CSS token block and the `#themeToggle` button live in `2026-06-05-ui-accessibility-and-theming.md`. This plan only *persists and applies* the value. `theme: "system"` deliberately **removes** the `data-theme` attribute so `prefers-color-scheme` takes over — do not set `data-theme="system"` (there is no such token). If the theming plan has not landed, applying `data-theme` is still harmless; the persistence works regardless.
- **`localStorage` is a cache, not the source of truth.** On bootstrap the cached `seekr_prefs` paints the theme instantly (avoiding a flash), then `/api/me` reconciles. Writes are optimistic-local then written through. If the server write fails, the change survives locally and the user is toasted — but the server remains authoritative on the next successful hydration.
- **Order of hydration vs. chip construction.** `applyPreferences` pre-fills the search filter chips, so it must run after the `ChipInput` instances exist and after `loadFilterOptions()` has populated their option lists. `applyPreferences` is idempotent — calling `hydratePreferences()` once early (for the theme) and again after `loadFilterOptions()` (for the filters) is the safe, simple ordering.
- **Default-filters write-through on search is intentional but cheap.** Persisting the current filter selection every time a search runs keeps "what I last searched with" as the default without a separate "save as default" button. It is fire-and-forget (no `await`) so it never delays result rendering. If a future plan adds an explicit "remember filters" toggle, gate this call behind it.
- **`results_per_page` is not yet surfaced in a control.** The frontend reads it for the search `limit` and binds an optional `#resultsPerPage` `<select>` if present. Adding that `<select>` to `search.html` is a trivial follow-up that this plan does not require; the value is fully functional via the API in the meantime.
