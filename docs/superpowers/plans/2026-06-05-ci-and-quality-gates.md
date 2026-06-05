# CI and Quality Gates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the project's first automated quality gate. Add a GitHub Actions CI workflow that installs system + Python deps, runs `ruff check`, then `pytest -q` on every push and pull request and is required to be green. Add a `ruff` configuration (and a lenient `mypy` config) consistent with `AGENTS.md` (PEP8 + type hints), pin them in a new `requirements-dev.txt`, and close the biggest gap in test coverage: the file extractors and the upload/path-traversal and system-update flows currently have **zero** tests.

**Architecture:** Quality tooling is config-only and additive — no source behaviour changes. `[tool.ruff]` and `[tool.mypy]` live in a new top-level `pyproject.toml`; dev tools are pinned in `requirements-dev.txt` (kept separate from the runtime `requirements.txt` so the Docker image stays lean). CI is a single workflow file `.github/workflows/ci.yml` running on `ubuntu-latest` with the four system packages from the README (`antiword`, `catdoc`, `poppler-utils`, `tesseract-ocr`) installed via `apt-get`. New tests follow the existing `tests/` conventions: real `tmp_path` fixtures, `pytest.importorskip("fastapi")` for integration tests, `monkeypatch` for subprocess/Ollama isolation, and small committed fixture files for the extractors.

**Tech Stack:** Python 3.11 (CI pins 3.11; the README states 3.11+), GitHub Actions, Ruff, mypy (lenient), pytest. No new runtime dependencies — every new package is dev-only.

**Scope boundaries:**

In scope:
- `.github/workflows/ci.yml` — lint + test on push/PR, gated green.
- `pyproject.toml` with `[tool.ruff]` + `[tool.ruff.lint]` + `[tool.mypy]`.
- `requirements-dev.txt` pinning `ruff`, `mypy`, and the `pytest` already used by the suite.
- A first `ruff check` pass with a **narrow**, file-scoped triage of pre-existing violations (auto-fix the safe ones, per-file-ignore the rest — never a blanket project-wide suppression).
- New tests:
  - `tests/test_extractors.py` — real `.txt` / `.md` extraction via committed fixtures + `LegacyOfficeTextExtractor` with `subprocess` monkeypatched, plus a documented path to add a binary `.docx` fixture.
  - `tests/test_upload_validation.py` — extension allow-list, path-traversal rejection on `target_subpath`, oversize-metadata rejection.
  - `tests/test_update_flow.py` — `/api/update/run` with `subprocess.run` monkeypatched (no real `update.sh`, no process replacement), plus the admin-only and feature-flag guards.
- Two tiny committed fixture files under `tests/fixtures/`.

Out of scope (deferred / explicitly NOT done here):
- No `pre-commit` hook config (CI is the gate; a local hook can come later).
- No `ruff format` / auto-formatting the whole tree (format churn would bury the real diff; lint-only for now).
- No strict `mypy` (the codebase is not annotated end-to-end; we start lenient and tighten in a later plan).
- No coverage threshold / `pytest-cov` gate (add once the suite is broader).
- No changes to any `document_search/**` source file. If `ruff check` surfaces a violation that cannot be fixed without touching source, prefer a per-file ignore in this plan and leave the source untouched (a separate `chore: ruff cleanup` plan handles real fixes).

---

## File Structure

**Create:**
- `pyproject.toml` — `[tool.ruff]`, `[tool.ruff.lint]`, `[tool.ruff.lint.isort]`, `[tool.ruff.lint.per-file-ignores]`, `[tool.mypy]`.
- `requirements-dev.txt` — `ruff`, `mypy`, `pytest` (and `httpx` for the FastAPI `TestClient`).
- `.github/workflows/ci.yml` — the CI pipeline.
- `tests/fixtures/sample.txt` — committed plain-text fixture.
- `tests/fixtures/sample.md` — committed Markdown fixture.
- `tests/test_extractors.py` — extractor unit tests.
- `tests/test_upload_validation.py` — upload validation / path-traversal tests.
- `tests/test_update_flow.py` — update endpoint tests (subprocess mocked).

**Modify:**
- `README.md` — add a short "Linting & Type Checks" subsection under Development Setup (commands only, no behaviour change).

**Untouched (must NOT be edited by this plan):**
- Everything under `document_search/` (source). Quality violations are handled via config ignores, not source edits.
- `requirements.txt` (runtime deps stay as-is; dev tools go in the new file).

---

## Key design decisions (locked)

- **CI runs on `ubuntu-latest`, not Windows.** The dev host is Windows 11, but the app ships in a Linux container and its system deps (`antiword`, `catdoc`, `poppler-utils`, `tesseract-ocr`) are apt packages. Pinning CI to Ubuntu matches production and keeps `apt-get` simple.
- **CI pins Python 3.11.** The README states "Python 3.11+". The local dev box runs 3.13, but production is 3.11 (`Dockerfile`), so CI uses 3.11 to catch 3.11-specific issues. The new tests use only stdlib features available in 3.11 (no `tomllib`-only or 3.12+ syntax).
- **`ruff check` runs before `pytest`.** Lint is cheap and fails fast; no point running the suite on code that won't lint. Both steps run with `if: always()` semantics? No — fail-fast is desired, so lint gates tests. (A maintainer wanting both reports can split into two jobs later.)
- **Line length 120.** `AGENTS.md` mandates PEP8 + type hints; the existing source has many lines in the 90–115 range (e.g. `legacy_office_extractor.py:16`). Black's default 88 would generate a huge violation set with no behaviour value. 120 is PEP8-compatible (PEP8 permits teams to raise the limit) and keeps the first `ruff check` diff small.
- **Lint rule set starts conservative:** `E`, `F`, `W` (pycodestyle + pyflakes), `I` (isort), `UP` (pyupgrade), `B` (bugbear). This catches real bugs (unused imports, mutable defaults) without drowning in stylistic noise. `D` (docstrings) and `ANN` (annotation completeness) are deliberately **off** for the first pass.
- **Pre-existing violations are triaged narrowly.** Run `ruff check`, apply `ruff check --fix` for the auto-fixable safe set (import sort, unused imports), then for anything left that would require a source edit, add a targeted `per-file-ignores` entry referencing the specific rule code — never `# noqa` blanket lines and never a project-wide `ignore` of a whole rule family. Document each ignore with a one-line reason.
- **mypy is lenient and non-blocking in CI for now.** It runs in the workflow as an informational step (`continue-on-error: true`) so the type-checking baseline is visible without blocking merges before the codebase is annotated. Tightening to a gate is a later plan.

---

## Task 1: Dev requirements file

**Files:**
- Create: `requirements-dev.txt`

- [ ] **Step 1: Create `requirements-dev.txt`**

This file pins the developer/CI-only tools. It does NOT duplicate runtime deps from `requirements.txt`; CI installs both. `httpx` is required because FastAPI's `TestClient` (used by the existing and new integration tests) depends on it, and it is not currently in `requirements.txt`.

```text
# Developer / CI-only tooling. Install with:
#   pip install -r requirements.txt -r requirements-dev.txt
ruff>=0.6.0
mypy>=1.11.0
pytest>=8.0.0
httpx>=0.27.0
```

- [ ] **Step 2: Verify it installs locally**

```powershell
$env:PYTHONPATH = "."; python -m pip install -r requirements-dev.txt
python -m ruff --version
python -m mypy --version
```

Expected: both print a version. If `ruff`/`mypy` were not previously installed, this is the first install.

- [ ] **Step 3: Commit**

```powershell
git add requirements-dev.txt
git commit -m "chore: add requirements-dev.txt for ruff/mypy/pytest"
```

---

## Task 2: Ruff + mypy configuration

**Files:**
- Create: `pyproject.toml`

- [ ] **Step 1: Create `pyproject.toml`**

Create the file at the repository root with exactly this content. (There is currently no `pyproject.toml`; this is the first one. It contains only tool config — no build-system / packaging section, because Seekr is run from source, not pip-installed.)

```toml
[tool.ruff]
# AGENTS.md mandates PEP8 + type hints. Line length 120 keeps PEP8 compliance
# while avoiding a large reformat of the existing source (many lines are 90-115).
line-length = 120
target-version = "py311"
# Only lint the application package and the tests; never the build/venv dirs.
src = ["document_search", "tests"]
extend-exclude = [
    ".venv",
    "build",
    "dist",
    "docs",
    "deploy",
    "scripts",
]

[tool.ruff.lint]
# Conservative, high-signal rule set:
#   E/W  pycodestyle (PEP8)        F  pyflakes (real bugs: unused imports/names)
#   I    isort (import ordering)   UP pyupgrade (py311 idioms)
#   B    flake8-bugbear (likely bugs: mutable defaults, etc.)
select = ["E", "W", "F", "I", "UP", "B"]
ignore = [
    # E501 line-too-long is handled by `line-length` above; the formatter is off,
    # so we keep E501 active. Nothing ignored globally at the rule-family level.
]

[tool.ruff.lint.isort]
known-first-party = ["document_search"]
combine-as-imports = true

[tool.ruff.lint.per-file-ignores]
# Tests favour readability over import hygiene: module-level imports placed after
# an importorskip guard (E402) and unused-fixture-style argument names are normal.
"tests/*" = ["E402"]
# NOTE: Task 3 may append narrowly-scoped, code-referenced ignores here for any
# pre-existing source violation that cannot be auto-fixed. Each must carry a
# one-line reason. Do NOT add blanket file ignores.

[tool.mypy]
# Lenient baseline. The codebase is not fully annotated yet, so we start
# permissive and tighten in a later plan. CI runs mypy informationally
# (continue-on-error) until the baseline is clean.
python_version = "3.11"
warn_unused_configs = true
ignore_missing_imports = true
no_implicit_optional = true
warn_redundant_casts = true
# Deliberately OFF for the first pass (enable incrementally later):
disallow_untyped_defs = false
disallow_incomplete_defs = false
check_untyped_defs = false
files = ["document_search"]

[[tool.mypy.overrides]]
# Third-party libs without bundled type stubs — silence missing-stub noise.
module = [
    "docx.*",
    "pptx.*",
    "pypdf.*",
    "pdf2image.*",
    "pytesseract.*",
    "fitz.*",
]
ignore_missing_imports = true
```

- [ ] **Step 2: Sanity-check the config parses**

```powershell
$env:PYTHONPATH = "."; python -m ruff check --show-settings . | Select-Object -First 5
```

Expected: prints resolved settings (line-length 120, the selected rules). Any TOML parse error shows here.

- [ ] **Step 3: Commit**

```powershell
git add pyproject.toml
git commit -m "chore: add ruff + lenient mypy config in pyproject.toml"
```

---

## Task 3: First `ruff check` pass and narrow triage

**Files:**
- Modify (config only, if needed): `pyproject.toml` (`[tool.ruff.lint.per-file-ignores]`)
- Do NOT modify any `document_search/**` source in this task.

This task surfaces pre-existing violations. The goal is a clean `ruff check` exit (code 0) achieved by (a) safe auto-fixes and (b) **narrow** per-file ignores — never mass suppression.

- [ ] **Step 1: Run the first check and capture the violation list**

```powershell
$env:PYTHONPATH = "."; python -m ruff check . --output-format=concise
```

Read the full list. Group findings by rule code (`E501`, `F401`, `I001`, `UP032`, `B008`, …).

- [ ] **Step 2: Apply only the safe auto-fixes**

`ruff` auto-fixes import sorting (`I001`), unused imports (`F401`), and most `UP` rewrites without changing behaviour. Apply just those:

```powershell
$env:PYTHONPATH = "."; python -m ruff check . --fix
```

Re-run `ruff check .` and re-read the remaining list. Auto-fixes touch source files — that is acceptable here because they are mechanical and behaviour-preserving (import order, dropping a genuinely unused import). Review the diff:

```powershell
git diff --stat
```

If any auto-fix removed an import that is actually used only for a side effect (rare), revert that single hunk and instead add a per-file ignore.

- [ ] **Step 3: Triage what remains — per-file ignore, NOT blanket**

For each remaining violation that you are NOT going to fix in source in this plan:

- If it is **`B008`** (function call in default argument) — this is FastAPI's `Header(default=None)` / `File(...)` idiom, which is correct and intentional. Add a scoped ignore for the app module:

  In `pyproject.toml` under `[tool.ruff.lint.per-file-ignores]`, append:
  ```toml
  # FastAPI dependency-injection defaults (Header(...), File(...), Form(...)) are
  # the documented idiom; B008 (call-in-default-arg) is a false positive here.
  "document_search/app.py" = ["B008"]
  ```

- If it is **`E501`** (line too long) on a handful of lines and the line is a long SQL string or URL that cannot be cleanly wrapped — prefer wrapping. Only if wrapping a line would require a source edit you are choosing to defer, add a single trailing `# noqa: E501` **on that exact line** (this is the one allowed inline noqa, and it must name the code). Record each such line in the commit message.

- For any other code, fix it in source ONLY if the fix is trivial and behaviour-preserving (e.g. an unused local). Otherwise add the narrow per-file ignore with a one-line reason comment.

**Do not** add `ignore = [...]` entries that disable a rule across the whole project, and **do not** add a `"document_search/*" = [...]` wildcard. Each ignore names a specific file and a specific code.

- [ ] **Step 4: Achieve a clean check**

```powershell
$env:PYTHONPATH = "."; python -m ruff check .
```

Expected: `All checks passed!` and exit code 0.

```powershell
$LASTEXITCODE   # must be 0
```

- [ ] **Step 5: Run mypy (informational)**

```powershell
$env:PYTHONPATH = "."; python -m mypy
```

Note the count of findings; it is fine if this is non-zero — mypy is non-blocking for now. Do not change source to satisfy it in this plan.

- [ ] **Step 6: Confirm the test suite still passes after auto-fixes**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: the existing suite passes unchanged (the auto-fixes are behaviour-preserving). If anything fails, a "safe" auto-fix was not safe — revert that hunk and replace it with a per-file ignore.

- [ ] **Step 7: Commit**

```powershell
git add pyproject.toml document_search tests
git commit -m "chore: clean ruff baseline (auto-fix imports, narrow per-file ignores)"
```

(If Step 2 produced no source changes, drop `document_search` from the `git add` line.)

---

## Task 4: CI workflow

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Create the workflow**

Create `.github/workflows/ci.yml` with exactly this content:

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

# Cancel superseded runs on the same ref to save minutes.
concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

jobs:
  lint-and-test:
    name: Lint and test (py3.11, ubuntu)
    runs-on: ubuntu-latest

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python 3.11
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: pip
          cache-dependency-path: |
            requirements.txt
            requirements-dev.txt

      - name: Install system packages
        run: |
          sudo apt-get update
          sudo apt-get install -y --no-install-recommends \
            antiword \
            catdoc \
            poppler-utils \
            tesseract-ocr \
            tesseract-ocr-eng \
            tesseract-ocr-deu
          # Verify the legacy-office + OCR tools the extractors shell out to.
          antiword -h >/dev/null 2>&1 || true
          tesseract --version

      - name: Install Python dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt -r requirements-dev.txt

      - name: Ruff lint (gate)
        run: ruff check .

      - name: mypy (informational)
        run: mypy
        continue-on-error: true

      - name: Run tests
        env:
          PYTHONPATH: .
        run: pytest -q
```

Notes baked into the file:
- `ruff check .` is a hard gate — a lint failure fails the job (and the required check), blocking merge.
- `mypy` is `continue-on-error: true` — informational until the type baseline is clean.
- `PYTHONPATH: .` mirrors the documented local invocation (`$env:PYTHONPATH = "."; pytest -q`).
- `pip cache` keyed on both requirement files speeds repeat runs.

- [ ] **Step 2: Lint the YAML locally (optional but recommended)**

If `yamllint` is available:

```powershell
python -m yamllint .github/workflows/ci.yml
```

If not installed, skip — GitHub will validate it on push. Do NOT add `yamllint` to the deps for this.

- [ ] **Step 3: Commit**

```powershell
git add .github/workflows/ci.yml
git commit -m "ci: add lint+test GitHub Actions workflow on push/PR"
```

- [ ] **Step 4: Make the check required (manual, post-merge)**

After this lands on `main` and the workflow has run once, a repository admin sets **Settings → Branches → Branch protection rule for `main` → Require status checks → `Lint and test (py3.11, ubuntu)`**. This step is a GitHub UI action, not code — note it in the PR description so the maintainer performs it. (It cannot be done from the workflow file itself.)

---

## Task 5: Committed extractor fixtures + extractor tests

**Files:**
- Create: `tests/fixtures/sample.txt`
- Create: `tests/fixtures/sample.md`
- Create: `tests/test_extractors.py`

The `.txt` and `.docx` extractors have **zero** tests today. `TxtTextExtractor` and `MdTextExtractor` read real bytes, so we commit tiny real fixtures. `LegacyOfficeTextExtractor` shells out to `antiword`/`catppt`; we test it by monkeypatching `subprocess.run` so the test is hermetic and needs no system package.

- [ ] **Step 1: Create the fixture files**

`tests/fixtures/sample.txt`:

```text
Seekr indexing smoke fixture.
Invoice number 12345 for customer ACME.
```

`tests/fixtures/sample.md`:

```text
# Heading One

Body paragraph with the keyword **annualreport** inside it.

- list item alpha
- list item beta
```

- [ ] **Step 2: Write the extractor tests**

Create `tests/test_extractors.py`:

```python
"""Extractor unit tests.

Covers the previously-untested text/markdown extractors against real committed
fixtures, and the legacy-office extractor with subprocess monkeypatched so the
test is hermetic (no antiword/catppt install required).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from document_search.extractors.legacy_office_extractor import LegacyOfficeTextExtractor
from document_search.extractors.md_extractor import MdTextExtractor
from document_search.extractors.txt_extractor import TxtTextExtractor

FIXTURES = Path(__file__).parent / "fixtures"


def test_txt_extractor_reads_committed_fixture():
    result = TxtTextExtractor().extract(FIXTURES / "sample.txt")
    assert result.status == "ok"
    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.block_type == "text_file"
    assert "Invoice number 12345" in block.text
    assert block.extractor == "TxtTextExtractor"


def test_md_extractor_reads_committed_fixture():
    result = MdTextExtractor().extract(FIXTURES / "sample.md")
    assert result.status == "ok"
    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.block_type == "markdown"
    assert "annualreport" in block.text
    assert "Heading One" in block.text


def test_txt_extractor_empty_file_yields_no_blocks(tmp_path: Path):
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    result = TxtTextExtractor().extract(empty)
    assert result.status == "ok"
    assert result.blocks == []


def test_txt_extractor_tolerates_bad_bytes(tmp_path: Path):
    # extract() uses errors="ignore"; invalid UTF-8 must not raise.
    bad = tmp_path / "bad.txt"
    bad.write_bytes(b"hello \xff\xfe world")
    result = TxtTextExtractor().extract(bad)
    assert result.status == "ok"
    assert "hello" in result.blocks[0].text


def test_legacy_office_extractor_success_with_mocked_subprocess(monkeypatch, tmp_path: Path):
    doc = tmp_path / "legacy.doc"
    doc.write_bytes(b"\xd0\xcf\x11\xe0fake-ole")  # OLE magic, content irrelevant

    def fake_run(cmd, **kwargs):
        assert cmd[0] == "antiword"
        assert cmd[1] == str(doc)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="Extracted legacy text", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = LegacyOfficeTextExtractor().extract(doc)
    assert result.status == "ok"
    assert result.blocks[0].block_type == "legacy_document"
    assert result.blocks[0].text == "Extracted legacy text"


def test_legacy_office_extractor_ppt_uses_catppt(monkeypatch, tmp_path: Path):
    ppt = tmp_path / "slides.ppt"
    ppt.write_bytes(b"\xd0\xcf\x11\xe0fake-ole")
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd0"] = cmd[0]
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="slide text", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = LegacyOfficeTextExtractor().extract(ppt)
    assert seen["cmd0"] == "catppt"
    assert result.status == "ok"


def test_legacy_office_extractor_nonzero_returncode_is_error(monkeypatch, tmp_path: Path):
    doc = tmp_path / "broken.doc"
    doc.write_bytes(b"\xd0\xcf\x11\xe0")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=2, stdout="", stderr="antiword: cannot parse")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = LegacyOfficeTextExtractor().extract(doc)
    assert result.status == "error"
    assert "cannot parse" in (result.error_message or "")


def test_legacy_office_extractor_missing_binary_is_error(monkeypatch, tmp_path: Path):
    doc = tmp_path / "x.doc"
    doc.write_bytes(b"\xd0\xcf\x11\xe0")

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = LegacyOfficeTextExtractor().extract(doc)
    assert result.status == "error"
    assert "not installed" in (result.error_message or "")


@pytest.mark.skipif(
    pytest.importorskip("docx", reason="python-docx not installed") is None,
    reason="python-docx required",
)
def test_docx_extractor_with_generated_fixture(tmp_path: Path):
    """A real .docx is a zip — we can't commit a meaningful one by hand-typing
    bytes, so generate a minimal one at test time with python-docx (a runtime
    dep, so always available in CI). This exercises the real DocxTextExtractor.

    To commit a STATIC .docx fixture instead (for environments without
    python-docx), run once locally:
        python -c "from docx import Document; d=Document(); \\
            d.add_heading('Title', level=1); d.add_paragraph('Body text here'); \\
            d.save('tests/fixtures/sample.docx')"
    and load it from FIXTURES like the .txt/.md tests above. We generate
    on-the-fly here to avoid committing an opaque binary blob.
    """
    from docx import Document

    from document_search.extractors.docx_extractor import DocxTextExtractor

    path = tmp_path / "generated.docx"
    doc = Document()
    doc.add_heading("Quarterly Title", level=1)
    doc.add_paragraph("Body paragraph mentioning invoice 9876.")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Key"
    table.rows[0].cells[1].text = "Value"
    doc.save(str(path))

    result = DocxTextExtractor().extract(path)
    assert result.status == "ok"
    all_text = " ".join(b.text for b in result.blocks)
    assert "Quarterly Title" in all_text
    assert "invoice 9876" in all_text
    block_types = {b.block_type for b in result.blocks}
    assert "table" in block_types
```

- [ ] **Step 3: Run the extractor tests**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_extractors.py -v
```

Expected: all pass. The `docx` test runs because `python-docx` is a runtime dependency (`requirements.txt`); it only skips in a stripped environment.

- [ ] **Step 4: Lint the new test file**

```powershell
$env:PYTHONPATH = "."; python -m ruff check tests/test_extractors.py
```

Expected: clean (the `tests/*` per-file-ignore covers `E402`).

- [ ] **Step 5: Commit**

```powershell
git add tests/fixtures/sample.txt tests/fixtures/sample.md tests/test_extractors.py
git commit -m "test: cover txt/md/legacy/docx extractors with fixtures and mocks"
```

---

## Task 6: Upload validation + path-traversal tests

**Files:**
- Create: `tests/test_upload_validation.py`

The `/api/upload` route (`document_search/app.py:661`) enforces: an extension allow-list, an 8 KB `metadata_json` cap, and a `target_subpath` containment check (`upload_root.resolve() not in target.parents`). None of these are tested. These tests use `monkeypatch.setenv("DOCUMENT_SEARCH_UPLOAD_ROOT", ...)` BEFORE `create_app`, so uploads land inside `tmp_path` and never touch a real `/documents/uploads`.

- [ ] **Step 1: Write the tests**

Create `tests/test_upload_validation.py`:

```python
"""Upload-endpoint validation tests: extension allow-list, oversize metadata,
and path-traversal rejection on target_subpath.

The upload root is redirected into tmp_path via the DOCUMENT_SEARCH_UPLOAD_ROOT
env var set BEFORE create_app, so nothing is written outside the temp dir.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi.testclient import TestClient  # noqa: E402

from document_search.app import create_app  # noqa: E402


def _login(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    monkeypatch.setenv("DOCUMENT_SEARCH_UPLOAD_ROOT", str(upload_root))
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as c:
        c.upload_root = upload_root  # stash for assertions
        yield c


def test_upload_rejects_disallowed_extension(client):
    token = _login(client)
    r = client.post(
        "/api/upload",
        headers={"X-Auth-Token": token},
        files={"file": ("evil.exe", b"MZ\x90\x00", "application/octet-stream")},
        data={"target_subpath": "", "tags": "", "metadata_json": "{}"},
    )
    assert r.status_code == 400
    assert "Unsupported file extension" in r.text


def test_upload_rejects_oversize_metadata(client):
    token = _login(client)
    big = "{" + '"k":"' + ("x" * 9000) + '"}'  # > 8192 bytes
    r = client.post(
        "/api/upload",
        headers={"X-Auth-Token": token},
        files={"file": ("ok.txt", b"hello world", "text/plain")},
        data={"target_subpath": "", "tags": "", "metadata_json": big},
    )
    assert r.status_code == 400
    assert "8 KB" in r.text


@pytest.mark.parametrize("evil", ["../escape", "../../etc", "/etc", "sub/../../escape"])
def test_upload_rejects_path_traversal_subpath(client, evil):
    token = _login(client)
    r = client.post(
        "/api/upload",
        headers={"X-Auth-Token": token},
        files={"file": ("ok.txt", b"hello", "text/plain")},
        data={"target_subpath": evil, "tags": "", "metadata_json": "{}"},
    )
    assert r.status_code == 400
    assert "Invalid target_subpath" in r.text
    # Nothing escaped the upload root.
    escaped = list((client.upload_root.parent).glob("escape*"))
    assert escaped == []


def test_upload_accepts_valid_txt_into_upload_root(client):
    token = _login(client)
    r = client.post(
        "/api/upload",
        headers={"X-Auth-Token": token},
        files={"file": ("note.txt", b"indexable body text", "text/plain")},
        data={"target_subpath": "inbox", "tags": "alpha,beta", "metadata_json": "{}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "uploaded"
    # File landed strictly inside the redirected upload root.
    stored = client.upload_root.resolve()
    assert stored in (client.upload_root / "inbox").resolve().parents or \
        (client.upload_root / "inbox").resolve() == stored
    written = list((client.upload_root / "inbox").glob("note_*.txt"))
    assert len(written) == 1


def test_upload_requires_authentication(client):
    r = client.post(
        "/api/upload",
        files={"file": ("ok.txt", b"hello", "text/plain")},
        data={"target_subpath": "", "tags": "", "metadata_json": "{}"},
    )
    assert r.status_code in (401, 403)
```

- [ ] **Step 2: Run the upload tests**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_upload_validation.py -v
```

Expected: all pass.

If `test_upload_accepts_valid_txt_into_upload_root` fails because the upload route calls into the Ollama organizer (`organizer.suggest(...)`) and the network is unavailable, the organizer must degrade gracefully (it returns a best-effort suggestion when Ollama is down). If it raises instead, narrow the test to assert the validation-only behaviour and skip the happy-path file write — but first confirm the organizer's offline behaviour by reading `document_search/services/ai_organizer.py`. Do not weaken the path-traversal assertions.

- [ ] **Step 3: Lint**

```powershell
$env:PYTHONPATH = "."; python -m ruff check tests/test_upload_validation.py
```

- [ ] **Step 4: Commit**

```powershell
git add tests/test_upload_validation.py
git commit -m "test: upload extension/metadata/path-traversal validation"
```

---

## Task 7: Update-flow tests (subprocess mocked)

**Files:**
- Create: `tests/test_update_flow.py`

The `/api/update/run` route (`document_search/app.py:886`) shells out to `/app/scripts/update.sh` via `subprocess.run` and tracks state in the module-level `_update_job` dict. We test it WITHOUT a real script and WITHOUT spawning the updater, by (a) monkeypatching `Path.exists` so the script "exists", and (b) monkeypatching `subprocess.run` so the runner thread records a deterministic result. We also assert the admin-only guard and the feature-flag guard.

- [ ] **Step 1: Write the tests**

Create `tests/test_update_flow.py`:

```python
"""Tests for the in-app update flow (/api/update/run, /api/update/status).

subprocess.run is monkeypatched so no real update.sh executes and the running
process is never replaced. The update script's existence is faked so the route
proceeds past its `script.exists()` guard.
"""
from __future__ import annotations

import subprocess
import time

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi.testclient import TestClient  # noqa: E402

import document_search.app as app_module  # noqa: E402
from document_search.app import create_app  # noqa: E402


def _login(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_UI_UPDATE_ENABLED", "true")
    app = create_app(str(tmp_path / "t.db"))
    with TestClient(app) as c:
        yield c


def _wait_for_status(client, token, terminal, timeout=5.0):
    deadline = time.monotonic() + timeout
    body = {}
    while time.monotonic() < deadline:
        body = client.get("/api/update/status", headers={"X-Auth-Token": token}).json()
        if body.get("status") in terminal:
            return body
        time.sleep(0.02)
    return body


def test_update_run_success_records_done(client, monkeypatch):
    token = _login(client)
    # The route checks Path("/app/scripts/update.sh").exists(); force it True.
    monkeypatch.setattr(app_module.Path, "exists", lambda self: True)

    def fake_run(cmd, **kwargs):
        assert cmd[0] == "/bin/sh"
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="update ok", stderr="")

    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    r = client.post("/api/update/run", headers={"X-Auth-Token": token})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "started"

    final = _wait_for_status(client, token, {"done", "error"})
    assert final["status"] == "done"
    assert final["exit_code"] == 0
    assert "update ok" in final["stdout"]


def test_update_run_nonzero_records_error(client, monkeypatch):
    token = _login(client)
    monkeypatch.setattr(app_module.Path, "exists", lambda self: True)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="git pull failed")

    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    client.post("/api/update/run", headers={"X-Auth-Token": token})
    final = _wait_for_status(client, token, {"done", "error"})
    assert final["status"] == "error"
    assert final["exit_code"] == 1
    assert "git pull failed" in final["stderr"]


def test_update_run_requires_admin(client, monkeypatch):
    # A non-admin user must be rejected before any subprocess is touched.
    called = {"ran": False}
    monkeypatch.setattr(app_module.Path, "exists", lambda self: True)

    def fake_run(cmd, **kwargs):
        called["ran"] = True
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(app_module.subprocess, "run", fake_run)

    admin_token = _login(client)
    client.post(
        "/api/users",
        headers={"X-Auth-Token": admin_token},
        json={"username": "bob", "password": "bobpassword", "role": "user"},
    )
    user_token = _login(client, "bob", "bobpassword")
    r = client.post("/api/update/run", headers={"X-Auth-Token": user_token})
    assert r.status_code == 403
    assert called["ran"] is False


def test_update_run_disabled_by_feature_flag(client, monkeypatch):
    token = _login(client)
    monkeypatch.setenv("DOCUMENT_SEARCH_UI_UPDATE_ENABLED", "false")
    monkeypatch.setattr(app_module.Path, "exists", lambda self: True)
    r = client.post("/api/update/run", headers={"X-Auth-Token": token})
    assert r.status_code == 403
    assert "disabled" in r.text.lower()


def test_update_run_missing_script_is_404(client, monkeypatch):
    token = _login(client)
    monkeypatch.setattr(app_module.Path, "exists", lambda self: False)
    r = client.post("/api/update/run", headers={"X-Auth-Token": token})
    assert r.status_code == 404
```

- [ ] **Step 2: Verify the user-creation endpoint name**

`test_update_run_requires_admin` POSTs to `/api/users` to create a non-admin user. Confirm that path + payload shape against `document_search/app.py`:

```powershell
$env:PYTHONPATH = "."; python -m pytest -q "tests/test_update_flow.py::test_update_run_requires_admin" -v
```

If it errors with 404/422 on the create-user call, grep the real route:

```powershell
$env:PYTHONPATH = "."; python -m pytest -q tests/test_update_flow.py -k requires_admin -v
```

Then open `document_search/app.py`, search for the create-user route (`@app.post("/api/users"` or similar), and adjust the path/payload in the test to match. The other four tests do not depend on user creation.

- [ ] **Step 3: Run the full update-flow file**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_update_flow.py -v
```

Expected: 5 passing.

If `monkeypatch.setattr(app_module.Path, "exists", ...)` raises because `Path` is imported differently in `app.py`, confirm `app.py` does `from pathlib import Path` (it does — `app.py` uses `Path(...)` throughout). Patching `app_module.Path.exists` patches the class method globally for the duration of the test, which is acceptable here because the route is the only `Path.exists()` caller exercised in these tests. If a different test in the suite becomes flaky from this, switch to monkeypatching the specific bound check by wrapping the route — but the per-test `monkeypatch` scope auto-reverts, so cross-test bleed should not occur.

- [ ] **Step 4: Lint**

```powershell
$env:PYTHONPATH = "."; python -m ruff check tests/test_update_flow.py
```

- [ ] **Step 5: Commit**

```powershell
git add tests/test_update_flow.py
git commit -m "test: update-flow endpoint with subprocess mocked"
```

---

## Task 8: README docs + final verification

**Files:**
- Modify: `README.md` (Development Setup section)

- [ ] **Step 1: Document the dev tooling in the README**

In `README.md`, under the `## Running Tests` section, add a new subsection immediately after the existing code block (the one ending with `PYTHONPATH=. pytest -q`). Insert:

```markdown
### Linting & type checks

Developer tooling lives in `requirements-dev.txt` and is configured in `pyproject.toml`.

```bash
pip install -r requirements.txt -r requirements-dev.txt

# Lint (this is the CI gate)
ruff check .

# Auto-fix the safe set (imports, simple rewrites)
ruff check . --fix

# Type check (informational; not yet a gate)
mypy
```

CI runs `ruff check .` and `pytest -q` on every push and pull request via
`.github/workflows/ci.yml`; a failing lint or test blocks the merge.
```

(Use a 4-backtick outer fence in the actual README if needed so the inner triple-backtick block renders; or split into two fenced blocks. Match the README's existing fencing style — it uses triple backticks, so write the bash block as one fenced block and the prose as plain text around it.)

- [ ] **Step 2: Run the full suite + lint once more**

```powershell
$env:PYTHONPATH = "."; python -m ruff check .
$env:PYTHONPATH = "."; pytest -q
```

Expected: `ruff check` exits 0; `pytest` reports the prior baseline plus the new tests (extractors + upload + update flow), all passing.

- [ ] **Step 3: Confirm the workflow file is valid syntactically**

```powershell
python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml')); print('yaml ok')"
```

Expected: prints `yaml ok`. (`PyYAML` is already a runtime dep, so this works without extra installs.)

- [ ] **Step 4: Commit**

```powershell
git add README.md
git commit -m "docs: document ruff/mypy/CI in README development section"
```

- [ ] **Step 5: Push and open the PR**

```powershell
git push -u origin HEAD
gh pr create --title "ci: quality gates (ruff + mypy + pytest CI) and extractor/upload/update tests" --body "Adds the first CI pipeline, ruff/mypy config, and tests for previously-untested extraction, upload-validation, and update-flow paths. Reviewer: after merge, set the 'Lint and test (py3.11, ubuntu)' check as required in branch protection."
```

Confirm the workflow runs green on the PR before requesting review.

---

## Definition of Done

- [ ] `.github/workflows/ci.yml` exists; on a PR it installs `antiword`/`catdoc`/`poppler-utils`/`tesseract-ocr`, runs `ruff check .` (gating) and `pytest -q`, and reports `mypy` informationally.
- [ ] The CI run is green on the PR branch.
- [ ] `pyproject.toml` defines `[tool.ruff]` (line-length 120, py311, select E/W/F/I/UP/B), `[tool.ruff.lint.isort]` with `known-first-party = ["document_search"]`, and a lenient `[tool.mypy]`.
- [ ] `requirements-dev.txt` pins `ruff`, `mypy`, `pytest`, `httpx`.
- [ ] `ruff check .` exits 0 locally — achieved via safe auto-fixes + narrowly-scoped per-file ignores (each naming a file and a rule code with a reason). No project-wide rule-family suppression, no blanket `# noqa`.
- [ ] `tests/test_extractors.py` passes: txt/md against committed fixtures, legacy-office with `subprocess` mocked (success, ppt→catppt, non-zero, missing-binary), and a docx generated-fixture test.
- [ ] `tests/fixtures/sample.txt` and `tests/fixtures/sample.md` are committed.
- [ ] `tests/test_upload_validation.py` passes: bad extension → 400, oversize metadata → 400, path traversal (`../`, absolute) → 400 with nothing written outside the temp upload root, valid upload → 200, unauthenticated → 401/403.
- [ ] `tests/test_update_flow.py` passes: success→`done`, non-zero→`error`, non-admin→403 (subprocess never invoked), feature-flag-off→403, missing-script→404 — all with `subprocess.run` monkeypatched (no real `update.sh`, no process replacement).
- [ ] `README.md` documents `ruff check`, `ruff check --fix`, and `mypy`.
- [ ] `pytest -q` is fully green; no `document_search/**` behaviour changed (only mechanical ruff auto-fixes, if any).

---

## Notes for the executing agent

- **Why dev tools are split out of `requirements.txt`:** the production Docker image installs `requirements.txt` only; `ruff`/`mypy` have no place in the runtime image. Keeping them in `requirements-dev.txt` keeps the image lean while CI installs both files.
- **Why `httpx` is in dev-requirements:** FastAPI's `TestClient` (used by the existing `test_app_*.py` and the new `test_upload_validation.py` / `test_update_flow.py`) requires `httpx` at runtime. It is not in `requirements.txt` today, which means the integration tests can only run where `httpx` is already present. Pinning it as a dev dep makes the test suite reproducible in CI.
- **Why CI pins Python 3.11 while the dev host has 3.13:** production runs 3.11 (`Dockerfile`); CI must catch 3.11-specific issues. Do not bump the workflow to 3.13 to match the local box — the new tests are written to run on 3.11 (no 3.12+ syntax).
- **Why `mypy` is non-blocking:** the codebase predates the AGENTS.md type-hint mandate and is only partially annotated. A blocking mypy gate would fail immediately and force a large, risky annotation sweep. Starting lenient + informational lets the baseline be visible; a later plan tightens it (e.g. `disallow_untyped_defs = true` on `document_search/services/`).
- **Triage discipline (Task 3):** the single most important rule is *narrow* suppression. A reviewer should be able to read every ignore and understand exactly which file and which rule was suppressed and why. If you find yourself wanting to ignore a whole rule across the project, stop — that is a signal the rule should be removed from `select` instead, which is a deliberate config choice, not a suppression.
- **The `Path.exists` monkeypatch in Task 7** is broad (it patches the class method) but lives inside a per-test `monkeypatch` fixture, so it auto-reverts after each test. It is safe here because these tests do not run concurrently with others that depend on real `Path.exists()` within the same test. If you observe cross-test interference, prefer patching the route's script-existence check more surgically rather than weakening the assertions.
- **The docx fixture decision:** committing a hand-authored `.docx` is impractical (it is a zip of XML). The plan generates a real `.docx` at test time using `python-docx` (a runtime dep, always present in CI) so the *real* `DocxTextExtractor` is exercised end-to-end. The docstring records the one-liner to materialise a static `tests/fixtures/sample.docx` if a binary fixture is ever preferred.
- **Do not reformat the tree.** `ruff format` is intentionally out of scope. Running it would produce a massive diff that buries the meaningful changes and risks merge conflicts with the in-flight job-queue work. Lint-only for this plan.
- **Branch protection is a manual UI step.** The workflow cannot mark itself required; a maintainer must add the check name (`Lint and test (py3.11, ubuntu)`) to the `main` branch protection rule after the first run. Flag this in the PR body (Task 8 Step 5 does).
