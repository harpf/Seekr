# Scan-Eingang (Hot-Folder-Ingestion mit Review-Warteschlange) — Implementierungsplan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Netzwerk-Scanner legen Dokumente in überwachte Ordner ab; Seekr arbeitet sie nach und nach ab (OCR → Index → KI-Vorschlag auf bestehende Ordnerstruktur) und stellt sie nach menschlicher Bestätigung in einer Review-Warteschlange am richtigen Ort bereit.

**Architecture:** Eigener Ingestion-Weg parallel zum manuellen Upload, aufgesetzt auf das bestehende Job-System. Ein Watcher-Manager (watchdog-Events + Polling-Fallback) erkennt stabile Dateien, verschiebt sie ins Staging und reiht `scan_ingest`-Jobs ein. Der Job erzwingt OCR explizit (ohne globale Env-Umschaltung), indexiert, holt einen auf existierende Zielordner eingeschränkten KI-Vorschlag und legt eine `scan_review`-Zeile an. Eine Review-UI bestätigt/korrigiert und verschiebt über die vorhandene `move_document`-Logik.

**Tech Stack:** Python 3.11+, FastAPI, SQLite (FTS5), Jinja2 + Vanilla JS, Tesseract via bestehendem `ocr_service`, Prometheus (`observability.REGISTRY`), optional `watchdog`.

**Spec:** `docs/superpowers/specs/2026-06-11-scan-inbox-ingestion-design.md`

---

## Design-Präzisierungen (gegenüber der Spec)

Beim Sammeln der Code-Muster wurden drei Punkte konkretisiert:

1. **Forcierte OCR ohne Env-Toggle.** OCR-Flags (`DOCUMENT_SEARCH_OCR_ENABLED`, `…_FORCE_OCR`) werden zur Extraktionszeit aus **prozessweiten** Env-Variablen gelesen. Diese im `scan_ingest`-Job umzuschalten würde parallele normale Index-Jobs beeinflussen (Race). Stattdessen ruft der Scan-Pfad `ocr_service.ocr_pdf_pages(...)` / `ocr_image_bytes(...)` **direkt** auf — diese Funktionen prüfen keine Enable-Flags und erzwingen damit OCR isoliert. Implementiert in `services/scan_extractor.py`.

2. **„Rollen" = Gruppen-Principals.** Das ACL-Modell kennt Principals vom Typ `user` und `group`; Gruppen sind das rollenartige Konstrukt. Die Zuständigen eines Eingangs sind daher **Gruppen** (`reviewers.groups`, external_id z. B. `accounting`) **und** Benutzer (`reviewers.users`, username). Admins haben immer Zugriff (separater `require_admin`-Pfad).

3. **Dokument-ACL der Scans.** `upsert_document` vergibt automatisch `public`-read. Der `scan_ingest`-Job **entzieht** danach `public`-read und vergibt explizit `read` an die Reviewer-Principals (Memory `acl-backfill-republicizes`: nie auf „keine ACL-Zeile = privat" verlassen). Bei der **Bestätigung** (Filing) wird `public`-read wiederhergestellt, damit das abgelegte Dokument sich wie ein normal indexiertes verhält.

---

## File Structure

**Neu:**
- `document_search/extractors/plugins/image_extractor.py` — Bild-Extraktor (`.jpg/.jpeg/.png/.tif/.tiff` → OCR), Drop-in-Plugin (Auto-Discovery, kein `__init__.py`-Eingriff). Exportiert `IMAGE_SUFFIXES` + `ocr_image_file()` zur Wiederverwendung.
- `document_search/services/scan_extractor.py` — `extract_for_scan(path, languages) -> ExtractionResult`, erzwingt OCR (Bilder + Bild-PDFs) isoliert.
- `document_search/services/scan_inbox_config.py` — `ScanInbox`-Dataclass, Parsing/Validierung/Slugify aus Config-Dicts (DB-frei, rein unit-testbar).
- `document_search/services/scan_review_store.py` — `ScanReviewStore`: einziger Schreib-/Lesepfad auf `scan_review` (nutzt `SqliteStore.conn`).
- `document_search/services/scan_watcher.py` — `ScanWatcherManager`: Stabilitätserkennung, atomarer Claim-Move ins Staging, Job-Enqueue, Polling + optionale watchdog-Events, Live-Reconfigure.
- `document_search/web/templates/scan.html` — „Scan-Posteingang"-Seite.
- Tests: `tests/test_image_extractor.py`, `tests/test_scan_extractor.py`, `tests/test_scan_inbox_config.py`, `tests/test_scan_review_store.py`, `tests/test_scan_watcher.py`, `tests/test_scan_ingest_job.py`, `tests/test_scan_api.py`, `tests/test_scan_acl_regression.py`.

**Geändert:**
- `document_search/config.py` — `AppConfig.scan_inboxes` Feld + Parsing in `load_config`.
- `document_search/index/sqlite_store.py` — `scan_review`-Tabelle in `_init_schema`, `_migrate_scan_review`, `set_scan_acl()`, `restore_public_read()`, `principal_id_for(...)`, `user_group_external_ids()`.
- `document_search/observability.py` — `SCAN_INGESTED_TOTAL`, `SCAN_REVIEW_PENDING`.
- `document_search/app.py` — `UiConfigRequest.scan_inboxes`, `scan_ingest`-Handler, Watcher-Manager-Wiring (startup/shutdown/reconfigure), API-Routen, `/scan`-Page-Route.
- `document_search/web/templates/*.html` — Nav-Link „Scan", Config-Sektion „Scan-Eingänge".
- `document_search/web/static/app.js` — Scan-Review-Modul + Config-CRUD-Modul.
- `requirements.txt` — `watchdog` (optional, import-guarded).

---

## Phase A — Extraktion (Bild + forcierte Scan-OCR)

### Task A1: Bild-Extraktor-Plugin

**Files:**
- Create: `document_search/extractors/plugins/image_extractor.py`
- Test: `tests/test_image_extractor.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_image_extractor.py
from pathlib import Path

from document_search.extractors.plugins.image_extractor import (
    IMAGE_SUFFIXES,
    ImageTextExtractor,
    ocr_image_file,
)


def test_image_suffixes_cover_common_scan_formats():
    assert {".jpg", ".jpeg", ".png", ".tif", ".tiff"} <= set(IMAGE_SUFFIXES)


def test_extract_uses_ocr_and_builds_one_page_block(tmp_path, monkeypatch):
    img = tmp_path / "scan.png"
    img.write_bytes(b"\x89PNG fake bytes")

    monkeypatch.setattr(
        "document_search.extractors.plugins.image_extractor.ocr_image_bytes",
        lambda blob, languages="eng+deu": "Rechnung Nr 42",
    )
    result = ImageTextExtractor().extract(img)

    assert result.status == "ok"
    assert len(result.blocks) == 1
    assert result.blocks[0].block_type == "ocr_page"
    assert result.blocks[0].text == "Rechnung Nr 42"


def test_extract_empty_ocr_yields_ok_with_no_blocks(tmp_path, monkeypatch):
    img = tmp_path / "blank.tiff"
    img.write_bytes(b"II*\x00 fake tiff")
    monkeypatch.setattr(
        "document_search.extractors.plugins.image_extractor.ocr_image_bytes",
        lambda blob, languages="eng+deu": "   ",
    )
    result = ImageTextExtractor().extract(img)
    assert result.status == "ok"
    assert result.blocks == []


def test_extract_read_error_returns_error_status(tmp_path):
    missing = tmp_path / "does_not_exist.jpg"
    result = ImageTextExtractor().extract(missing)
    assert result.status == "error"
    assert result.error_message
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_image_extractor.py`
Expected: FAIL with `ModuleNotFoundError: ... image_extractor`.

- [ ] **Step 3: Write minimal implementation**

```python
# document_search/extractors/plugins/image_extractor.py
"""Image OCR extractor (.jpg/.png/.tiff). Drop-in plugin: auto-discovered by the
extractor loader. Images carry no native text layer, so extraction is always OCR.
"""
from __future__ import annotations

from pathlib import Path

from document_search.extractors.base import TextExtractor
from document_search.models import ContentBlock, ExtractionResult
from document_search.services.ocr_service import ocr_image_bytes, resolve_ocr_language

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".tif", ".tiff")


def ocr_image_file(path: Path, languages: str | None = None) -> str:
    """OCR a single image file, returning the recognised text (or "")."""
    lang = languages or resolve_ocr_language()
    return ocr_image_bytes(path.read_bytes(), languages=lang).strip()


class ImageTextExtractor(TextExtractor):
    suffixes = IMAGE_SUFFIXES

    def extract(self, file_path: Path) -> ExtractionResult:
        try:
            text = ocr_image_file(file_path)
        except OSError as exc:
            return ExtractionResult(
                file_path=file_path, status="error", error_message=str(exc)
            )
        blocks = []
        if text:
            blocks.append(
                ContentBlock("ocr_page", 1, text, self.__class__.__name__, {})
            )
        return ExtractionResult(file_path=file_path, status="ok", blocks=blocks)


def register(register_extractor) -> None:
    """Plugin hook: register the image extractor for every image suffix."""
    extractor = ImageTextExtractor()
    for suffix in IMAGE_SUFFIXES:
        register_extractor(suffix, extractor)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_image_extractor.py`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add document_search/extractors/plugins/image_extractor.py tests/test_image_extractor.py
git commit -m "feat(extractors): image OCR extractor plugin (jpg/png/tiff)"
```

---

### Task A2: Forcierter Scan-Extraktor

**Files:**
- Create: `document_search/services/scan_extractor.py`
- Test: `tests/test_scan_extractor.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scan_extractor.py
from pathlib import Path

import document_search.services.scan_extractor as se
from document_search.services.scan_extractor import extract_for_scan


def test_image_path_dispatches_to_image_ocr(tmp_path, monkeypatch):
    img = tmp_path / "scan.jpg"
    img.write_bytes(b"fake")
    monkeypatch.setattr(se, "ocr_image_file", lambda p, languages=None: "Hallo Welt")
    result = extract_for_scan(img, languages="deu+eng")
    assert result.status == "ok"
    assert result.blocks[0].text == "Hallo Welt"
    assert result.blocks[0].block_type == "ocr_page"


def test_pdf_path_forces_page_ocr_regardless_of_native_text(tmp_path, monkeypatch):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    # Pretend the PDF has 2 pages with no native text; OCR returns text per page.
    monkeypatch.setattr(se, "_pdf_page_count", lambda p: 2)
    monkeypatch.setattr(se, "_pdf_native_text", lambda p: {1: "", 2: ""})
    monkeypatch.setattr(
        se, "ocr_pdf_pages", lambda p, pages, languages="eng+deu": {1: "Seite eins", 2: "Seite zwei"}
    )
    result = extract_for_scan(pdf, languages="deu+eng")
    assert result.status == "ok"
    assert [b.text for b in result.blocks] == ["Seite eins", "Seite zwei"]
    assert all(b.block_type == "ocr_page" for b in result.blocks)


def test_pdf_prefers_native_text_when_present(tmp_path, monkeypatch):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(se, "_pdf_page_count", lambda p: 1)
    monkeypatch.setattr(se, "_pdf_native_text", lambda p: {1: "echter Text"})
    # OCR must NOT be needed when native text exists.
    monkeypatch.setattr(
        se, "ocr_pdf_pages", lambda p, pages, languages="eng+deu": {}
    )
    result = extract_for_scan(pdf, languages="deu+eng")
    assert result.blocks[0].text == "echter Text"
    assert result.blocks[0].block_type == "page"


def test_unsupported_extension_returns_error(tmp_path):
    f = tmp_path / "scan.xyz"
    f.write_bytes(b"x")
    result = extract_for_scan(f, languages="deu+eng")
    assert result.status == "error"
    assert "Unsupported" in (result.error_message or "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_scan_extractor.py`
Expected: FAIL (`ModuleNotFoundError: ... scan_extractor`).

- [ ] **Step 3: Write minimal implementation**

```python
# document_search/services/scan_extractor.py
"""Scan-path extraction with FORCED OCR, isolated from the global OCR env flags.

The normal extractors gate OCR on process-wide env vars; toggling those inside a
job would race concurrent index jobs. The scan path instead calls the ocr_service
primitives directly (they do not check enable flags), so OCR is always applied for
scanned input without mutating shared state.
"""
from __future__ import annotations

from pathlib import Path

from document_search.extractors import extractor_for
from document_search.extractors.plugins.image_extractor import (
    IMAGE_SUFFIXES,
    ocr_image_file,
)
from document_search.models import ContentBlock, ExtractionResult
from document_search.services.ocr_service import ocr_pdf_pages


def _pdf_page_count(path: Path) -> int:
    from pypdf import PdfReader

    return len(PdfReader(str(path)).pages)


def _pdf_native_text(path: Path) -> dict[int, str]:
    """1-based page number -> native (non-OCR) text, best effort."""
    from pypdf import PdfReader

    out: dict[int, str] = {}
    reader = PdfReader(str(path))
    for i, page in enumerate(reader.pages, start=1):
        try:
            out[i] = (page.extract_text() or "").strip()
        except Exception:
            out[i] = ""
    return out


def _extract_image(path: Path, languages: str) -> ExtractionResult:
    text = ocr_image_file(path, languages=languages)
    blocks = (
        [ContentBlock("ocr_page", 1, text, "ImageTextExtractor", {})] if text else []
    )
    return ExtractionResult(file_path=path, status="ok", blocks=blocks)


def _extract_pdf_forced(path: Path, languages: str) -> ExtractionResult:
    native = _pdf_native_text(path)
    page_count = _pdf_page_count(path)
    pages_needing_ocr = [n for n in range(1, page_count + 1) if not native.get(n)]
    ocr_text = (
        ocr_pdf_pages(path, pages_needing_ocr, languages=languages)
        if pages_needing_ocr
        else {}
    )
    blocks: list[ContentBlock] = []
    for n in range(1, page_count + 1):
        if native.get(n):
            blocks.append(ContentBlock("page", n, native[n], "ScanPdfExtractor", {}))
        elif ocr_text.get(n, "").strip():
            blocks.append(
                ContentBlock("ocr_page", n, ocr_text[n].strip(), "ScanPdfExtractor", {})
            )
    return ExtractionResult(file_path=path, status="ok", blocks=blocks)


def extract_for_scan(path: Path, languages: str) -> ExtractionResult:
    """Extract text from a scanned file, forcing OCR for images and image-PDFs."""
    ext = path.suffix.lower()
    try:
        if ext in IMAGE_SUFFIXES:
            return _extract_image(path, languages)
        if ext == ".pdf":
            return _extract_pdf_forced(path, languages)
        extractor = extractor_for(ext)
        if extractor is None:
            return ExtractionResult(
                file_path=path,
                status="error",
                error_message=f"Unsupported scan format: {ext}",
            )
        return extractor.extract(path)
    except Exception as exc:  # noqa: BLE001 - surface as error row, never crash the job
        return ExtractionResult(
            file_path=path, status="error", error_message=f"{type(exc).__name__}: {exc}"
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_scan_extractor.py`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add document_search/services/scan_extractor.py tests/test_scan_extractor.py
git commit -m "feat(scan): forced-OCR scan extractor isolated from global OCR env"
```

---

## Phase B — Konfigurationsmodell

### Task B1: `ScanInbox`-Modell + Validierung

**Files:**
- Create: `document_search/services/scan_inbox_config.py`
- Test: `tests/test_scan_inbox_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scan_inbox_config.py
import pytest

from document_search.services.scan_inbox_config import (
    ScanInbox,
    ScanInboxConfigError,
    parse_scan_inboxes,
    slugify_id,
    validate_inbox_paths,
)


def test_slugify_id_is_stable_and_safe():
    assert slugify_id("Scan-Buchhaltung 2024!") == "scan-buchhaltung-2024"
    assert slugify_id("   ") == ""


def test_parse_fills_defaults_and_derives_id():
    raw = [{"label": "Scan HR", "inbox_path": "/in/hr", "target_root": "/docs/HR"}]
    inboxes = parse_scan_inboxes(raw)
    assert len(inboxes) == 1
    ib = inboxes[0]
    assert ib.id == "scan-hr"
    assert ib.stability_seconds == 300
    assert ib.poll_interval_seconds == 60
    assert ib.enabled is True
    assert ib.reviewers_groups == [] and ib.reviewers_users == []


def test_parse_preserves_explicit_id_and_reviewers():
    raw = [{
        "id": "fixed", "label": "X", "inbox_path": "/in", "target_root": "/out",
        "reviewers": {"groups": ["accounting"], "users": ["m.muster"]},
        "stability_seconds": 30, "poll_interval_seconds": 10, "enabled": False,
    }]
    ib = parse_scan_inboxes(raw)[0]
    assert ib.id == "fixed"
    assert ib.reviewers_groups == ["accounting"]
    assert ib.reviewers_users == ["m.muster"]
    assert ib.enabled is False


def test_duplicate_ids_rejected():
    raw = [
        {"label": "A", "inbox_path": "/a", "target_root": "/x"},
        {"label": "A", "inbox_path": "/b", "target_root": "/y"},
    ]
    with pytest.raises(ScanInboxConfigError, match="duplicate"):
        parse_scan_inboxes(raw)


def test_stability_below_minimum_rejected():
    raw = [{"label": "A", "inbox_path": "/a", "target_root": "/x", "stability_seconds": 5}]
    with pytest.raises(ScanInboxConfigError, match="stability_seconds"):
        parse_scan_inboxes(raw)


def test_validate_inbox_paths_rejects_inbox_inside_target(tmp_path):
    target = tmp_path / "docs"
    inbox = target / "incoming"
    inbox.mkdir(parents=True)
    ib = ScanInbox(id="x", label="X", inbox_path=str(inbox), target_root=str(target))
    with pytest.raises(ScanInboxConfigError, match="inside"):
        validate_inbox_paths(ib)


def test_validate_inbox_paths_rejects_missing(tmp_path):
    ib = ScanInbox(id="x", label="X", inbox_path=str(tmp_path / "nope"),
                   target_root=str(tmp_path))
    with pytest.raises(ScanInboxConfigError, match="does not exist"):
        validate_inbox_paths(ib)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_scan_inbox_config.py`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Write minimal implementation**

```python
# document_search/services/scan_inbox_config.py
"""Parsing + validation for the `scan_inboxes` config list. DB-free and pure so
it is fully unit-testable. Persistence is handled by the existing config save."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_MIN_STABILITY_SECONDS = 30


class ScanInboxConfigError(ValueError):
    """Raised when a scan inbox configuration is invalid."""


@dataclass(slots=True)
class ScanInbox:
    id: str
    label: str
    inbox_path: str
    target_root: str
    reviewers_groups: list[str] = field(default_factory=list)
    reviewers_users: list[str] = field(default_factory=list)
    stability_seconds: int = 300
    poll_interval_seconds: int = 60
    enabled: bool = True

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "inbox_path": self.inbox_path,
            "target_root": self.target_root,
            "reviewers": {"groups": self.reviewers_groups, "users": self.reviewers_users},
            "stability_seconds": self.stability_seconds,
            "poll_interval_seconds": self.poll_interval_seconds,
            "enabled": self.enabled,
        }


def slugify_id(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (label or "").strip().lower())
    return s.strip("-")


def _as_str_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def parse_scan_inboxes(raw: object) -> list[ScanInbox]:
    """Parse the raw `scan_inboxes` config value into validated ScanInbox objects.

    Validates structural rules (id/label present, unique ids, sane numbers).
    Filesystem existence is checked separately by `validate_inbox_paths` (so unit
    tests and config load do not require the paths to exist)."""
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ScanInboxConfigError("scan_inboxes must be a list")

    inboxes: list[ScanInbox] = []
    seen_ids: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise ScanInboxConfigError("each scan inbox must be an object")
        label = str(entry.get("label", "")).strip()
        if not label:
            raise ScanInboxConfigError("scan inbox label must not be empty")
        inbox_path = str(entry.get("inbox_path", "")).strip()
        target_root = str(entry.get("target_root", "")).strip()
        if not inbox_path or not target_root:
            raise ScanInboxConfigError(f"inbox '{label}' needs inbox_path and target_root")

        ib_id = str(entry.get("id", "")).strip() or slugify_id(label)
        if not ib_id:
            raise ScanInboxConfigError(f"inbox '{label}' produced an empty id")
        if ib_id in seen_ids:
            raise ScanInboxConfigError(f"duplicate scan inbox id: {ib_id}")
        seen_ids.add(ib_id)

        reviewers = entry.get("reviewers") or {}
        stability = int(entry.get("stability_seconds", 300))
        if stability < _MIN_STABILITY_SECONDS:
            raise ScanInboxConfigError(
                f"inbox '{ib_id}': stability_seconds must be >= {_MIN_STABILITY_SECONDS}"
            )
        poll = int(entry.get("poll_interval_seconds", 60))
        if poll < 5:
            raise ScanInboxConfigError(f"inbox '{ib_id}': poll_interval_seconds must be >= 5")

        inboxes.append(
            ScanInbox(
                id=ib_id,
                label=label,
                inbox_path=inbox_path,
                target_root=target_root,
                reviewers_groups=_as_str_list(reviewers.get("groups")),
                reviewers_users=_as_str_list(reviewers.get("users")),
                stability_seconds=stability,
                poll_interval_seconds=poll,
                enabled=bool(entry.get("enabled", True)),
            )
        )
    return inboxes


def validate_inbox_paths(inbox: ScanInbox) -> None:
    """Filesystem-level validation used by the UI "Test" action and config save."""
    inbox_p = Path(inbox.inbox_path).resolve()
    target_p = Path(inbox.target_root).resolve()
    if not inbox_p.exists() or not inbox_p.is_dir():
        raise ScanInboxConfigError(f"inbox_path does not exist or is not a directory: {inbox.inbox_path}")
    if not target_p.exists() or not target_p.is_dir():
        raise ScanInboxConfigError(f"target_root does not exist or is not a directory: {inbox.target_root}")
    if inbox_p == target_p:
        raise ScanInboxConfigError("inbox_path and target_root must differ")
    if inbox_p.is_relative_to(target_p):
        raise ScanInboxConfigError("inbox_path must not be inside target_root (filed docs would be re-ingested)")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_scan_inbox_config.py`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add document_search/services/scan_inbox_config.py tests/test_scan_inbox_config.py
git commit -m "feat(scan): scan_inboxes config model + validation"
```

---

### Task B2: `scan_inboxes` in `AppConfig` + `load_config`

**Files:**
- Modify: `document_search/config.py` (AppConfig dataclass ~line 26-42; `load_config` ~line 45-63)
- Test: `tests/test_scan_inbox_config.py` (append)

- [ ] **Step 1: Write the failing test (append to the existing file)**

```python
# tests/test_scan_inbox_config.py  (append)
import json

from document_search.config import load_config


def test_load_config_reads_scan_inboxes(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({
        "scan_inboxes": [
            {"id": "b", "label": "B", "inbox_path": "/in", "target_root": "/out"}
        ]
    }), encoding="utf-8")
    cfg = load_config(cfg_file)
    assert isinstance(cfg.scan_inboxes, list)
    assert cfg.scan_inboxes[0]["id"] == "b"


def test_load_config_defaults_scan_inboxes_empty(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text("{}", encoding="utf-8")
    assert load_config(cfg_file).scan_inboxes == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_scan_inbox_config.py -k scan_inboxes`
Expected: FAIL (`AttributeError: 'AppConfig' object has no attribute 'scan_inboxes'`).

- [ ] **Step 3: Write minimal implementation**

In `document_search/config.py`, add the field to `AppConfig` (after `ocr: OcrConfig = ...`):

```python
    scan_inboxes: list[dict] = field(default_factory=list)
```

`load_config` already copies unknown top-level keys via `setattr(cfg, key, value)` when `hasattr(cfg, key)`, so the raw list is stored as-is. No change needed there. Verify the field is a plain `list[dict]` (parsing/validation is done by `parse_scan_inboxes`, not at load time, to keep config load resilient).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_scan_inbox_config.py -k scan_inboxes`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add document_search/config.py tests/test_scan_inbox_config.py
git commit -m "feat(config): add scan_inboxes list to AppConfig"
```

---

## Phase C — `scan_review`-Tabelle, Store & ACL-Helfer

### Task C1: `scan_review`-Schema + Migration

**Files:**
- Modify: `document_search/index/sqlite_store.py` (`_init_schema` executescript; add `_migrate_scan_review`; call it where other migrations run)
- Test: `tests/test_scan_review_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scan_review_store.py
from pathlib import Path

from document_search.index.sqlite_store import SqliteStore


def test_scan_review_table_and_index_exist(tmp_path):
    db = SqliteStore(Path(tmp_path / "t.db"))
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(scan_review)").fetchall()}
    assert {"id", "inbox_id", "document_id", "staging_path", "status",
            "suggested_folder", "suggested_tags", "ai_reasoning",
            "ai_decision_id", "error_message", "created_at", "updated_at",
            "reviewed_by", "reviewed_at", "original_filename"} <= cols
    idx = {r[1] for r in db.conn.execute("PRAGMA index_list(scan_review)").fetchall()}
    assert "idx_scan_review_inbox_status" in idx


def test_migration_creates_table_on_legacy_db(tmp_path):
    # Simulate a legacy DB without scan_review, then re-open to trigger migration.
    db_path = Path(tmp_path / "legacy.db")
    db = SqliteStore(db_path)
    db.conn.execute("DROP TABLE scan_review")
    db.conn.commit()
    db.conn.close()
    db2 = SqliteStore(db_path)  # re-open runs migrations
    assert db2.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='scan_review'"
    ).fetchone() is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_scan_review_store.py -k table or migration`
Expected: FAIL (`no such table: scan_review`).

- [ ] **Step 3: Write minimal implementation**

In `sqlite_store.py` `_init_schema` executescript, add (alongside the other `CREATE TABLE IF NOT EXISTS` statements — table first, then index):

```sql
CREATE TABLE IF NOT EXISTS scan_review (
  id INTEGER PRIMARY KEY,
  inbox_id TEXT NOT NULL,
  document_id INTEGER,
  staging_path TEXT NOT NULL,
  original_filename TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  suggested_folder TEXT,
  suggested_tags TEXT,
  ai_reasoning TEXT,
  ai_decision_id INTEGER,
  error_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  reviewed_by TEXT,
  reviewed_at TEXT,
  FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_scan_review_inbox_status ON scan_review(inbox_id, status);
```

Add a migration method (mirrors the existing `_migrate_*` pattern) and call it where the other migrations are invoked in `__init__` (after `_init_schema`, alongside `_migrate_jobs_cancellation`):

```python
    def _migrate_scan_review(self) -> None:
        """Create scan_review on legacy DBs. Table is additive (no ALTER), so the
        index is created right after the table — never before an ALTER (see the
        schema-migration-ordering convention)."""
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scan_review (
              id INTEGER PRIMARY KEY,
              inbox_id TEXT NOT NULL,
              document_id INTEGER,
              staging_path TEXT NOT NULL,
              original_filename TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',
              suggested_folder TEXT,
              suggested_tags TEXT,
              ai_reasoning TEXT,
              ai_decision_id INTEGER,
              error_message TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              reviewed_by TEXT,
              reviewed_at TEXT,
              FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE SET NULL
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scan_review_inbox_status "
            "ON scan_review(inbox_id, status)"
        )
        self.conn.commit()
```

Find the `__init__` block that calls `self._migrate_jobs_cancellation()` and add `self._migrate_scan_review()` immediately after it.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_scan_review_store.py -k "table or migration"`
Expected: PASS.

- [ ] **Step 5: Run against the root DB (legacy-DB safety, per schema-migration-ordering memory)**

Run: `python -c "from pathlib import Path; from document_search.index.sqlite_store import SqliteStore; SqliteStore(Path('document_index.db')); print('ok')"`
Expected: prints `ok` (no exception opening the real DB).

- [ ] **Step 6: Commit**

```bash
git add document_search/index/sqlite_store.py tests/test_scan_review_store.py
git commit -m "feat(store): scan_review table + legacy-DB migration"
```

---

### Task C2: ACL- & Principal-Helfer im Store

**Files:**
- Modify: `document_search/index/sqlite_store.py` (add `principal_id_for`, `set_scan_acl`, `restore_public_read`, `user_group_external_ids`)
- Test: `tests/test_scan_review_store.py` (append)

- [ ] **Step 1: Write the failing test (append)**

```python
# tests/test_scan_review_store.py  (append)
from document_search.services.acl_service import visible_document_ids_subquery
from document_search.models import ExtractionResult, FileFingerprint, ContentBlock
from datetime import datetime, UTC


def _make_doc(db, tmp_path, name="d.pdf"):
    p = tmp_path / name
    p.write_text("x")
    fp = FileFingerprint(path=p, file_size=1, modified_at=datetime.now(tz=UTC), sha256="h" + name)
    ext = ExtractionResult(file_path=p, status="ok",
                           blocks=[ContentBlock("page", 1, "hello", "T", {})])
    return db.upsert_document(fp, ext)


def _visible_to(db, user_id, doc_id):
    sql, params = visible_document_ids_subquery(user_id)
    rows = db.conn.execute(f"SELECT 1 FROM ({sql}) WHERE document_id = ?",
                           list(params) + [doc_id]).fetchall()
    return bool(rows)


def test_set_scan_acl_revokes_public_and_grants_reviewers(tmp_path):
    db = SqliteStore(Path(tmp_path / "t.db"))
    reviewer = db.create_user("reviewer", "pw12345678", role="user")
    other = db.create_user("other", "pw12345678", role="user")
    doc_id = _make_doc(db, tmp_path)
    # Initially public-read -> everyone sees it.
    assert _visible_to(db, other, doc_id)

    db.set_scan_acl(doc_id, group_external_ids=[], user_external_ids=["reviewer"])

    assert _visible_to(db, reviewer, doc_id)
    assert not _visible_to(db, other, doc_id)


def test_restore_public_read_makes_doc_visible_again(tmp_path):
    db = SqliteStore(Path(tmp_path / "t.db"))
    other = db.create_user("other", "pw12345678", role="user")
    doc_id = _make_doc(db, tmp_path)
    db.set_scan_acl(doc_id, group_external_ids=[], user_external_ids=[])
    assert not _visible_to(db, other, doc_id)
    db.restore_public_read(doc_id)
    assert _visible_to(db, other, doc_id)
```

> Note: `create_user` signature — confirm against the store (the auth flow creates the default admin). If the method name/signature differs, adapt the test helper to the real user-creation API used elsewhere in `tests/` (search existing tests for how a user is created).

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_scan_review_store.py -k "scan_acl or public_read"`
Expected: FAIL (`AttributeError: ... set_scan_acl`).

- [ ] **Step 3: Write minimal implementation**

Add to `SqliteStore`:

```python
    def principal_id_for(self, *, group_external_id: str | None = None,
                         user_external_id: str | None = None) -> int | None:
        """Resolve a principal id by group external_id or by username. Creates a
        'user'-type principal on demand for a known username (mirrors backfill)."""
        if group_external_id:
            row = self.conn.execute(
                "SELECT id FROM principals WHERE type='group' AND external_id=?",
                (group_external_id,),
            ).fetchone()
            return row["id"] if row else None
        if user_external_id:
            row = self.conn.execute(
                "SELECT id FROM principals WHERE type='user' AND external_id=?",
                (user_external_id,),
            ).fetchone()
            if row:
                return row["id"]
            # Known user without a principal yet? create one (idempotent).
            u = self.conn.execute(
                "SELECT id FROM users WHERE username=?", (user_external_id,)
            ).fetchone()
            if u is None:
                return None
            now = datetime.now(tz=UTC).isoformat()
            self.conn.execute(
                "INSERT OR IGNORE INTO principals(type, external_id, display_name, created_at) "
                "VALUES('user', ?, ?, ?)",
                (user_external_id, user_external_id, now),
            )
            self.conn.execute(
                "UPDATE users SET principal_id=(SELECT id FROM principals "
                "WHERE type='user' AND external_id=?) WHERE id=? AND principal_id IS NULL",
                (user_external_id, u["id"]),
            )
            self.conn.commit()
            p = self.conn.execute(
                "SELECT id FROM principals WHERE type='user' AND external_id=?",
                (user_external_id,),
            ).fetchone()
            return p["id"] if p else None
        return None

    def _public_principal_id(self) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM principals WHERE type='group' AND external_id='public'"
        ).fetchone()
        return row["id"] if row else None

    def set_scan_acl(self, document_id: int, *, group_external_ids: list[str],
                     user_external_ids: list[str]) -> None:
        """Make a scanned doc private to reviewers: drop public-read, grant read to
        the configured reviewer groups + users. Explicit grants only — never rely on
        absence of ACL rows (the backfill re-publicises empty docs)."""
        public_id = self._public_principal_id()
        if public_id is not None:
            self.conn.execute(
                "DELETE FROM document_acl WHERE document_id=? AND principal_id=? AND permission='read'",
                (document_id, public_id),
            )
        now = datetime.now(tz=UTC).isoformat()
        for gid in group_external_ids:
            pid = self.principal_id_for(group_external_id=gid)
            if pid is not None:
                self.conn.execute(
                    "INSERT OR IGNORE INTO document_acl(document_id, principal_id, permission, granted_at) "
                    "VALUES(?,?, 'read', ?)", (document_id, pid, now))
        for uid in user_external_ids:
            pid = self.principal_id_for(user_external_id=uid)
            if pid is not None:
                self.conn.execute(
                    "INSERT OR IGNORE INTO document_acl(document_id, principal_id, permission, granted_at) "
                    "VALUES(?,?, 'read', ?)", (document_id, pid, now))
        self.conn.commit()

    def restore_public_read(self, document_id: int) -> None:
        """Re-grant public read (used when a scan is filed so it behaves like a
        normally indexed document)."""
        public_id = self._public_principal_id()
        if public_id is None:
            return
        now = datetime.now(tz=UTC).isoformat()
        self.conn.execute(
            "INSERT OR IGNORE INTO document_acl(document_id, principal_id, permission, granted_at) "
            "VALUES(?,?, 'read', ?)", (document_id, public_id, now))
        self.conn.commit()

    def user_group_external_ids(self, user_id: int) -> set[str]:
        """The external_ids of every group the user belongs to (for inbox auth)."""
        rows = self.conn.execute(
            "SELECT p.external_id FROM user_groups ug "
            "JOIN principals p ON p.id = ug.principal_id "
            "WHERE ug.user_id = ? AND p.type='group'",
            (user_id,),
        ).fetchall()
        return {r["external_id"] for r in rows}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_scan_review_store.py -k "scan_acl or public_read"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add document_search/index/sqlite_store.py tests/test_scan_review_store.py
git commit -m "feat(store): explicit scan ACL helpers (reviewer-only read, public restore)"
```

---

### Task C3: `ScanReviewStore`

**Files:**
- Create: `document_search/services/scan_review_store.py`
- Test: `tests/test_scan_review_store.py` (append)

- [ ] **Step 1: Write the failing test (append)**

```python
# tests/test_scan_review_store.py  (append)
from document_search.services.scan_review_store import ScanReviewStore


def test_create_list_and_status_transitions(tmp_path):
    db = SqliteStore(Path(tmp_path / "t.db"))
    doc_id = _make_doc(db, tmp_path)
    srs = ScanReviewStore(db)

    rid = srs.create_pending(
        inbox_id="b", document_id=doc_id, staging_path="/stage/b/x.pdf",
        original_filename="x.pdf", suggested_folder="Rechnungen",
        suggested_tags=["rechnung"], ai_reasoning="weil", ai_decision_id=None,
    )
    row = srs.get(rid)
    assert row["status"] == "pending"
    assert row["suggested_tags"] == ["rechnung"]

    pending = srs.list_reviews(inbox_ids=["b"], status="pending")
    assert len(pending) == 1

    srs.mark_filed(rid, reviewed_by="admin")
    assert srs.get(rid)["status"] == "filed"
    assert srs.count_pending(["b"]) == 0


def test_create_error_and_retry(tmp_path):
    db = SqliteStore(Path(tmp_path / "t.db"))
    srs = ScanReviewStore(db)
    rid = srs.create_error(inbox_id="b", staging_path="/stage/b/bad.pdf",
                           original_filename="bad.pdf", error_message="OCR failed")
    assert srs.get(rid)["status"] == "error"
    srs.set_pending(rid)
    assert srs.get(rid)["status"] == "pending"


def test_staging_paths_with_rows(tmp_path):
    db = SqliteStore(Path(tmp_path / "t.db"))
    srs = ScanReviewStore(db)
    srs.create_error(inbox_id="b", staging_path="/stage/b/a.pdf",
                     original_filename="a.pdf", error_message="e")
    assert "/stage/b/a.pdf" in srs.staging_paths_with_rows()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_scan_review_store.py -k "transitions or error_and_retry or staging_paths"`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Write minimal implementation**

```python
# document_search/services/scan_review_store.py
"""Sole read/write surface for the scan_review queue. Wraps a SqliteStore so it
shares the per-thread connection + WAL semantics of the rest of the index layer."""
from __future__ import annotations

import json
from datetime import UTC, datetime


class ScanReviewStore:
    def __init__(self, store) -> None:
        self._store = store
        self.conn = store.conn

    @staticmethod
    def _now() -> str:
        return datetime.now(tz=UTC).isoformat()

    def create_pending(self, *, inbox_id: str, document_id: int | None,
                       staging_path: str, original_filename: str,
                       suggested_folder: str | None, suggested_tags: list[str] | None,
                       ai_reasoning: str | None, ai_decision_id: int | None) -> int:
        now = self._now()
        cur = self.conn.execute(
            "INSERT INTO scan_review(inbox_id, document_id, staging_path, original_filename, "
            "status, suggested_folder, suggested_tags, ai_reasoning, ai_decision_id, "
            "created_at, updated_at) VALUES(?,?,?,?, 'pending', ?,?,?,?, ?,?)",
            (inbox_id, document_id, staging_path, original_filename, suggested_folder,
             json.dumps(suggested_tags or []), ai_reasoning, ai_decision_id, now, now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def create_error(self, *, inbox_id: str, staging_path: str,
                     original_filename: str, error_message: str) -> int:
        now = self._now()
        cur = self.conn.execute(
            "INSERT INTO scan_review(inbox_id, document_id, staging_path, original_filename, "
            "status, error_message, created_at, updated_at) "
            "VALUES(?,?,?,?, 'error', ?, ?, ?)",
            (inbox_id, None, staging_path, original_filename, error_message, now, now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def _row_to_dict(self, row) -> dict:
        d = dict(row)
        d["suggested_tags"] = json.loads(d.get("suggested_tags") or "[]")
        return d

    def get(self, review_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM scan_review WHERE id=?", (review_id,)
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_reviews(self, *, inbox_ids: list[str] | None, status: str | None) -> list[dict]:
        clauses, params = [], []
        if inbox_ids is not None:
            if not inbox_ids:
                return []
            clauses.append(f"inbox_id IN ({','.join('?' * len(inbox_ids))})")
            params.extend(inbox_ids)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM scan_review{where} ORDER BY created_at DESC", params
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def _set_status(self, review_id: int, status: str, reviewed_by: str | None) -> None:
        now = self._now()
        self.conn.execute(
            "UPDATE scan_review SET status=?, reviewed_by=COALESCE(?, reviewed_by), "
            "reviewed_at=CASE WHEN ? IS NULL THEN reviewed_at ELSE ? END, updated_at=? "
            "WHERE id=?",
            (status, reviewed_by, reviewed_by, now, now, review_id),
        )
        self.conn.commit()

    def mark_filed(self, review_id: int, reviewed_by: str) -> None:
        self._set_status(review_id, "filed", reviewed_by)

    def mark_rejected(self, review_id: int, reviewed_by: str) -> None:
        self._set_status(review_id, "rejected", reviewed_by)

    def set_pending(self, review_id: int) -> None:
        now = self._now()
        self.conn.execute(
            "UPDATE scan_review SET status='pending', error_message=NULL, updated_at=? WHERE id=?",
            (now, review_id),
        )
        self.conn.commit()

    def count_pending(self, inbox_ids: list[str]) -> int:
        if not inbox_ids:
            return 0
        row = self.conn.execute(
            f"SELECT COUNT(*) AS c FROM scan_review WHERE status='pending' "
            f"AND inbox_id IN ({','.join('?' * len(inbox_ids))})",
            inbox_ids,
        ).fetchone()
        return int(row["c"])

    def staging_paths_with_rows(self) -> set[str]:
        rows = self.conn.execute("SELECT staging_path FROM scan_review").fetchall()
        return {r["staging_path"] for r in rows}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_scan_review_store.py`
Expected: PASS (all tests in file).

- [ ] **Step 5: Commit**

```bash
git add document_search/services/scan_review_store.py tests/test_scan_review_store.py
git commit -m "feat(scan): ScanReviewStore queue persistence"
```

---

## Phase D — Watcher

### Task D1: Stabilitätserkennung + Claim-Move

**Files:**
- Create: `document_search/services/scan_watcher.py`
- Test: `tests/test_scan_watcher.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scan_watcher.py
from pathlib import Path

from document_search.services.scan_inbox_config import ScanInbox
from document_search.services.scan_watcher import (
    is_stable,
    scan_once,
    staging_dir_for,
)


def _inbox(tmp_path) -> ScanInbox:
    inbox = tmp_path / "in"
    target = tmp_path / "out"
    inbox.mkdir()
    target.mkdir()
    return ScanInbox(id="b", label="B", inbox_path=str(inbox), target_root=str(target),
                     stability_seconds=300)


def test_is_stable_uses_mtime_age(tmp_path):
    f = tmp_path / "a.pdf"
    f.write_text("x")
    # mtime is "now" -> not stable against a 300s window at now+10s.
    assert not is_stable(f, stability_seconds=300, now=f.stat().st_mtime + 10)
    # ...stable once 300s have elapsed since mtime.
    assert is_stable(f, stability_seconds=300, now=f.stat().st_mtime + 301)


def test_scan_once_moves_stable_file_and_enqueues(tmp_path):
    ib = _inbox(tmp_path)
    src = Path(ib.inbox_path) / "scan001.pdf"
    src.write_bytes(b"%PDF fake")

    enqueued = []

    def fake_enqueue(inbox_id, staging_path, original_filename):
        enqueued.append((inbox_id, staging_path, original_filename))

    moved = scan_once(ib, data_dir=tmp_path / "data", enqueue=fake_enqueue,
                      now=src.stat().st_mtime + 400)

    assert moved == 1
    assert not src.exists()  # claimed out of the inbox
    staged = list((staging_dir_for(tmp_path / "data", ib.id)).glob("*.pdf"))
    assert len(staged) == 1
    assert enqueued and enqueued[0][0] == "b"
    assert enqueued[0][2] == "scan001.pdf"


def test_scan_once_skips_unstable_file(tmp_path):
    ib = _inbox(tmp_path)
    src = Path(ib.inbox_path) / "growing.pdf"
    src.write_bytes(b"partial")
    moved = scan_once(ib, data_dir=tmp_path / "data", enqueue=lambda *a: None,
                      now=src.stat().st_mtime + 10)  # only 10s old
    assert moved == 0
    assert src.exists()


def test_scan_once_dedups_same_name_collision(tmp_path):
    ib = _inbox(tmp_path)
    staging = staging_dir_for(tmp_path / "data", ib.id)
    staging.mkdir(parents=True)
    (staging / "dup.pdf").write_bytes(b"old")
    src = Path(ib.inbox_path) / "dup.pdf"
    src.write_bytes(b"new")
    scan_once(ib, data_dir=tmp_path / "data", enqueue=lambda *a: None,
              now=src.stat().st_mtime + 400)
    # Both files must coexist in staging (claim must not overwrite).
    assert len(list(staging.glob("dup*.pdf"))) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_scan_watcher.py -k "is_stable or scan_once"`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Write minimal implementation**

```python
# document_search/services/scan_watcher.py
"""Watches scan inboxes for stable files, claims them into staging, and enqueues
scan_ingest jobs. Polling is the robust default (SMB-friendly); watchdog events
are an optional low-latency accelerator layered on top (Task D2)."""
from __future__ import annotations

import logging
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from document_search.services.scan_inbox_config import ScanInbox

log = logging.getLogger(__name__)

# Only files with these suffixes are picked up from an inbox.
SCAN_SUFFIXES = (".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff")


def staging_dir_for(data_dir: Path, inbox_id: str) -> Path:
    return Path(data_dir) / "scan-staging" / inbox_id / "pending-review"


def is_stable(path: Path, *, stability_seconds: int, now: float | None = None) -> bool:
    """A file is stable when it has not been modified for `stability_seconds`."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    current = time.time() if now is None else now
    return (current - mtime) >= stability_seconds


def _claim_destination(staging: Path, name: str) -> Path:
    """A non-colliding destination path inside staging."""
    dest = staging / name
    if not dest.exists():
        return dest
    stem, suffix = Path(name).stem, Path(name).suffix
    return staging / f"{stem}_{uuid.uuid4().hex[:8]}{suffix}"


def scan_once(inbox: ScanInbox, *, data_dir: Path,
              enqueue: Callable[[str, str, str], None],
              now: float | None = None) -> int:
    """Scan one inbox once: move every stable, supported file into staging and
    enqueue it. Returns the number of files claimed. The move IS the claim — atomic
    on the same filesystem, so only one pass wins a given file."""
    inbox_path = Path(inbox.inbox_path)
    if not inbox_path.is_dir():
        log.warning("Scan inbox %s path missing: %s", inbox.id, inbox.inbox_path)
        return 0
    staging = staging_dir_for(data_dir, inbox.id)
    staging.mkdir(parents=True, exist_ok=True)
    claimed = 0
    for entry in sorted(inbox_path.iterdir()):
        if not entry.is_file() or entry.suffix.lower() not in SCAN_SUFFIXES:
            continue
        if not is_stable(entry, stability_seconds=inbox.stability_seconds, now=now):
            continue
        dest = _claim_destination(staging, entry.name)
        try:
            shutil.move(str(entry), str(dest))
        except OSError:
            log.warning("Failed to claim scan file %s", entry, exc_info=True)
            continue
        try:
            enqueue(inbox.id, str(dest), entry.name)
        except Exception:
            log.exception("Failed to enqueue scan_ingest for %s", dest)
        claimed += 1
    return claimed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_scan_watcher.py -k "is_stable or scan_once"`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add document_search/services/scan_watcher.py tests/test_scan_watcher.py
git commit -m "feat(scan): stability detection + atomic claim-to-staging"
```

---

### Task D2: `ScanWatcherManager` (Polling + optional watchdog + Reconfigure)

**Files:**
- Modify: `document_search/services/scan_watcher.py` (add the manager class)
- Modify: `requirements.txt` (add `watchdog`)
- Test: `tests/test_scan_watcher.py` (append)

- [ ] **Step 1: Write the failing test (append)**

```python
# tests/test_scan_watcher.py  (append)
from document_search.services.scan_watcher import ScanWatcherManager


def test_manager_reconfigure_tracks_enabled_inboxes(tmp_path):
    enq = []
    mgr = ScanWatcherManager(
        data_dir=tmp_path / "data",
        enqueue=lambda i, s, o: enq.append((i, s, o)),
    )
    ib_on = ScanInbox(id="on", label="On", inbox_path=str(tmp_path),
                      target_root=str(tmp_path / "t1"), enabled=True)
    ib_off = ScanInbox(id="off", label="Off", inbox_path=str(tmp_path),
                       target_root=str(tmp_path / "t2"), enabled=False)
    mgr.reconfigure([ib_on, ib_off])
    assert mgr.active_inbox_ids() == {"on"}
    mgr.reconfigure([])
    assert mgr.active_inbox_ids() == set()


def test_manager_recover_reenqueues_orphan_staging_files(tmp_path):
    data = tmp_path / "data"
    staging = staging_dir_for(data, "b")
    staging.mkdir(parents=True)
    orphan = staging / "left.pdf"
    orphan.write_bytes(b"x")
    enq = []
    mgr = ScanWatcherManager(data_dir=data, enqueue=lambda i, s, o: enq.append((i, s, o)))
    ib = ScanInbox(id="b", label="B", inbox_path=str(tmp_path), target_root=str(tmp_path / "t"))
    mgr.recover_orphans([ib], known_staging_paths=set())
    assert enq == [("b", str(orphan), "left.pdf")]


def test_manager_recover_skips_files_with_existing_rows(tmp_path):
    data = tmp_path / "data"
    staging = staging_dir_for(data, "b")
    staging.mkdir(parents=True)
    tracked = staging / "tracked.pdf"
    tracked.write_bytes(b"x")
    enq = []
    mgr = ScanWatcherManager(data_dir=data, enqueue=lambda i, s, o: enq.append((i, s, o)))
    ib = ScanInbox(id="b", label="B", inbox_path=str(tmp_path), target_root=str(tmp_path / "t"))
    mgr.recover_orphans([ib], known_staging_paths={str(tracked)})
    assert enq == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_scan_watcher.py -k manager`
Expected: FAIL (`ImportError: cannot import name 'ScanWatcherManager'`).

- [ ] **Step 3: Write minimal implementation**

Append to `scan_watcher.py`:

```python
class ScanWatcherManager:
    """Owns one polling thread per enabled inbox; optionally augments each with a
    watchdog observer for low-latency event triggers. Reconfigure starts/stops
    threads live without an app restart."""

    def __init__(self, *, data_dir: Path,
                 enqueue: Callable[[str, str, str], None]) -> None:
        self._data_dir = Path(data_dir)
        self._enqueue = enqueue
        self._inboxes: dict[str, ScanInbox] = {}
        self._stops: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._observers: dict[str, object] = {}
        self._lock = threading.Lock()

    def active_inbox_ids(self) -> set[str]:
        with self._lock:
            return set(self._inboxes)

    def _poll_loop(self, inbox_id: str, stop: threading.Event) -> None:
        while not stop.is_set():
            with self._lock:
                inbox = self._inboxes.get(inbox_id)
            if inbox is None:
                return
            try:
                scan_once(inbox, data_dir=self._data_dir, enqueue=self._enqueue)
            except Exception:
                log.exception("scan_once failed for inbox %s", inbox_id)
            stop.wait(inbox.poll_interval_seconds)

    def _start_inbox(self, inbox: ScanInbox) -> None:
        stop = threading.Event()
        self._inboxes[inbox.id] = inbox
        self._stops[inbox.id] = stop
        t = threading.Thread(target=self._poll_loop, args=(inbox.id, stop),
                             name=f"scan-watch-{inbox.id}", daemon=True)
        self._threads[inbox.id] = t
        t.start()
        self._start_observer(inbox)

    def _start_observer(self, inbox: ScanInbox) -> None:
        """Optional watchdog accelerator. Absent dependency -> polling-only."""
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except Exception:
            return

        manager = self

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                with manager._lock:
                    ib = manager._inboxes.get(inbox.id)
                if ib is None:
                    return
                try:
                    scan_once(ib, data_dir=manager._data_dir, enqueue=manager._enqueue)
                except Exception:
                    log.exception("event-triggered scan_once failed for %s", inbox.id)

        try:
            obs = Observer()
            obs.schedule(_Handler(), inbox.inbox_path, recursive=False)
            obs.daemon = True
            obs.start()
            self._observers[inbox.id] = obs
        except Exception:
            log.warning("watchdog observer failed for %s; polling only", inbox.id, exc_info=True)

    def _stop_inbox(self, inbox_id: str) -> None:
        stop = self._stops.pop(inbox_id, None)
        if stop is not None:
            stop.set()
        t = self._threads.pop(inbox_id, None)
        if t is not None:
            t.join(timeout=2.0)
        obs = self._observers.pop(inbox_id, None)
        if obs is not None:
            try:
                obs.stop()
                obs.join(timeout=2.0)
            except Exception:
                log.warning("Failed to stop observer for %s", inbox_id, exc_info=True)
        self._inboxes.pop(inbox_id, None)

    def reconfigure(self, inboxes: list[ScanInbox]) -> None:
        """Diff the desired enabled set against the running set; start/stop deltas."""
        desired = {ib.id: ib for ib in inboxes if ib.enabled}
        with self._lock:
            running = set(self._inboxes)
            wanted = set(desired)
            for inbox_id in running - wanted:
                self._stop_inbox(inbox_id)
            for inbox_id in wanted - running:
                self._start_inbox(desired[inbox_id])
            for inbox_id in wanted & running:  # update config (interval/paths)
                self._inboxes[inbox_id] = desired[inbox_id]

    def recover_orphans(self, inboxes: list[ScanInbox], *,
                        known_staging_paths: set[str]) -> None:
        """Re-enqueue staged files that have no scan_review row (crash recovery)."""
        for inbox in inboxes:
            staging = staging_dir_for(self._data_dir, inbox.id)
            if not staging.is_dir():
                continue
            for f in sorted(staging.iterdir()):
                if f.is_file() and str(f) not in known_staging_paths:
                    try:
                        self._enqueue(inbox.id, str(f), f.name)
                    except Exception:
                        log.exception("Failed to recover orphan scan %s", f)

    def stop_all(self, timeout: float = 5.0) -> None:
        with self._lock:
            for inbox_id in list(self._inboxes):
                self._stop_inbox(inbox_id)
```

Add to `requirements.txt`:

```
watchdog
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_scan_watcher.py`
Expected: PASS (all watcher tests).

- [ ] **Step 5: Commit**

```bash
git add document_search/services/scan_watcher.py requirements.txt tests/test_scan_watcher.py
git commit -m "feat(scan): watcher manager with polling, optional watchdog, recovery"
```

---

## Phase E — `scan_ingest`-Job, Metriken & App-Wiring

### Task E1: Scan-Metriken

**Files:**
- Modify: `document_search/observability.py` (add two metrics near `INDEX_DOCS_TOTAL`)
- Test: `tests/test_scan_ingest_job.py` (create with a metrics smoke test)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scan_ingest_job.py
import document_search.observability as obs


def test_scan_metrics_registered():
    assert obs.SCAN_INGESTED_TOTAL is not None
    assert obs.SCAN_REVIEW_PENDING is not None
    # Labelled counter increments without error.
    obs.SCAN_INGESTED_TOTAL.labels(inbox="b", outcome="pending").inc()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_scan_ingest_job.py -k metrics`
Expected: FAIL (`AttributeError: ... SCAN_INGESTED_TOTAL`).

- [ ] **Step 3: Write minimal implementation**

In `observability.py`, after `INDEX_DOCS_TOTAL`:

```python
# Scanned documents ingested, labelled by inbox and outcome (pending|error).
SCAN_INGESTED_TOTAL = Counter(
    "seekr_scan_ingested_total",
    "Scanned documents processed by the scan-inbox ingester.",
    ["inbox", "outcome"],
    registry=REGISTRY,
)

# Current number of scan reviews awaiting human action (refreshed at scrape time).
SCAN_REVIEW_PENDING = Gauge(
    "seekr_scan_review_pending",
    "Scan reviews currently in the pending state.",
    registry=REGISTRY,
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_scan_ingest_job.py -k metrics`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add document_search/observability.py tests/test_scan_ingest_job.py
git commit -m "feat(obs): scan ingest + pending-review metrics"
```

---

### Task E2: `scan_ingest`-Worker-Handler

**Files:**
- Modify: `document_search/app.py` (register `@worker.handler("scan_ingest")` next to the other handlers, e.g. after `_handle_index_paths`)
- Test: `tests/test_scan_ingest_job.py` (append)

**Behavioural contract (the handler does, in order):**
1. Resolve the inbox from current config by `payload["inbox_id"]`; if gone → mark `scan_review` error and return.
2. `extract_for_scan(staging_path, languages)` with languages from config OCR block.
3. On extraction error → `ScanReviewStore.create_error(...)`, metric `outcome="error"`, return.
4. `upsert_document(fingerprint(staging_path), result, owner_principal_id=None)`.
5. Build candidate folder list = existing immediate subdirectories of `target_root`; call `organizer.suggest(...)`; validate the suggested subpath against the candidate list (`ai_validation`), fall back to `None` when not a real folder.
6. `record_ai_decision(...)` → `ai_decision_id`.
7. `set_scan_acl(doc_id, groups, users)` (revoke public, grant reviewers).
8. `ScanReviewStore.create_pending(...)`; metric `outcome="pending"`.

- [ ] **Step 1: Write the failing test (append)**

```python
# tests/test_scan_ingest_job.py  (append)
import json
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from document_search.app import create_app  # noqa: E402
from document_search.services.scan_review_store import ScanReviewStore  # noqa: E402
from document_search.index.sqlite_store import SqliteStore  # noqa: E402


def _run_handler(app, kind, payload):
    """Invoke a registered worker handler synchronously for testing."""
    handler = app.state.worker._handlers[kind]
    return handler(payload, lambda p: None)


def test_scan_ingest_creates_pending_review_with_constrained_suggestion(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    db_path = data / "document_index.db"
    # Build a target_root with a real subfolder the AI is allowed to choose.
    target = tmp_path / "Buchhaltung"
    (target / "Rechnungen").mkdir(parents=True)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "database_path": str(db_path),
        "scan_inboxes": [{
            "id": "b", "label": "B", "inbox_path": str(tmp_path / "in"),
            "target_root": str(target),
            "reviewers": {"groups": [], "users": ["reviewer"]},
        }],
    }), encoding="utf-8")
    (tmp_path / "in").mkdir()
    monkeypatch.setenv("DOCUMENT_SEARCH_CONFIG_PATH", str(config))

    app = create_app(str(db_path))

    # Stage a fake scan and force a deterministic extraction + AI suggestion.
    staging = data / "scan-staging" / "b" / "pending-review"
    staging.mkdir(parents=True)
    staged = staging / "scan001.pdf"
    staged.write_bytes(b"%PDF fake")

    import document_search.app as appmod
    monkeypatch.setattr(
        "document_search.services.scan_extractor.extract_for_scan",
        lambda p, languages: __import__("document_search.models", fromlist=["ExtractionResult", "ContentBlock"]).ExtractionResult(
            file_path=p, status="ok",
            blocks=[__import__("document_search.models", fromlist=["ContentBlock"]).ContentBlock("ocr_page", 1, "Rechnung", "X", {})],
        ),
    )
    # AI proposes a folder that DOES exist -> kept.
    monkeypatch.setattr(appmod.organizer, "suggest",
                        lambda **kw: appmod.organizer.suggest.__self__.__class__.__mro__ and _Sug("Rechnungen", ["rechnung"]))

    _run_handler(app, "scan_ingest",
                 {"inbox_id": "b", "staging_path": str(staged), "original_filename": "scan001.pdf"})

    db = SqliteStore(Path(db_path))
    reviews = ScanReviewStore(db).list_reviews(inbox_ids=["b"], status="pending")
    assert len(reviews) == 1
    assert reviews[0]["suggested_folder"] == "Rechnungen"
    assert reviews[0]["document_id"] is not None


def test_scan_ingest_drops_hallucinated_folder(tmp_path, monkeypatch):
    # Same setup as above but the AI proposes a non-existent folder -> None.
    ...  # mirror the above; assert reviews[0]["suggested_folder"] is None


class _Sug:
    def __init__(self, subpath, tags):
        self.suggested_subpath = subpath
        self.suggested_tags = tags
        self.reason = "because"
        self.model = "test"
```

> Note: the monkeypatch for `organizer.suggest` above is illustrative; implement it with a small lambda returning a `_Sug`. Fill in `test_scan_ingest_drops_hallucinated_folder` by returning `_Sug("Nonexistent", [])` and asserting `suggested_folder is None`. Keep the AI seam at `appmod.organizer.suggest` (the single network seam, per the ai-validation-provenance memory).

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_scan_ingest_job.py -k scan_ingest`
Expected: FAIL (`KeyError: 'scan_ingest'` — handler not registered).

- [ ] **Step 3: Write minimal implementation**

Add a candidate-folder helper and the handler inside `create_app`, near `_handle_index_paths`:

```python
    def _existing_target_subfolders(target_root: str) -> list[str]:
        root = Path(target_root)
        if not root.is_dir():
            return []
        return sorted(p.name for p in root.iterdir() if p.is_dir())

    def _inbox_by_id(inbox_id: str):
        from document_search.services.scan_inbox_config import parse_scan_inboxes
        raw = []
        if config_path.exists():
            try:
                raw = json.loads(config_path.read_text(encoding="utf-8")).get("scan_inboxes", [])
            except Exception:
                raw = []
        for ib in parse_scan_inboxes(raw):
            if ib.id == inbox_id:
                return ib
        return None

    @worker.handler("scan_ingest")
    def _handle_scan_ingest(payload: dict, progress_cb):
        from document_search.services.scan_extractor import extract_for_scan
        from document_search.services.scan_review_store import ScanReviewStore

        inbox_id = payload["inbox_id"]
        staging_path = payload["staging_path"]
        original_filename = payload["original_filename"]
        db = SqliteStore(Path(db_path))
        srs = ScanReviewStore(db)

        inbox = _inbox_by_id(inbox_id)
        if inbox is None:
            srs.create_error(inbox_id=inbox_id, staging_path=staging_path,
                             original_filename=original_filename,
                             error_message=f"Unknown scan inbox '{inbox_id}'")
            _obs.SCAN_INGESTED_TOTAL.labels(inbox=inbox_id, outcome="error").inc()
            return {"status": "error", "reason": "unknown_inbox"}

        cfg = load_config(config_path) if config_path.exists() else AppConfig()
        languages = "+".join(cfg.ocr.languages) if cfg.ocr.languages else "deu+eng"
        result = extract_for_scan(Path(staging_path), languages=languages)
        if result.status == "error":
            srs.create_error(inbox_id=inbox_id, staging_path=staging_path,
                             original_filename=original_filename,
                             error_message=result.error_message or "extraction failed")
            _obs.SCAN_INGESTED_TOTAL.labels(inbox=inbox_id, outcome="error").inc()
            return {"status": "error", "reason": "extraction"}

        fp = fingerprint(Path(staging_path))
        doc_id = db.upsert_document(fp, result, owner_principal_id=None)

        candidates = _existing_target_subfolders(inbox.target_root)
        extracted_text = " ".join(b.text[:500] for b in result.blocks[:6])
        suggested_folder = None
        suggested_tags: list[str] = []
        reason = None
        try:
            sug = organizer.suggest(
                file_path=Path(staging_path),
                extracted_text=extracted_text,
                tags=[],
                metadata={"candidate_folders": ", ".join(candidates),
                          "filename": original_filename},
            )
            # Constrain: only keep a suggestion that names a real existing folder.
            if sug.suggested_subpath and sug.suggested_subpath in candidates:
                suggested_folder = sug.suggested_subpath
            suggested_tags = sug.suggested_tags or []
            reason = sug.reason
        except Exception:
            log.warning("AI suggestion unavailable for scan %s; manual filing", staging_path, exc_info=True)

        ai_decision_id = db.record_ai_decision(
            kind="scan_filing",
            model=getattr(organizer, "model", None),
            prompt_sha256=hashlib.sha256(extracted_text.encode()).hexdigest(),
            document_id=doc_id,
            output={"suggested_folder": suggested_folder, "suggested_tags": suggested_tags,
                    "reason": reason, "candidates": candidates},
            applied=0,
            user_id=None,
        )
        db.set_scan_acl(doc_id, group_external_ids=inbox.reviewers_groups,
                        user_external_ids=inbox.reviewers_users)
        srs.create_pending(
            inbox_id=inbox_id, document_id=doc_id, staging_path=staging_path,
            original_filename=original_filename, suggested_folder=suggested_folder,
            suggested_tags=suggested_tags, ai_reasoning=reason, ai_decision_id=ai_decision_id,
        )
        _obs.SCAN_INGESTED_TOTAL.labels(inbox=inbox_id, outcome="pending").inc()
        return {"status": "pending", "document_id": doc_id, "review_inbox": inbox_id}
```

> `fingerprint`, `organizer`, `_obs`, `load_config`, `AppConfig`, `config_path`, `hashlib`, `SqliteStore` are all already imported/available in `create_app` (confirm imports at module top; add `import hashlib` if not present).

Expose the worker on `app.state` for tests (if not already): find where `worker` is created in `create_app` and add `app.state.worker = worker`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_scan_ingest_job.py -k scan_ingest`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add document_search/app.py tests/test_scan_ingest_job.py
git commit -m "feat(scan): scan_ingest worker handler (OCR, constrained AI filing, reviewer ACL)"
```

---

### Task E3: Watcher-Manager-Wiring (startup/shutdown) + Enqueue-Bridge

**Files:**
- Modify: `document_search/app.py` (instantiate `ScanWatcherManager`, wire startup/shutdown, recovery; expose on `app.state`)
- Test: `tests/test_scan_ingest_job.py` (append integration-lite test)

- [ ] **Step 1: Write the failing test (append)**

```python
# tests/test_scan_ingest_job.py  (append)
def test_watcher_manager_wired_on_app(tmp_path, monkeypatch):
    db_path = tmp_path / "document_index.db"
    config = tmp_path / "config.json"
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    config.write_text(json.dumps({
        "database_path": str(db_path),
        "scan_inboxes": [{"id": "b", "label": "B", "inbox_path": str(tmp_path / "in"),
                          "target_root": str(tmp_path / "out"), "enabled": True}],
    }), encoding="utf-8")
    monkeypatch.setenv("DOCUMENT_SEARCH_CONFIG_PATH", str(config))
    app = create_app(str(db_path))
    assert app.state.scan_watcher_manager is not None
    # The enqueue bridge creates a scan_ingest job.
    app.state.scan_watcher_manager._enqueue("b", str(tmp_path / "in" / "x.pdf"), "x.pdf")
    jobs = app.state.job_store.list_jobs(limit=10)
    assert any(j["kind"] == "scan_ingest" for j in jobs)
```

> Confirm `app.state.job_store` is exposed; if not, add `app.state.job_store = job_store` in `create_app`. Adapt `list_jobs` call to its real signature (see `JobStore.list_jobs`).

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_scan_ingest_job.py -k watcher_manager_wired`
Expected: FAIL (`AttributeError: ... scan_watcher_manager`).

- [ ] **Step 3: Write minimal implementation**

In `create_app`, after the worker/scheduler setup, add:

```python
    from document_search.services.scan_watcher import ScanWatcherManager

    _data_dir = Path(db_path).resolve().parent

    def _enqueue_scan_ingest(inbox_id: str, staging_path: str, original_filename: str) -> None:
        job_store.enqueue("scan_ingest", {
            "inbox_id": inbox_id,
            "staging_path": staging_path,
            "original_filename": original_filename,
        }, owner_user_id=None, max_retries=0)

    scan_watcher_manager = ScanWatcherManager(data_dir=_data_dir, enqueue=_enqueue_scan_ingest)
    app.state.scan_watcher_manager = scan_watcher_manager
    app.state.job_store = job_store  # if not already exposed

    def _current_scan_inboxes():
        from document_search.services.scan_inbox_config import parse_scan_inboxes
        raw = []
        if config_path.exists():
            try:
                raw = json.loads(config_path.read_text(encoding="utf-8")).get("scan_inboxes", [])
            except Exception:
                log.warning("Failed to read scan_inboxes from config", exc_info=True)
        try:
            return parse_scan_inboxes(raw)
        except Exception:
            log.warning("Invalid scan_inboxes config; watcher idle", exc_info=True)
            return []

    app.state.current_scan_inboxes = _current_scan_inboxes
```

Extend the existing `@app.on_event("startup")` worker startup function to also start watchers + recovery:

```python
        # Scan watcher: recover orphaned staged files, then start enabled inboxes.
        inboxes = _current_scan_inboxes()
        from document_search.services.scan_review_store import ScanReviewStore
        known = ScanReviewStore(store()).staging_paths_with_rows()
        scan_watcher_manager.recover_orphans(inboxes, known_staging_paths=known)
        scan_watcher_manager.reconfigure(inboxes)
```

Extend the existing `@app.on_event("shutdown")` to stop watchers:

```python
        scan_watcher_manager.stop_all(timeout=5.0)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_scan_ingest_job.py -k watcher_manager_wired`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add document_search/app.py tests/test_scan_ingest_job.py
git commit -m "feat(scan): wire watcher manager into app lifecycle + enqueue bridge"
```

---

## Phase F — API-Routen

### Task F1: Config-Speicherung um `scan_inboxes` erweitern + Test-Route

**Files:**
- Modify: `document_search/app.py` (`UiConfigRequest`: add `scan_inboxes`; `api_save_config`: validate + persist + `scan_watcher_manager.reconfigure`; add `POST /api/scan/inboxes/test`)
- Test: `tests/test_scan_api.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scan_api.py
import json
import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from document_search.app import create_app  # noqa: E402


def _admin_client(tmp_path, monkeypatch, extra_cfg=None):
    db_path = tmp_path / "document_index.db"
    cfg = {"database_path": str(db_path)}
    cfg.update(extra_cfg or {})
    config = tmp_path / "config.json"
    config.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("DOCUMENT_SEARCH_CONFIG_PATH", str(config))
    app = create_app(str(db_path))
    client = TestClient(app)
    r = client.post("/api/login", json={"username": "admin", "password": "admin"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    return app, client, token, config


def test_path_test_route_validates_inbox(tmp_path, monkeypatch):
    app, client, token, _ = _admin_client(tmp_path, monkeypatch)
    inbox = tmp_path / "in"; inbox.mkdir()
    target = tmp_path / "out"; target.mkdir()
    r = client.post("/api/scan/inboxes/test",
                    headers={"X-Auth-Token": token},
                    json={"inbox_path": str(inbox), "target_root": str(target)})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_path_test_rejects_inbox_inside_target(tmp_path, monkeypatch):
    app, client, token, _ = _admin_client(tmp_path, monkeypatch)
    target = tmp_path / "out"; (target / "in").mkdir(parents=True)
    r = client.post("/api/scan/inboxes/test",
                    headers={"X-Auth-Token": token},
                    json={"inbox_path": str(target / "in"), "target_root": str(target)})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "inside" in r.json()["error"]
```

> Confirm the login route path/payload (`/api/login`) and default admin creds against existing FastAPI tests in `tests/` and adapt if different.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_scan_api.py -k path_test`
Expected: FAIL (404 — route missing).

- [ ] **Step 3: Write minimal implementation**

Add a Pydantic model near the other request models:

```python
class ScanInboxTestRequest(BaseModel):
    inbox_path: str
    target_root: str
```

Add the route inside `create_app`:

```python
    @app.post("/api/scan/inboxes/test")
    def api_scan_inbox_test(req: ScanInboxTestRequest, x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        from document_search.services.scan_inbox_config import (
            ScanInbox, ScanInboxConfigError, validate_inbox_paths,
        )
        ib = ScanInbox(id="_test", label="_test",
                       inbox_path=req.inbox_path, target_root=req.target_root)
        try:
            validate_inbox_paths(ib)
        except ScanInboxConfigError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}
```

Extend `UiConfigRequest` with:

```python
    scan_inboxes: list[dict] = Field(default_factory=list)
```

In `api_save_config`, before `config_path.write_text(...)`, validate and (after writing) reconfigure the watcher:

```python
        from document_search.services.scan_inbox_config import (
            ScanInboxConfigError, parse_scan_inboxes, validate_inbox_paths,
        )
        try:
            parsed_inboxes = parse_scan_inboxes(req.scan_inboxes)
            for ib in parsed_inboxes:
                validate_inbox_paths(ib)
        except ScanInboxConfigError as e:
            raise HTTPException(status_code=400, detail=f"Invalid scan inbox: {e}")
```

After the existing `config_path.write_text(...)` and OCR-env block, add:

```python
        try:
            app.state.scan_watcher_manager.reconfigure(parsed_inboxes)
        except Exception:
            log.warning("Failed to reconfigure scan watchers after config save", exc_info=True)
```

> `req.model_dump()` already includes `scan_inboxes`, so persistence needs no extra code. Ensure the JSON written keeps the normalized form — optionally replace `req.scan_inboxes` in the dumped dict with `[ib.to_dict() for ib in parsed_inboxes]` so derived ids are persisted. Implement that normalization before `write_text`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_scan_api.py -k path_test`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add document_search/app.py tests/test_scan_api.py
git commit -m "feat(scan): config persistence + path-test route for scan inboxes"
```

---

### Task F2: Review-Listing, Folders, Confirm/Reject/Retry

**Files:**
- Modify: `document_search/app.py` (auth helper for inbox visibility; the five review routes)
- Test: `tests/test_scan_api.py` (append)

**Inbox authorization rule (helper `_authorized_inbox_ids(user_id)`):**
- Admin → all configured inbox ids.
- Else → inboxes where `username in reviewers_users` OR `user_group_external_ids(user_id) ∩ reviewers_groups`.

- [ ] **Step 1: Write the failing test (append)**

```python
# tests/test_scan_api.py  (append)
from pathlib import Path
from datetime import datetime, UTC

from document_search.index.sqlite_store import SqliteStore
from document_search.services.scan_review_store import ScanReviewStore
from document_search.models import ExtractionResult, FileFingerprint, ContentBlock


def _seed_pending(db_path, tmp_path, inbox_id="b", folder="Rechnungen"):
    db = SqliteStore(Path(db_path))
    p = tmp_path / "staged.pdf"; p.write_text("x")
    fp = FileFingerprint(path=p, file_size=1, modified_at=datetime.now(tz=UTC), sha256="h1")
    ext = ExtractionResult(file_path=p, status="ok",
                           blocks=[ContentBlock("ocr_page", 1, "Rechnung", "X", {})])
    doc_id = db.upsert_document(fp, ext)
    srs = ScanReviewStore(db)
    rid = srs.create_pending(inbox_id=inbox_id, document_id=doc_id, staging_path=str(p),
                             original_filename="staged.pdf", suggested_folder=folder,
                             suggested_tags=["rechnung"], ai_reasoning="r", ai_decision_id=None)
    return rid, doc_id, p


def test_admin_lists_pending_reviews(tmp_path, monkeypatch):
    inbox = tmp_path / "in"; inbox.mkdir()
    target = tmp_path / "Buchhaltung"; (target / "Rechnungen").mkdir(parents=True)
    app, client, token, _ = _admin_client(tmp_path, monkeypatch, extra_cfg={
        "scan_inboxes": [{"id": "b", "label": "B", "inbox_path": str(inbox),
                          "target_root": str(target)}]})
    rid, doc_id, _ = _seed_pending(tmp_path / "document_index.db", tmp_path)
    r = client.get("/api/scan/review?inbox=b&status=pending", headers={"X-Auth-Token": token})
    assert r.status_code == 200
    assert any(item["id"] == rid for item in r.json())


def test_folders_route_lists_existing_subfolders(tmp_path, monkeypatch):
    inbox = tmp_path / "in"; inbox.mkdir()
    target = tmp_path / "Buchhaltung"; (target / "Rechnungen").mkdir(parents=True)
    (target / "Verträge").mkdir()
    app, client, token, _ = _admin_client(tmp_path, monkeypatch, extra_cfg={
        "scan_inboxes": [{"id": "b", "label": "B", "inbox_path": str(inbox),
                          "target_root": str(target)}]})
    rid, _, _ = _seed_pending(tmp_path / "document_index.db", tmp_path)
    r = client.get(f"/api/scan/review/{rid}/folders", headers={"X-Auth-Token": token})
    assert r.status_code == 200
    assert set(r.json()) == {"Rechnungen", "Verträge"}


def test_confirm_moves_file_and_marks_filed(tmp_path, monkeypatch):
    inbox = tmp_path / "in"; inbox.mkdir()
    target = tmp_path / "Buchhaltung"; (target / "Rechnungen").mkdir(parents=True)
    app, client, token, _ = _admin_client(tmp_path, monkeypatch, extra_cfg={
        "scan_inboxes": [{"id": "b", "label": "B", "inbox_path": str(inbox),
                          "target_root": str(target)}]})
    rid, doc_id, staged = _seed_pending(tmp_path / "document_index.db", tmp_path)
    r = client.post(f"/api/scan/review/{rid}/confirm", headers={"X-Auth-Token": token},
                    json={"folder": "Rechnungen", "tags": ["rechnung"]})
    assert r.status_code == 200, r.text
    assert (target / "Rechnungen" / "staged.pdf").exists()
    assert not staged.exists()


def test_confirm_rejects_path_traversal(tmp_path, monkeypatch):
    inbox = tmp_path / "in"; inbox.mkdir()
    target = tmp_path / "Buchhaltung"; (target / "Rechnungen").mkdir(parents=True)
    app, client, token, _ = _admin_client(tmp_path, monkeypatch, extra_cfg={
        "scan_inboxes": [{"id": "b", "label": "B", "inbox_path": str(inbox),
                          "target_root": str(target)}]})
    rid, _, _ = _seed_pending(tmp_path / "document_index.db", tmp_path)
    r = client.post(f"/api/scan/review/{rid}/confirm", headers={"X-Auth-Token": token},
                    json={"folder": "../../etc", "tags": []})
    assert r.status_code == 400


def test_reject_removes_from_index(tmp_path, monkeypatch):
    inbox = tmp_path / "in"; inbox.mkdir()
    target = tmp_path / "Buchhaltung"; target.mkdir()
    app, client, token, _ = _admin_client(tmp_path, monkeypatch, extra_cfg={
        "scan_inboxes": [{"id": "b", "label": "B", "inbox_path": str(inbox),
                          "target_root": str(target)}]})
    rid, doc_id, staged = _seed_pending(tmp_path / "document_index.db", tmp_path)
    r = client.post(f"/api/scan/review/{rid}/reject", headers={"X-Auth-Token": token})
    assert r.status_code == 200
    db = SqliteStore(Path(tmp_path / "document_index.db"))
    assert db.conn.execute("SELECT 1 FROM documents WHERE id=?", (doc_id,)).fetchone() is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_scan_api.py -k "lists_pending or folders_route or confirm or reject"`
Expected: FAIL (404 routes missing).

- [ ] **Step 3: Write minimal implementation**

Add request models:

```python
class ScanConfirmRequest(BaseModel):
    folder: str
    tags: list[str] = Field(default_factory=list)
    new_folder: bool = False
```

Add the helper + routes inside `create_app`:

```python
    def _authorized_inbox_ids(user_id: int) -> list[str]:
        inboxes = app.state.current_scan_inboxes()
        sess_role = session_store_role(user_id)  # see note
        if sess_role == "admin":
            return [ib.id for ib in inboxes]
        db = store()
        groups = db.user_group_external_ids(user_id)
        username = db.get_username(user_id)  # add if missing: SELECT username FROM users WHERE id=?
        out = []
        for ib in inboxes:
            if username in ib.reviewers_users or (groups & set(ib.reviewers_groups)):
                out.append(ib.id)
        return out

    def _require_inbox_access(user_id: int, inbox_id: str) -> None:
        if inbox_id not in _authorized_inbox_ids(user_id):
            raise HTTPException(status_code=403, detail="Not authorized for this scan inbox")

    @app.get("/api/scan/inboxes")
    def api_scan_inboxes(x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        allowed = set(_authorized_inbox_ids(user_id))
        return [{"id": ib.id, "label": ib.label}
                for ib in app.state.current_scan_inboxes() if ib.id in allowed]

    @app.get("/api/scan/review")
    def api_scan_review_list(inbox: str | None = None, status: str | None = None,
                             x_auth_token: str | None = Header(default=None)):
        from document_search.services.scan_review_store import ScanReviewStore
        user_id = require_user(x_auth_token)
        allowed = _authorized_inbox_ids(user_id)
        inbox_ids = [inbox] if inbox else allowed
        inbox_ids = [i for i in inbox_ids if i in allowed]
        return ScanReviewStore(store()).list_reviews(inbox_ids=inbox_ids, status=status)

    def _review_or_404(review_id: int):
        from document_search.services.scan_review_store import ScanReviewStore
        row = ScanReviewStore(store()).get(review_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Review not found")
        return row

    @app.get("/api/scan/review/{review_id}/folders")
    def api_scan_review_folders(review_id: int, x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        row = _review_or_404(review_id)
        _require_inbox_access(user_id, row["inbox_id"])
        inbox = _inbox_by_id(row["inbox_id"])
        if inbox is None:
            raise HTTPException(status_code=404, detail="Inbox no longer configured")
        return _existing_target_subfolders(inbox.target_root)

    @app.post("/api/scan/review/{review_id}/confirm")
    def api_scan_review_confirm(review_id: int, req: ScanConfirmRequest, request: Request,
                                x_auth_token: str | None = Header(default=None)):
        from document_search.services.scan_review_store import ScanReviewStore
        user_id = require_user(x_auth_token)
        row = _review_or_404(review_id)
        _require_inbox_access(user_id, row["inbox_id"])
        if row["status"] != "pending":
            raise HTTPException(status_code=409, detail=f"Review is {row['status']}, not pending")
        inbox = _inbox_by_id(row["inbox_id"])
        if inbox is None:
            raise HTTPException(status_code=404, detail="Inbox no longer configured")

        # Validate the chosen folder against target_root (traversal-proof).
        target_root = Path(inbox.target_root).resolve()
        folder = (req.folder or "").strip()
        if not folder or "\x00" in folder:
            raise HTTPException(status_code=400, detail="Invalid folder")
        dest_dir = (target_root / folder).resolve()
        if not (dest_dir == target_root or dest_dir.is_relative_to(target_root)):
            raise HTTPException(status_code=400, detail="folder escapes target_root")
        if not req.new_folder and not dest_dir.is_dir():
            raise HTTPException(status_code=400, detail="folder does not exist (set new_folder to create)")

        db = store()
        staged = Path(row["staging_path"])
        if not staged.exists():
            raise HTTPException(status_code=410, detail="Staged file is gone")
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            new_path = dest_dir / staged.name
            import shutil as _shutil
            _shutil.move(str(staged), str(new_path))
            db.move_document(row["document_id"], str(new_path))
            if req.tags:
                db.set_tags(user_id, row["document_id"], req.tags)
            db.restore_public_read(row["document_id"])
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Filing failed: {e}")

        db.record_ai_decision(
            kind="scan_filing_confirmed",
            model=None, prompt_sha256="",
            document_id=row["document_id"],
            output={"chosen_folder": folder, "tags": req.tags,
                    "suggested_folder": row["suggested_folder"],
                    "accepted_suggestion": folder == (row["suggested_folder"] or "")},
            applied=1, user_id=user_id,
        )
        ScanReviewStore(db).mark_filed(review_id, reviewed_by=db.get_username(user_id))
        _audit(user_id, "scan.review.confirm", target_type="document",
               target_id=row["document_id"],
               detail={"inbox": row["inbox_id"], "folder": folder}, request=request)
        return {"status": "filed", "path": str(new_path)}

    @app.post("/api/scan/review/{review_id}/reject")
    def api_scan_review_reject(review_id: int, request: Request,
                               x_auth_token: str | None = Header(default=None)):
        from document_search.services.scan_review_store import ScanReviewStore
        user_id = require_user(x_auth_token)
        row = _review_or_404(review_id)
        _require_inbox_access(user_id, row["inbox_id"])
        db = store()
        if row["document_id"] is not None:
            db.delete_documents([row["document_id"]])
        # Move the original out of pending-review into a rejected/ sibling.
        staged = Path(row["staging_path"])
        if staged.exists():
            rejected_dir = staged.parent.parent / "rejected"
            rejected_dir.mkdir(parents=True, exist_ok=True)
            import shutil as _shutil
            try:
                _shutil.move(str(staged), str(rejected_dir / staged.name))
            except OSError:
                log.warning("Failed to move rejected scan %s", staged, exc_info=True)
        ScanReviewStore(db).mark_rejected(review_id, reviewed_by=db.get_username(user_id))
        _audit(user_id, "scan.review.reject", target_type="scan_review",
               target_id=review_id, detail={"inbox": row["inbox_id"]}, request=request)
        return {"status": "rejected"}

    @app.post("/api/scan/review/{review_id}/retry")
    def api_scan_review_retry(review_id: int, request: Request,
                              x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        row = _review_or_404(review_id)
        _require_inbox_access(user_id, row["inbox_id"])
        if row["status"] != "error":
            raise HTTPException(status_code=409, detail="Only error reviews can be retried")
        job_store.enqueue("scan_ingest", {
            "inbox_id": row["inbox_id"], "staging_path": row["staging_path"],
            "original_filename": row["original_filename"],
        }, owner_user_id=user_id, max_retries=0)
        # Remove the stale error row; the re-run creates a fresh pending row.
        from document_search.services.scan_review_store import ScanReviewStore
        ScanReviewStore(store()).set_pending(review_id)
        _audit(user_id, "scan.review.retry", target_type="scan_review",
               target_id=review_id, detail={"inbox": row["inbox_id"]}, request=request)
        return {"status": "retrying"}
```

> **Helper notes:**
> - `session_store_role(user_id)` is illustrative — replace with the real way a route learns the caller's role. `require_admin` already encodes the admin check; simplest is to derive role from the session inside `_authorized_inbox_ids` by reading the session via the token. Since these helpers take `user_id`, add a tiny `store().get_user_role(user_id)` (`SELECT role FROM users WHERE id=?`) OR thread the role through. Implement `get_user_role` and `get_username` on `SqliteStore` if absent.
> - `set_tags(user_id, doc_id, tags)` exists (used by upload). Confirm signature.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_scan_api.py`
Expected: PASS (all scan API tests).

- [ ] **Step 5: Commit**

```bash
git add document_search/app.py document_search/index/sqlite_store.py tests/test_scan_api.py
git commit -m "feat(scan): review list/folders/confirm/reject/retry API"
```

---

## Phase G — Review-UI & Config-UI

### Task G1: ACL-Regressionstest (Legacy-DB) für Scan-Sichtbarkeit

**Files:**
- Test: `tests/test_scan_acl_regression.py`

This task is test-only — it locks in the reviewer-only visibility guarantee against the backfill, including a legacy-DB path (per the schema-migration-ordering + acl-backfill memories). No production code should be needed; if it fails, fix the ACL code from Task C2/E2.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scan_acl_regression.py
from datetime import UTC, datetime
from pathlib import Path

from document_search.index.sqlite_store import SqliteStore
from document_search.models import ContentBlock, ExtractionResult, FileFingerprint
from document_search.services.acl_service import visible_document_ids_subquery


def _make_scan_doc(db, tmp_path):
    p = tmp_path / "scan.pdf"; p.write_text("x")
    fp = FileFingerprint(path=p, file_size=1, modified_at=datetime.now(tz=UTC), sha256="hh")
    ext = ExtractionResult(file_path=p, status="ok",
                           blocks=[ContentBlock("ocr_page", 1, "secret", "X", {})])
    return db.upsert_document(fp, ext)


def _visible(db, user_id, doc_id):
    sql, params = visible_document_ids_subquery(user_id)
    return bool(db.conn.execute(f"SELECT 1 FROM ({sql}) WHERE document_id=?",
                                list(params) + [doc_id]).fetchall())


def test_scan_doc_stays_reviewer_only_across_store_reopen(tmp_path):
    """The backfill runs on every store construction and re-publicises docs with
    no ACL rows. A scanned doc must keep explicit reviewer grants and stay hidden
    from non-reviewers even after the DB is reopened (backfill re-run)."""
    db_path = Path(tmp_path / "document_index.db")
    db = SqliteStore(db_path)
    reviewer = db.create_user("reviewer", "pw12345678", role="user")
    other = db.create_user("other", "pw12345678", role="user")
    doc_id = _make_scan_doc(db, tmp_path)
    db.set_scan_acl(doc_id, group_external_ids=[], user_external_ids=["reviewer"])
    assert _visible(db, reviewer, doc_id)
    assert not _visible(db, other, doc_id)
    db.conn.close()

    db2 = SqliteStore(db_path)  # re-open -> _backfill_acl runs again
    assert _visible(db2, reviewer, doc_id)
    assert not _visible(db2, other, doc_id), "backfill must not re-publicise a scan doc"
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `pytest -q tests/test_scan_acl_regression.py`
Expected: PASS if Task C2 is correct. If it FAILS (doc visible to `other` after reopen), the backfill is re-granting public read — fix by ensuring `set_scan_acl` leaves at least one explicit ACL row so `_backfill_acl`'s `WHERE NOT EXISTS (... document_acl ...)` guard skips it. (Granting a reviewer row already satisfies this; the failing case would be an inbox with no reviewers — handle by always granting read to an `admins` principal or leaving a sentinel. If no reviewers are configured, grant read to the owner/admin group so the doc is never left ACL-empty.)

> **Design follow-up to implement if needed:** in `set_scan_acl`, when both reviewer lists are empty, still insert a non-public sentinel ACL row (e.g. grant read to a dedicated `scan-admins` group created on demand, or to the configured default owner principal) so the row count is never zero. Add a test for the empty-reviewers case.

- [ ] **Step 3: Commit**

```bash
git add tests/test_scan_acl_regression.py document_search/index/sqlite_store.py
git commit -m "test(scan): reviewer-only ACL regression across backfill reopen"
```

---

### Task G2: „Scan-Posteingang"-Seite (Template + Route + Nav)

**Files:**
- Create: `document_search/web/templates/scan.html`
- Modify: `document_search/app.py` (add `@app.get("/scan")` page route)
- Modify: `document_search/web/templates/*.html` (add a "Scan" nav link in the shared topbar of each page; mirror the existing nav markup)

- [ ] **Step 1: Add the page route**

In `create_app`, next to `ingest_page`:

```python
    @app.get("/scan", response_class=HTMLResponse)
    def scan_page(request: Request):
        return templates.TemplateResponse("scan.html", {"request": request})
```

- [ ] **Step 2: Create the template**

Create `document_search/web/templates/scan.html` by copying the structure of `ingest.html` (topbar nav, auth-gate partial, toast zone, `app.js` include) and replacing the main content with:

```html
<main class="container">
  <div id="authGate"></div>
  <section class="card">
    <header class="card-header">
      <h2>Scan-Posteingang</h2>
      <p class="muted">Gescannte Dokumente prüfen und ablegen.</p>
    </header>
    <div class="card-body">
      <div class="row">
        <label>Eingang
          <select id="scanInboxSelect"></select>
        </label>
        <label>Status
          <select id="scanStatusSelect">
            <option value="pending">Offen</option>
            <option value="error">Fehler</option>
            <option value="">Alle</option>
          </select>
        </label>
        <button id="scanRefreshBtn" class="btn">Aktualisieren</button>
      </div>
      <div id="scanReviewList" class="list" aria-live="polite"></div>
    </div>
  </section>
</main>
<div id="toastZone" class="toast-zone"></div>
```

> Match the exact topbar/`<head>`/script-include markup from `ingest.html` so styling and auth flow are identical. Add the `<a href="/scan">Scan</a>` nav entry into the topbar of every page template that has the shared nav (index/search/ingest/config/jobs/wiki and the new scan.html).

- [ ] **Step 3: Manual smoke check**

Run: `uvicorn document_search.app:app --port 8080` (or the project's run command) and open `/scan`. Expected: page renders, auth gate works, empty list shows an empty state. (No automated assertion — covered functionally by the API tests.)

- [ ] **Step 4: Commit**

```bash
git add document_search/web/templates/ document_search/app.py
git commit -m "feat(scan): scan inbox review page + nav link"
```

---

### Task G3: Frontend-Logik (Review-Modul in `app.js`)

**Files:**
- Modify: `document_search/web/static/app.js` (append a scan-review module, guarded by presence of `#scanReviewList`)

- [ ] **Step 1: Implement the module**

Append to `app.js`, following the existing `api(path, method, body)` helper + `showToast` + `escHtml` conventions:

```javascript
// ---- Scan inbox review -------------------------------------------------
(function scanReviewModule() {
  const listEl = document.getElementById('scanReviewList');
  if (!listEl) return; // only on /scan
  const inboxSel = document.getElementById('scanInboxSelect');
  const statusSel = document.getElementById('scanStatusSelect');
  const refreshBtn = document.getElementById('scanRefreshBtn');

  async function loadInboxes() {
    const inboxes = await api('/api/scan/inboxes');
    inboxSel.innerHTML = inboxes.map(i => `<option value="${escHtml(i.id)}">${escHtml(i.label)}</option>`).join('');
  }

  function reviewCard(item) {
    const tags = (item.suggested_tags || []).map(t => `<span class="chip">${escHtml(t)}</span>`).join('');
    const suggestion = item.suggested_folder
      ? `Vorschlag: <strong>${escHtml(item.suggested_folder)}</strong>`
      : '<em>kein KI-Vorschlag</em>';
    const err = item.status === 'error'
      ? `<div class="error">${escHtml(item.error_message || 'Fehler')}</div>
         <button data-retry="${item.id}" class="btn">Erneut versuchen</button>`
      : `<label>Zielordner <select data-folder="${item.id}"></select></label>
         <label>Tags <input data-tags="${item.id}" value="${escHtml((item.suggested_tags||[]).join(', '))}"></label>
         <button data-confirm="${item.id}" class="btn btn-primary">Bestätigen</button>
         <button data-reject="${item.id}" class="btn btn-danger">Ablehnen</button>`;
    return `<article class="review-row" data-row="${item.id}">
        <div class="review-meta"><strong>${escHtml(item.original_filename)}</strong>
          <span class="muted">${escHtml(item.created_at)}</span></div>
        <div class="review-suggestion">${suggestion} ${tags}</div>
        ${item.ai_reasoning ? `<div class="muted">${escHtml(item.ai_reasoning)}</div>` : ''}
        <div class="review-actions">${err}</div>
      </article>`;
  }

  async function fillFolders(reviewId, selectEl, preselect) {
    const folders = await api(`/api/scan/review/${reviewId}/folders`);
    selectEl.innerHTML = folders.map(f =>
      `<option value="${escHtml(f)}"${f === preselect ? ' selected' : ''}>${escHtml(f)}</option>`).join('');
  }

  async function refresh() {
    const inbox = inboxSel.value;
    const status = statusSel.value;
    if (!inbox) { listEl.innerHTML = '<div class="empty">Kein Eingang.</div>'; return; }
    let items;
    try {
      items = await api(`/api/scan/review?inbox=${encodeURIComponent(inbox)}&status=${encodeURIComponent(status)}`);
    } catch (e) { showToast(String(e.message || e), 'error'); return; }
    if (!items.length) { listEl.innerHTML = '<div class="empty">Nichts zu prüfen.</div>'; return; }
    listEl.innerHTML = items.map(reviewCard).join('');
    for (const item of items) {
      if (item.status !== 'error') {
        const sel = listEl.querySelector(`select[data-folder="${item.id}"]`);
        if (sel) await fillFolders(item.id, sel, item.suggested_folder);
      }
    }
  }

  listEl.addEventListener('click', async (ev) => {
    const t = ev.target;
    const id = t.getAttribute('data-confirm') || t.getAttribute('data-reject')
      || t.getAttribute('data-retry');
    if (!id) return;
    try {
      if (t.hasAttribute('data-confirm')) {
        const folder = listEl.querySelector(`select[data-folder="${id}"]`).value;
        const tags = listEl.querySelector(`input[data-tags="${id}"]`).value
          .split(',').map(s => s.trim()).filter(Boolean);
        await api(`/api/scan/review/${id}/confirm`, 'POST', { folder, tags });
        showToast('Abgelegt', 'success');
      } else if (t.hasAttribute('data-reject')) {
        await api(`/api/scan/review/${id}/reject`, 'POST');
        showToast('Abgelehnt', 'success');
      } else {
        await api(`/api/scan/review/${id}/retry`, 'POST');
        showToast('Erneut eingereiht', 'success');
      }
      await refresh();
    } catch (e) { showToast(String(e.message || e), 'error'); }
  });

  refreshBtn.addEventListener('click', refresh);
  inboxSel.addEventListener('change', refresh);
  statusSel.addEventListener('change', refresh);
  (async () => { try { await loadInboxes(); await refresh(); } catch (_) {} })();
})();
```

> Confirm `api()`, `showToast()`, `escHtml()` names against the real `app.js` (the exploration confirmed these exist) and adjust if the helper signatures differ.

- [ ] **Step 2: Manual smoke check**

Reload `/scan` with at least one configured inbox and a seeded pending review (or after dropping a file into a real inbox). Expected: list renders, folder dropdown populates from `target_root` subfolders, confirm/reject/retry round-trip and the list refreshes.

- [ ] **Step 3: Commit**

```bash
git add document_search/web/static/app.js
git commit -m "feat(scan): review UI logic (list, constrained folder pick, confirm/reject/retry)"
```

---

### Task G4: Config-UI „Scan-Eingänge" (CRUD)

**Files:**
- Modify: `document_search/web/templates/config.html` (add a "Scan-Eingänge" card/section, admin-only)
- Modify: `document_search/web/static/app.js` (CRUD module that reads/writes `scan_inboxes` via the existing `/api/config` save, with a per-inbox "Testen" button calling `/api/scan/inboxes/test`)

- [ ] **Step 1: Add the config section**

In `config.html`, in the admin-only area (mirror the existing Paths card), add a card with: a table/list of inboxes (label, inbox_path, target_root, reviewers, stability, poll, enabled), an "Add inbox" form, a "Test" button per row, and a "Save" that posts the full config (including the assembled `scan_inboxes` array) to `/api/config`. Reuse the existing config-save flow — the scan section just contributes the `scan_inboxes` field to the saved payload.

Reviewer pickers: populate "Gruppen" from `GET /api/groups` (external_id) and "Benutzer" from `GET /api/users` (username) as multi-selects.

- [ ] **Step 2: Implement the JS module**

Append a `scanInboxConfigModule()` IIFE to `app.js` (guarded by a `#scanInboxConfig` element) that:
- loads current config (`GET /api/config` if present, else reads from the page's existing config bootstrap) and renders rows from `scan_inboxes`;
- "Test" button → `POST /api/scan/inboxes/test` with `{inbox_path, target_root}`, shows ok/error inline;
- "Save" → merges the assembled `scan_inboxes` into the existing config payload and `POST /api/config`, then toasts success.

```javascript
// ---- Scan inbox config (admin) ----------------------------------------
(function scanInboxConfigModule() {
  const root = document.getElementById('scanInboxConfig');
  if (!root) return;
  // rows state: [{id?, label, inbox_path, target_root, reviewers:{groups:[],users:[]},
  //               stability_seconds, poll_interval_seconds, enabled}]
  let rows = [];

  async function testInbox(inbox_path, target_root) {
    return api('/api/scan/inboxes/test', 'POST', { inbox_path, target_root });
  }

  async function save(fullConfigPayload) {
    fullConfigPayload.scan_inboxes = rows;
    await api('/api/config', 'POST', fullConfigPayload);
    showToast('Scan-Eingänge gespeichert', 'success');
  }
  // render(), addRow(), removeRow(), bindEvents() ... (follow the Paths card pattern)
  window.__scanInboxConfig = { get rows() { return rows; }, set rows(v) { rows = v; }, testInbox, save };
})();
```

> This task is UI plumbing over already-tested APIs (`/api/config` validation + `/api/scan/inboxes/test`). Keep the markup/interactions consistent with the existing Paths management card. No new backend code.

- [ ] **Step 3: Manual smoke check**

Open `/config` as admin → Scan-Eingänge: add an inbox, click Test (ok/error inline), Save, reload — the inbox persists and (if enabled) the watcher starts (visible via a dropped test file appearing in `/scan`).

- [ ] **Step 4: Commit**

```bash
git add document_search/web/templates/config.html document_search/web/static/app.js
git commit -m "feat(scan): admin config UI for scan inboxes (CRUD + test)"
```

---

## Final verification

- [ ] **Run the full suite**

Run: `pytest -q`
Expected: all tests pass (FastAPI integration tests run because `fastapi` is installed; otherwise skipped).

- [ ] **Lint (CI gate)**

Run: `ruff check .`
Expected: no errors. Fix any reported issues.

- [ ] **Root-DB migration smoke (legacy-DB safety)**

Run: `python -c "from pathlib import Path; from document_search.index.sqlite_store import SqliteStore; SqliteStore(Path('document_index.db')); print('ok')"`
Expected: `ok`.

- [ ] **README/docs**

Add a short "Scan inboxes" section to `README.md` documenting: configuring inboxes in Config → Scan-Eingänge, the review queue at `/scan`, the 5-minute stability window, forced OCR, and the reviewer-only ACL. Commit:

```bash
git add README.md
git commit -m "docs(scan): document scan inbox ingestion + review workflow"
```

---

## Self-Review (durchgeführt)

**Spec coverage:** Review-Queue (C/E/F/G) ✓ · Events+Polling (D2) ✓ · 5-Min-Stabilität (D1) ✓ · Staging-Lifecycle (D1) ✓ · Admin+Rollen/Benutzer-Zuständige (C2 ACL, F2 auth) ✓ · mehrere Eingänge (B/D) ✓ · PDF+Bildformate, forcierte OCR (A1/A2) ✓ · Ziel-Wurzel pro Eingang + eingeschränkte Ordnerwahl (E2 candidates, F2 folders/confirm) ✓ · Config über UI (F1, G4) ✓ · explizite Scan-ACL gegen Backfill (C2, G1) ✓ · Provenance/ai_decisions (E2, F2) ✓ · Audit (F2) ✓ · private Metrics-Registry (E1) ✓ · Migrations-Ordering + Legacy-DB-Test (C1, G1) ✓ · Pfad-Traversal-Schutz (F2) ✓ · Recovery bei Neustart (D2, E3) ✓ · Live-Reconfigure (F1) ✓.

**Known integration points to confirm during execution (flagged inline):** exact `create_user`/login route + default-admin creds in tests; `set_tags` signature; presence of `app.state.worker`/`job_store` (add if missing); add `get_username`/`get_user_role` on `SqliteStore` if absent; exact shared-nav markup to clone for `scan.html`; `JobStore.list_jobs` signature. These are small lookups, not design gaps.

**Empty-reviewers ACL edge:** handled in G1 follow-up — never leave a scan doc with zero ACL rows (backfill would re-publicise it).
