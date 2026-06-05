# New Extractors & OCR Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add four new file-type extractors (`.xlsx`/`.csv` tabular, `.html`, `.eml`) following the existing `TextExtractor` contract, register them in the dispatch table + upload allowlist + config defaults, and improve the OCR pipeline so language is configurable per source, scanned PDFs can be force-OCR'd, and OCR confidence is surfaced in block metadata. `.epub` (ebooklib) and `.msg` (extract-msg) are scoped out as follow-ups that reuse the exact same pattern (sketched in Notes).

**Architecture:** Each extractor is a self-contained module in `document_search/extractors/` implementing `TextExtractor.extract(file_path) -> ExtractionResult`. It returns a list of `ContentBlock`s (each `block_type`, `block_number`, `text`, `extractor`, `metadata`) plus `status` (`"ok"` / `"error"`) and optional `document_metadata`. Extractors are wired into the singleton `_EXTRACTORS` dict in `app.py` (and the parallel dict in `main.py` for the CLI), surfaced through `extractor_for(ext)`. The upload allowlist set and `AppConfig.supported_extensions` / `config.example.json` are extended in lock-step. OCR changes live in `document_search/services/ocr_service.py` (a thin, dependency-tolerant wrapper around Tesseract) and are threaded through the PDF/DOCX/PPTX extractors that already call it.

**Tech Stack:** Python 3.11, pytest. New third-party deps: `openpyxl` (xlsx), `beautifulsoup4` + `lxml` (html). `.csv` and `.eml` use the standard library (`csv`, `email`). OCR confidence uses the existing `pytesseract` via `image_to_data`.

**Scope boundaries:**

In scope:
- New extractors: `.xlsx`, `.csv`, `.html`, `.eml` — each its own module, each registered in `app.py` + `main.py`, each added to the upload allowlist and config defaults.
- Tests that build their fixtures in-memory / in `tmp_path` (an `.xlsx` via openpyxl, a `.csv` string, an `.html` string, an `.eml` string) — **no binary fixtures committed**.
- OCR: per-source language config plumbed through `ocr_service`, a `force_ocr` toggle for the PDF extractor, OCR confidence captured into block metadata.
- `requirements.txt` updated with rationale.

Out of scope (explicit follow-ups, same pattern):
- `.epub` via `ebooklib` and `.msg` via `extract-msg` — sketched in Notes; not implemented here to keep the dependency surface and review small.
- OCR-only image files (`.png`/`.jpg` as first-class indexable documents).
- A per-source-path config UI; per-source OCR language is read from config/env, not a new settings screen.
- Re-indexing/migration of already-indexed documents to pick up new block metadata.

---

## File Structure

**Create:**
- `document_search/extractors/xlsx_extractor.py` — `XlsxTextExtractor`.
- `document_search/extractors/csv_extractor.py` — `CsvTextExtractor`.
- `document_search/extractors/html_extractor.py` — `HtmlTextExtractor`.
- `document_search/extractors/eml_extractor.py` — `EmlTextExtractor`.
- `tests/test_xlsx_extractor.py`
- `tests/test_csv_extractor.py`
- `tests/test_html_extractor.py`
- `tests/test_eml_extractor.py`
- `tests/test_ocr_service.py`

**Modify:**
- `document_search/app.py` — import + register the 4 extractors in `_EXTRACTORS`; extend the upload allowlist set (`app.py:672`).
- `document_search/main.py` — register the 4 extractors in the CLI's `extractor_for` dict.
- `document_search/config.py` — extend `supported_extensions` default; extend `OcrConfig` with `force_ocr` and `languages` already present.
- `config.example.json` — add the 4 extensions to `supported_extensions`.
- `document_search/services/ocr_service.py` — add `force_ocr` support + OCR confidence (`ocr_image_with_confidence`, `ocr_pdf_file_with_confidence`); keep existing functions backward-compatible.
- `document_search/extractors/pdf_extractor.py` — wire `force_ocr` + confidence into `ocr_page` blocks.
- `requirements.txt` — add `openpyxl`, `beautifulsoup4`, `lxml`.

**Untouched contract (do not change):**
- `document_search/extractors/base.py` — the `TextExtractor` ABC stays as-is.
- `document_search/models.py` — `ContentBlock` / `ExtractionResult` shapes stay as-is.

---

## Key design decisions (locked)

- **Block-type vocabulary.** New extractors reuse existing `block_type` values where natural and introduce a small, predictable set otherwise:
  - xlsx/csv: `block_type="table"` per sheet/file, with `metadata={"sheet": <name>, "rows": <n>, "cols": <n>}`. Rows are rendered tab-separated, newline-joined (mirrors `DocxTextExtractor`'s table rendering so search/highlighting behaves identically).
  - html: `block_type="paragraph"` per visible text block; document `<title>` captured into `document_metadata`.
  - eml: a `block_type="header"` block (From/To/Subject/Date) + one `block_type="paragraph"` block for the body; attachments are **not** recursed (listed in metadata only).
- **`extractor` field** is always `self.__class__.__name__` (matches every existing extractor; the CI plan asserts this).
- **Errors are swallowed into `status="error"`** with `error_message=str(ex)` — never raised — exactly like `PdfTextExtractor`/`DocxTextExtractor`. Empty input yields `status="ok"` with zero blocks (matches `TxtTextExtractor`).
- **Optional deps degrade, never crash.** `openpyxl`/`bs4` are imported *inside* `extract()` (or guarded at module top with a clear `ExtractionResult(status="error", ...)` when missing), so a missing wheel produces a per-file error, not an import-time crash of the whole app. CSV and EML use stdlib and are always available.
- **OCR confidence** is the mean of per-word confidences Tesseract reports via `image_to_data` (values `0–100`; `-1` sentinels dropped). It is written to `metadata["ocr_confidence"]` (float, 1 decimal) on every OCR-produced block. Existing non-confidence helpers stay for backward-compat.
- **`force_ocr`** is read from `OcrConfig.force_ocr` (default `False`) and from `DOCUMENT_SEARCH_FORCE_OCR` env (truthy `"true"`). When set, the PDF extractor runs OCR even if `pypdf` extracted text, appending `ocr_page` blocks alongside the native `page` blocks (it does not discard native text). Per-source language is the existing `OcrConfig.languages` joined with `+` (e.g. `["deu","eng"]` -> `"deu+eng"`), with `DOCUMENT_SEARCH_OCR_LANG` env as an override.
- **Config plumbing is env-first** to match the *current* code: extractors already read `DOCUMENT_SEARCH_OCR_ENABLED` from the environment rather than receiving config objects. We keep that pattern (add `DOCUMENT_SEARCH_OCR_LANG` and `DOCUMENT_SEARCH_FORCE_OCR`) so no extractor signature changes, and document that the app should export these from `OcrConfig` at startup.

---

## Task 1: Add dependencies

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add the three new libs with rationale**

Append to `requirements.txt`:

```text
openpyxl>=3.1.0
beautifulsoup4>=4.12.0
lxml>=5.0.0
```

Rationale (record in the commit body, not the file):
- `openpyxl` — pure-Python `.xlsx` reader/writer; read-only mode (`load_workbook(read_only=True)`) streams cells without loading the whole sheet into memory. Also used by the **test** to build the fixture, so it must be a real dep, not test-only.
- `beautifulsoup4` — robust HTML text extraction that tolerates malformed markup; `get_text()` with separator handling gives clean paragraph splits.
- `lxml` — fast C-backed parser backend for BeautifulSoup (`BeautifulSoup(html, "lxml")`); falls back to stdlib `html.parser` if the wheel is unavailable, but we pin it for speed and correctness on real-world HTML.

`.csv` (stdlib `csv`) and `.eml` (stdlib `email`) need no new deps.

- [ ] **Step 2: Install and verify import**

```powershell
python -m pip install -r requirements.txt
python -c "import openpyxl, bs4, lxml; print('deps ok')"
```

Expected: `deps ok`.

- [ ] **Step 3: Commit**

```powershell
git add requirements.txt
git commit -m @'
feat(extractors): add openpyxl, beautifulsoup4, lxml deps

openpyxl for .xlsx tabular extraction (read-only streaming), beautifulsoup4
for resilient .html text extraction, lxml as the fast parser backend. csv and
eml extractors use the stdlib and need no new deps.
'@
```

---

## Task 2: `.csv` extractor

Start with CSV — it is stdlib-only and the simplest, establishing the table-block convention the xlsx extractor reuses.

**Files:**
- Create: `document_search/extractors/csv_extractor.py`
- Test: `tests/test_csv_extractor.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_csv_extractor.py`:

```python
from pathlib import Path

from document_search.extractors.csv_extractor import CsvTextExtractor
from document_search.models import ExtractionResult


def _write_csv(tmp_path: Path) -> Path:
    p = tmp_path / "sample.csv"
    p.write_text("name,city\nAlice,Bern\nBob,Zürich\n", encoding="utf-8")
    return p


def test_csv_extracts_single_table_block(tmp_path: Path):
    result = CsvTextExtractor().extract(_write_csv(tmp_path))
    assert isinstance(result, ExtractionResult)
    assert result.status == "ok"
    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.block_type == "table"
    assert block.block_number == 1
    assert block.extractor == "CsvTextExtractor"
    # tab-separated, newline-joined rendering
    assert "name\tcity" in block.text
    assert "Alice\tBern" in block.text
    assert "Bob\tZürich" in block.text
    assert block.metadata["rows"] == 3
    assert block.metadata["cols"] == 2


def test_csv_empty_file_yields_no_blocks(tmp_path: Path):
    p = tmp_path / "empty.csv"
    p.write_text("", encoding="utf-8")
    result = CsvTextExtractor().extract(p)
    assert result.status == "ok"
    assert result.blocks == []


def test_csv_semicolon_delimiter_is_sniffed(tmp_path: Path):
    p = tmp_path / "semi.csv"
    p.write_text("a;b;c\n1;2;3\n", encoding="utf-8")
    result = CsvTextExtractor().extract(p)
    assert result.status == "ok"
    assert result.blocks[0].text.startswith("a\tb\tc")
    assert result.blocks[0].metadata["cols"] == 3
```

Run it — it must fail with `ModuleNotFoundError` (module not yet created):

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_csv_extractor.py
```

- [ ] **Step 2: Implement the extractor**

Create `document_search/extractors/csv_extractor.py`:

```python
from __future__ import annotations

import csv
from pathlib import Path

from document_search.extractors.base import TextExtractor
from document_search.models import ContentBlock, ExtractionResult


class CsvTextExtractor(TextExtractor):
    def extract(self, file_path: Path) -> ExtractionResult:
        try:
            raw = file_path.read_text(encoding="utf-8", errors="ignore")
            if not raw.strip():
                return ExtractionResult(file_path=file_path, status="ok", blocks=[])
            # Sniff the delimiter; fall back to comma on failure.
            sample = raw[:4096]
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel
            rows = list(csv.reader(raw.splitlines(), dialect))
            rows = [r for r in rows if any(cell.strip() for cell in r)]
            if not rows:
                return ExtractionResult(file_path=file_path, status="ok", blocks=[])
            text = "\n".join("\t".join(cell.strip() for cell in row) for row in rows)
            cols = max((len(r) for r in rows), default=0)
            block = ContentBlock(
                "table", 1, text, self.__class__.__name__,
                {"rows": len(rows), "cols": cols},
            )
            return ExtractionResult(file_path=file_path, status="ok", blocks=[block])
        except Exception as ex:
            return ExtractionResult(file_path=file_path, status="error", error_message=str(ex))
```

- [ ] **Step 3: Make it green**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_csv_extractor.py
```

Expected: 3 passed.

- [ ] **Step 4: Commit**

```powershell
git add document_search/extractors/csv_extractor.py tests/test_csv_extractor.py
git commit -m "feat(extractors): add .csv extractor with delimiter sniffing"
```

---

## Task 3: `.xlsx` extractor

**Files:**
- Create: `document_search/extractors/xlsx_extractor.py`
- Test: `tests/test_xlsx_extractor.py`

- [ ] **Step 1: Write the failing test** (builds the `.xlsx` in-memory with openpyxl — no binary fixture committed)

Create `tests/test_xlsx_extractor.py`:

```python
from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl")

from document_search.extractors.xlsx_extractor import XlsxTextExtractor
from document_search.models import ExtractionResult


def _build_xlsx(tmp_path: Path) -> Path:
    from openpyxl import Workbook

    wb = Workbook()
    ws1 = wb.active
    ws1.title = "People"
    ws1.append(["name", "city"])
    ws1.append(["Alice", "Bern"])
    ws1.append(["Bob", "Zürich"])
    ws2 = wb.create_sheet("Numbers")
    ws2.append(["x", "y"])
    ws2.append([1, 2])
    path = tmp_path / "book.xlsx"
    wb.save(path)
    return path


def test_xlsx_one_table_block_per_sheet(tmp_path: Path):
    result = XlsxTextExtractor().extract(_build_xlsx(tmp_path))
    assert isinstance(result, ExtractionResult)
    assert result.status == "ok"
    assert len(result.blocks) == 2
    first = result.blocks[0]
    assert first.block_type == "table"
    assert first.extractor == "XlsxTextExtractor"
    assert first.metadata["sheet"] == "People"
    assert "name\tcity" in first.text
    assert "Alice\tBern" in first.text
    assert "Bob\tZürich" in first.text
    second = result.blocks[1]
    assert second.metadata["sheet"] == "Numbers"
    assert "x\ty" in second.text
    assert "1\t2" in second.text
    # sheet names surfaced in document metadata
    assert result.document_metadata["sheet_names"] == ["People", "Numbers"]


def test_xlsx_empty_sheet_is_skipped(tmp_path: Path):
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.title = "Empty"
    wb.create_sheet("Data").append(["only", "this"])
    path = tmp_path / "mixed.xlsx"
    wb.save(path)
    result = XlsxTextExtractor().extract(path)
    assert result.status == "ok"
    assert [b.metadata["sheet"] for b in result.blocks] == ["Data"]


def test_xlsx_corrupt_file_returns_error(tmp_path: Path):
    path = tmp_path / "broken.xlsx"
    path.write_bytes(b"not a real xlsx")
    result = XlsxTextExtractor().extract(path)
    assert result.status == "error"
    assert result.error_message
```

Run — must fail (`ModuleNotFoundError` on the extractor module):

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_xlsx_extractor.py
```

- [ ] **Step 2: Implement the extractor**

Create `document_search/extractors/xlsx_extractor.py`:

```python
from __future__ import annotations

from pathlib import Path

from document_search.extractors.base import TextExtractor
from document_search.models import ContentBlock, ExtractionResult


def _cell_to_str(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


class XlsxTextExtractor(TextExtractor):
    def extract(self, file_path: Path) -> ExtractionResult:
        try:
            from openpyxl import load_workbook
        except Exception as ex:  # pragma: no cover - missing optional dep
            return ExtractionResult(
                file_path=file_path, status="error",
                error_message=f"openpyxl not available: {ex}",
            )
        try:
            wb = load_workbook(filename=str(file_path), read_only=True, data_only=True)
        except Exception as ex:
            return ExtractionResult(file_path=file_path, status="error", error_message=str(ex))
        try:
            blocks: list[ContentBlock] = []
            idx = 1
            for ws in wb.worksheets:
                rows: list[str] = []
                max_cols = 0
                for row in ws.iter_rows(values_only=True):
                    cells = [_cell_to_str(c) for c in row]
                    if not any(cells):
                        continue
                    max_cols = max(max_cols, len(cells))
                    rows.append("\t".join(cells))
                if not rows:
                    continue
                blocks.append(ContentBlock(
                    "table", idx, "\n".join(rows), self.__class__.__name__,
                    {"sheet": ws.title, "rows": len(rows), "cols": max_cols},
                ))
                idx += 1
            meta = {"sheet_names": [ws.title for ws in wb.worksheets]}
            return ExtractionResult(
                file_path=file_path, status="ok",
                document_metadata=meta, blocks=blocks,
            )
        finally:
            wb.close()
```

- [ ] **Step 3: Make it green**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_xlsx_extractor.py
```

Expected: 3 passed.

- [ ] **Step 4: Commit**

```powershell
git add document_search/extractors/xlsx_extractor.py tests/test_xlsx_extractor.py
git commit -m "feat(extractors): add .xlsx extractor (one table block per sheet)"
```

---

## Task 4: `.html` extractor

**Files:**
- Create: `document_search/extractors/html_extractor.py`
- Test: `tests/test_html_extractor.py`

- [ ] **Step 1: Write the failing test** (feeds an HTML string in `tmp_path`)

Create `tests/test_html_extractor.py`:

```python
from pathlib import Path

import pytest

pytest.importorskip("bs4")

from document_search.extractors.html_extractor import HtmlTextExtractor
from document_search.models import ExtractionResult

_HTML = """<!doctype html>
<html>
  <head>
    <title>Quarterly Report</title>
    <style>.x { color: red; }</style>
  </head>
  <body>
    <h1>Revenue</h1>
    <p>Revenue grew in Bern.</p>
    <script>var ignored = 1;</script>
    <p>Costs fell in Zürich.</p>
  </body>
</html>
"""


def _write_html(tmp_path: Path) -> Path:
    p = tmp_path / "page.html"
    p.write_text(_HTML, encoding="utf-8")
    return p


def test_html_extracts_visible_text_blocks(tmp_path: Path):
    result = HtmlTextExtractor().extract(_write_html(tmp_path))
    assert isinstance(result, ExtractionResult)
    assert result.status == "ok"
    assert result.document_metadata["title"] == "Quarterly Report"
    joined = "\n".join(b.text for b in result.blocks)
    assert "Revenue grew in Bern." in joined
    assert "Costs fell in Zürich." in joined
    # script/style content must be stripped
    assert "var ignored" not in joined
    assert "color: red" not in joined
    for b in result.blocks:
        assert b.block_type == "paragraph"
        assert b.extractor == "HtmlTextExtractor"


def test_html_empty_body_yields_no_blocks(tmp_path: Path):
    p = tmp_path / "blank.html"
    p.write_text("<html><head><title>T</title></head><body></body></html>", encoding="utf-8")
    result = HtmlTextExtractor().extract(p)
    assert result.status == "ok"
    assert result.blocks == []
    assert result.document_metadata["title"] == "T"
```

Run — must fail:

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_html_extractor.py
```

- [ ] **Step 2: Implement the extractor**

Create `document_search/extractors/html_extractor.py`:

```python
from __future__ import annotations

from pathlib import Path

from document_search.extractors.base import TextExtractor
from document_search.models import ContentBlock, ExtractionResult


class HtmlTextExtractor(TextExtractor):
    def extract(self, file_path: Path) -> ExtractionResult:
        try:
            from bs4 import BeautifulSoup
        except Exception as ex:  # pragma: no cover - missing optional dep
            return ExtractionResult(
                file_path=file_path, status="error",
                error_message=f"beautifulsoup4 not available: {ex}",
            )
        try:
            raw = file_path.read_text(encoding="utf-8", errors="ignore")
            try:
                soup = BeautifulSoup(raw, "lxml")
            except Exception:
                soup = BeautifulSoup(raw, "html.parser")

            title_tag = soup.title
            title = title_tag.get_text(strip=True) if title_tag else None

            for tag in soup(["script", "style", "noscript", "template"]):
                tag.decompose()

            blocks: list[ContentBlock] = []
            idx = 1
            for line in soup.get_text(separator="\n").splitlines():
                txt = line.strip()
                if txt:
                    blocks.append(ContentBlock("paragraph", idx, txt, self.__class__.__name__, {}))
                    idx += 1

            meta = {"title": title}
            return ExtractionResult(
                file_path=file_path, status="ok",
                document_metadata=meta, blocks=blocks,
            )
        except Exception as ex:
            return ExtractionResult(file_path=file_path, status="error", error_message=str(ex))
```

- [ ] **Step 3: Make it green**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_html_extractor.py
```

Expected: 2 passed.

- [ ] **Step 4: Commit**

```powershell
git add document_search/extractors/html_extractor.py tests/test_html_extractor.py
git commit -m "feat(extractors): add .html extractor (bs4 visible-text blocks)"
```

---

## Task 5: `.eml` extractor

**Files:**
- Create: `document_search/extractors/eml_extractor.py`
- Test: `tests/test_eml_extractor.py`

- [ ] **Step 1: Write the failing test** (builds an `.eml` string with the stdlib `email` package)

Create `tests/test_eml_extractor.py`:

```python
from email.message import EmailMessage
from pathlib import Path

from document_search.extractors.eml_extractor import EmlTextExtractor
from document_search.models import ExtractionResult


def _write_eml(tmp_path: Path) -> Path:
    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    msg["Subject"] = "Quarterly numbers"
    msg["Date"] = "Mon, 01 Jun 2026 09:00:00 +0000"
    msg.set_content("Revenue grew in Bern.\nCosts fell in Zürich.")
    p = tmp_path / "mail.eml"
    p.write_bytes(msg.as_bytes())
    return p


def test_eml_extracts_header_and_body_blocks(tmp_path: Path):
    result = EmlTextExtractor().extract(_write_eml(tmp_path))
    assert isinstance(result, ExtractionResult)
    assert result.status == "ok"

    header_blocks = [b for b in result.blocks if b.block_type == "header"]
    body_blocks = [b for b in result.blocks if b.block_type == "paragraph"]
    assert len(header_blocks) == 1
    assert len(body_blocks) == 1

    header = header_blocks[0]
    assert header.extractor == "EmlTextExtractor"
    assert "alice@example.com" in header.text
    assert "bob@example.com" in header.text
    assert "Quarterly numbers" in header.text

    body = body_blocks[0]
    assert "Revenue grew in Bern." in body.text
    assert "Costs fell in Zürich." in body.text

    md = result.document_metadata
    assert md["subject"] == "Quarterly numbers"
    assert md["from"] == "alice@example.com"


def test_eml_html_only_body_is_stripped_to_text(tmp_path: Path):
    msg = EmailMessage()
    msg["From"] = "a@x.com"
    msg["To"] = "b@x.com"
    msg["Subject"] = "HTML mail"
    msg.set_content("plain fallback")
    msg.add_alternative("<html><body><p>Hello <b>Bern</b></p></body></html>", subtype="html")
    p = tmp_path / "html.eml"
    p.write_bytes(msg.as_bytes())
    result = EmlTextExtractor().extract(p)
    assert result.status == "ok"
    body = " ".join(b.text for b in result.blocks if b.block_type == "paragraph")
    # prefers the text/plain part
    assert "plain fallback" in body
```

Run — must fail:

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_eml_extractor.py
```

- [ ] **Step 2: Implement the extractor**

Create `document_search/extractors/eml_extractor.py`:

```python
from __future__ import annotations

from email import message_from_bytes
from email.message import Message
from email.policy import default as default_policy
from pathlib import Path

from document_search.extractors.base import TextExtractor
from document_search.models import ContentBlock, ExtractionResult

_HEADER_FIELDS = ("From", "To", "Cc", "Subject", "Date")


def _best_body(msg: Message) -> str:
    """Prefer text/plain; fall back to text/html stripped of tags."""
    plain: str | None = None
    html_body: str | None = None
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disp = (part.get("Content-Disposition") or "").lower()
        if "attachment" in disp:
            continue
        try:
            payload = part.get_content()
        except Exception:
            continue
        if not isinstance(payload, str):
            continue
        if ctype == "text/plain" and plain is None:
            plain = payload
        elif ctype == "text/html" and html_body is None:
            html_body = payload
    if plain is not None and plain.strip():
        return plain
    if html_body:
        try:
            from bs4 import BeautifulSoup

            return BeautifulSoup(html_body, "html.parser").get_text(separator="\n")
        except Exception:
            return html_body
    return ""


class EmlTextExtractor(TextExtractor):
    def extract(self, file_path: Path) -> ExtractionResult:
        try:
            raw = file_path.read_bytes()
            msg = message_from_bytes(raw, policy=default_policy)

            blocks: list[ContentBlock] = []
            header_lines = []
            for field in _HEADER_FIELDS:
                value = msg.get(field)
                if value:
                    header_lines.append(f"{field}: {value}")
            if header_lines:
                blocks.append(ContentBlock(
                    "header", 1, "\n".join(header_lines), self.__class__.__name__, {},
                ))

            body = _best_body(msg).strip()
            if body:
                blocks.append(ContentBlock(
                    "paragraph", len(blocks) + 1, body, self.__class__.__name__, {},
                ))

            attachments = [
                part.get_filename()
                for part in msg.walk()
                if (part.get("Content-Disposition") or "").lower().startswith("attachment")
                and part.get_filename()
            ]
            meta = {
                "from": msg.get("From"),
                "to": msg.get("To"),
                "subject": msg.get("Subject"),
                "date": msg.get("Date"),
                "attachments": attachments,
            }
            return ExtractionResult(
                file_path=file_path, status="ok",
                document_metadata=meta, blocks=blocks,
            )
        except Exception as ex:
            return ExtractionResult(file_path=file_path, status="error", error_message=str(ex))
```

- [ ] **Step 3: Make it green**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_eml_extractor.py
```

Expected: 2 passed.

- [ ] **Step 4: Commit**

```powershell
git add document_search/extractors/eml_extractor.py tests/test_eml_extractor.py
git commit -m "feat(extractors): add .eml extractor (headers + best-effort body)"
```

---

## Task 6: Register the four extractors (dispatch + allowlist + config)

**Files:**
- Modify: `document_search/app.py` (imports, `_EXTRACTORS`, upload allowlist `app.py:672`)
- Modify: `document_search/main.py` (CLI `extractor_for` dict)
- Modify: `document_search/config.py` (`supported_extensions` default)
- Modify: `config.example.json`

- [ ] **Step 1: Write the failing wiring test**

Create `tests/test_extractor_registration.py`:

```python
from document_search.app import extractor_for
from document_search.config import AppConfig
from document_search.extractors.csv_extractor import CsvTextExtractor
from document_search.extractors.eml_extractor import EmlTextExtractor
from document_search.extractors.html_extractor import HtmlTextExtractor
from document_search.extractors.xlsx_extractor import XlsxTextExtractor

EXPECTED = {
    ".xlsx": XlsxTextExtractor,
    ".csv": CsvTextExtractor,
    ".html": HtmlTextExtractor,
    ".eml": EmlTextExtractor,
}


def test_new_extensions_are_dispatched():
    for ext, cls in EXPECTED.items():
        extr = extractor_for(ext)
        assert extr is not None, f"{ext} not registered in app._EXTRACTORS"
        assert isinstance(extr, cls)


def test_new_extensions_in_default_supported_extensions():
    cfg = AppConfig()
    for ext in EXPECTED:
        assert ext in cfg.supported_extensions
```

Run — must fail (extensions not yet registered):

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_extractor_registration.py
```

- [ ] **Step 2: Register in `app.py`**

Add imports near the other extractor imports (after `document_search/app.py:42`):

```python
from document_search.extractors.csv_extractor import CsvTextExtractor
from document_search.extractors.eml_extractor import EmlTextExtractor
from document_search.extractors.html_extractor import HtmlTextExtractor
from document_search.extractors.xlsx_extractor import XlsxTextExtractor
```

Extend the `_EXTRACTORS` dict (`app.py:50`):

```python
_EXTRACTORS: dict[str, object] = {
    ".pdf":  PdfTextExtractor(),
    ".docx": DocxTextExtractor(),
    ".pptx": PptxTextExtractor(),
    ".txt":  TxtTextExtractor(),
    ".md":   MdTextExtractor(),
    ".doc":  LegacyOfficeTextExtractor(),
    ".ppt":  LegacyOfficeTextExtractor(),
    ".xlsx": XlsxTextExtractor(),
    ".csv":  CsvTextExtractor(),
    ".html": HtmlTextExtractor(),
    ".eml":  EmlTextExtractor(),
}
```

Extend the upload allowlist set at `app.py:672`:

```python
        allowed = {
            ".pdf", ".docx", ".pptx", ".txt", ".md", ".doc", ".ppt",
            ".xlsx", ".csv", ".html", ".eml",
        }
```

- [ ] **Step 3: Register in `main.py`** (CLI parity)

Add the same four imports near `main.py:15` and extend the dict inside `extractor_for` (`main.py:26`):

```python
from document_search.extractors.csv_extractor import CsvTextExtractor
from document_search.extractors.eml_extractor import EmlTextExtractor
from document_search.extractors.html_extractor import HtmlTextExtractor
from document_search.extractors.xlsx_extractor import XlsxTextExtractor
```

```python
        ".xlsx": XlsxTextExtractor(),
        ".csv": CsvTextExtractor(),
        ".html": HtmlTextExtractor(),
        ".eml": EmlTextExtractor(),
```

- [ ] **Step 4: Extend config defaults**

In `document_search/config.py`, update the `supported_extensions` default (`config.py:21`):

```python
    supported_extensions: list[str] = field(default_factory=lambda: [
        ".pdf", ".docx", ".pptx", ".txt", ".md", ".doc", ".ppt",
        ".xlsx", ".csv", ".html", ".eml",
    ])
```

In `config.example.json`, update the `supported_extensions` array to include `".xlsx", ".csv", ".html", ".eml"`.

- [ ] **Step 5: Make it green + full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_extractor_registration.py
$env:PYTHONPATH = "."; pytest -q
```

Expected: registration test passes; full suite green (no regressions).

- [ ] **Step 6: Commit**

```powershell
git add document_search/app.py document_search/main.py document_search/config.py config.example.json tests/test_extractor_registration.py
git commit -m "feat(extractors): register xlsx/csv/html/eml in dispatch, allowlist, config"
```

---

## Task 7: OCR pipeline — language config, confidence, force-OCR

**Files:**
- Modify: `document_search/services/ocr_service.py`
- Modify: `document_search/extractors/pdf_extractor.py`
- Test: `tests/test_ocr_service.py`

This task adds confidence-aware OCR helpers and threads a per-source language + force-OCR toggle through the PDF extractor. Tesseract/Pillow are optional; tests **monkeypatch** the OCR boundary so they run without a system Tesseract install (matching the CI plan's `subprocess`-monkeypatch convention for `legacy_office`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ocr_service.py`:

```python
import document_search.services.ocr_service as ocr


def test_ocr_lang_from_env_overrides_default(monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_OCR_LANG", "fra+ita")
    assert ocr.resolve_ocr_language("deu+eng") == "fra+ita"


def test_ocr_lang_default_used_when_no_env(monkeypatch):
    monkeypatch.delenv("DOCUMENT_SEARCH_OCR_LANG", raising=False)
    assert ocr.resolve_ocr_language("deu+eng") == "deu+eng"


def test_mean_confidence_drops_sentinels():
    # Tesseract reports -1 for non-text regions; they must be ignored.
    assert ocr._mean_confidence([95.0, -1, 85.0]) == 90.0
    assert ocr._mean_confidence([-1, -1]) is None
    assert ocr._mean_confidence([]) is None


def test_ocr_image_with_confidence_uses_image_to_data(monkeypatch):
    # Fake pytesseract.image_to_data returning two confident words.
    class FakePyt:
        @staticmethod
        def image_to_data(image, lang, output_type):
            return {"text": ["Hello", "Bern"], "conf": ["96", "88"]}

    class FakeImageMod:
        @staticmethod
        def open(_buf):
            return object()

    monkeypatch.setattr(ocr, "_load_ocr_dependencies", lambda: (FakePyt, FakeImageMod))
    text, conf = ocr.ocr_image_with_confidence(b"fakebytes", languages="deu+eng")
    assert text == "Hello Bern"
    assert conf == 92.0


def test_ocr_image_with_confidence_handles_missing_deps(monkeypatch):
    monkeypatch.setattr(ocr, "_load_ocr_dependencies", lambda: (None, None))
    text, conf = ocr.ocr_image_with_confidence(b"x")
    assert text == ""
    assert conf is None
```

Run — must fail (`resolve_ocr_language`, `_mean_confidence`, `ocr_image_with_confidence` don't exist):

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_ocr_service.py
```

- [ ] **Step 2: Extend `ocr_service.py`**

Add to `document_search/services/ocr_service.py` (keep the existing functions unchanged; add `os` import at the top):

```python
import os
```

```python
def resolve_ocr_language(default: str = "eng+deu") -> str:
    """Per-source language: DOCUMENT_SEARCH_OCR_LANG env overrides the passed default."""
    env = os.getenv("DOCUMENT_SEARCH_OCR_LANG", "").strip()
    return env or default


def force_ocr_enabled() -> bool:
    return os.getenv("DOCUMENT_SEARCH_FORCE_OCR", "false").lower() == "true"


def _mean_confidence(values: list[float]) -> float | None:
    kept = [v for v in values if v is not None and v >= 0]
    if not kept:
        return None
    return round(sum(kept) / len(kept), 1)


def ocr_image_with_confidence(blob: bytes, languages: str = "eng+deu") -> tuple[str, float | None]:
    """OCR an image, returning (text, mean_word_confidence_or_None)."""
    pytesseract, image_mod = _load_ocr_dependencies()
    if not pytesseract or not image_mod:
        return "", None
    try:
        import io

        image = image_mod.open(io.BytesIO(blob))
        data = pytesseract.image_to_data(
            image, lang=languages, output_type="dict",
        )
        words = [w for w in data.get("text", []) if w and w.strip()]
        confs: list[float] = []
        for raw_text, raw_conf in zip(data.get("text", []), data.get("conf", [])):
            if not raw_text or not raw_text.strip():
                continue
            try:
                confs.append(float(raw_conf))
            except (TypeError, ValueError):
                continue
        return " ".join(words).strip(), _mean_confidence(confs)
    except Exception:
        return "", None


def ocr_pdf_file_with_confidence(
    path: Path, languages: str = "eng+deu",
) -> list[tuple[str, float | None]]:
    """Like ocr_pdf_file but each page is (text, confidence)."""
    try:
        from pdf2image import convert_from_path
    except Exception:
        return []
    pytesseract, _ = _load_ocr_dependencies()
    if not pytesseract:
        return []
    results: list[tuple[str, float | None]] = []
    try:
        pages = convert_from_path(str(path))
    except Exception:
        return []
    for page in pages:
        try:
            data = pytesseract.image_to_data(page, lang=languages, output_type="dict")
            words = [w for w in data.get("text", []) if w and w.strip()]
            confs: list[float] = []
            for raw_text, raw_conf in zip(data.get("text", []), data.get("conf", [])):
                if not raw_text or not raw_text.strip():
                    continue
                try:
                    confs.append(float(raw_conf))
                except (TypeError, ValueError):
                    continue
            results.append((" ".join(words).strip(), _mean_confidence(confs)))
        except Exception:
            results.append(("", None))
    return results
```

Note on `output_type`: the test's fake `image_to_data` accepts `output_type` positionally/by keyword. With the real `pytesseract`, `output_type="dict"` is the correct sentinel; the fake ignores its value and returns a dict, so the test stays hermetic.

- [ ] **Step 3: Make `ocr_service` tests green**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_ocr_service.py
```

Expected: 5 passed.

- [ ] **Step 4: Write the failing PDF-extractor OCR test**

Append to a new `tests/test_pdf_extractor_ocr.py` (monkeypatch the OCR boundary so no real PDF rasterisation/Tesseract is needed):

```python
from pathlib import Path

import document_search.extractors.pdf_extractor as pdf_mod
from document_search.extractors.pdf_extractor import PdfTextExtractor


class _FakePage:
    def __init__(self, text):
        self._text = text

    def extract_text(self):
        return self._text


class _FakeReader:
    def __init__(self, *_args, **_kwargs):
        self.pages = [_FakePage("")]  # scanned: no embedded text
        self.metadata = {}


def test_force_ocr_appends_ocr_pages_with_confidence(tmp_path: Path, monkeypatch):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    monkeypatch.setattr(pdf_mod, "PdfReader", _FakeReader)
    monkeypatch.setenv("DOCUMENT_SEARCH_OCR_ENABLED", "true")
    monkeypatch.setenv("DOCUMENT_SEARCH_FORCE_OCR", "true")
    monkeypatch.setattr(
        pdf_mod, "ocr_pdf_file_with_confidence",
        lambda path, languages: [("Scanned text from Bern", 91.0)],
    )

    result = PdfTextExtractor().extract(pdf)
    assert result.status == "ok"
    ocr_blocks = [b for b in result.blocks if b.block_type == "ocr_page"]
    assert len(ocr_blocks) == 1
    block = ocr_blocks[0]
    assert "Scanned text from Bern" in block.text
    assert block.metadata["ocr_confidence"] == 91.0
    assert block.metadata["source"] == "ocr"


def test_no_ocr_when_disabled(tmp_path: Path, monkeypatch):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(pdf_mod, "PdfReader", _FakeReader)
    monkeypatch.delenv("DOCUMENT_SEARCH_OCR_ENABLED", raising=False)
    monkeypatch.delenv("DOCUMENT_SEARCH_FORCE_OCR", raising=False)
    result = PdfTextExtractor().extract(pdf)
    assert [b for b in result.blocks if b.block_type == "ocr_page"] == []
```

Run — must fail (extractor still imports/uses the old non-confidence helper):

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_pdf_extractor_ocr.py
```

- [ ] **Step 5: Update `pdf_extractor.py`**

Replace the OCR import and OCR branch. New import line:

```python
from document_search.services.ocr_service import (
    force_ocr_enabled,
    ocr_pdf_file_with_confidence,
    resolve_ocr_language,
)
```

Replace the OCR block (`pdf_extractor.py:25-28`) with:

```python
            ocr_enabled = os.getenv("DOCUMENT_SEARCH_OCR_ENABLED", "false").lower() == "true"
            likely_scanned = not blocks
            if ocr_enabled and (likely_scanned or force_ocr_enabled()):
                lang = resolve_ocr_language("eng+deu")
                base_no = len(blocks)
                for i, (ocr_text, conf) in enumerate(
                    ocr_pdf_file_with_confidence(file_path, languages=lang), start=1
                ):
                    if ocr_text:
                        meta = {"page": i, "source": "ocr"}
                        if conf is not None:
                            meta["ocr_confidence"] = conf
                        blocks.append(ContentBlock(
                            "ocr_page", base_no + i, ocr_text, self.__class__.__name__, meta,
                        ))
```

The `metadata` dict that follows already computes `likely_scanned`; leave it but reuse the local: change its line to `"likely_scanned": likely_scanned and len(reader.pages) > 0,` (preserves the original semantic — all pages empty — because `blocks` is empty exactly when every page was empty at the point `likely_scanned` is computed).

- [ ] **Step 6: Make it green + full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_pdf_extractor_ocr.py
$env:PYTHONPATH = "."; pytest -q
```

Expected: OCR tests pass; full suite green.

- [ ] **Step 7: Commit**

```powershell
git add document_search/services/ocr_service.py document_search/extractors/pdf_extractor.py tests/test_ocr_service.py tests/test_pdf_extractor_ocr.py
git commit -m @'
feat(extractors): configurable OCR language, force-OCR toggle, confidence metadata

ocr_service gains resolve_ocr_language (per-source via DOCUMENT_SEARCH_OCR_LANG),
force_ocr_enabled (DOCUMENT_SEARCH_FORCE_OCR), and *_with_confidence helpers that
surface mean Tesseract word confidence. The PDF extractor force-OCRs scanned PDFs
and writes ocr_confidence into ocr_page block metadata.
'@
```

---

## Task 8: Wire OCR config into app startup (env export)

**Files:**
- Modify: `document_search/app.py` (export `OcrConfig` to env at startup)
- Modify: `document_search/config.py` (`OcrConfig.force_ocr`)

The extractors read OCR settings from env (current pattern). This task makes the app translate the loaded `OcrConfig` into those env vars once at startup so configuration files actually take effect.

- [ ] **Step 1: Add `force_ocr` to `OcrConfig`**

In `document_search/config.py`:

```python
@dataclass(slots=True)
class OcrConfig:
    enabled: bool = False
    languages: list[str] = field(default_factory=lambda: ["deu", "eng"])
    force_ocr: bool = False
```

- [ ] **Step 2: Export at startup**

Locate the app startup where config is loaded (search for `load_config`/`load_effective_config` in `app.py`). Add, right after the effective config is available at process start:

```python
    _ocr_cfg = load_effective_config().ocr
    os.environ.setdefault("DOCUMENT_SEARCH_OCR_ENABLED", "true" if _ocr_cfg.enabled else "false")
    if _ocr_cfg.languages:
        os.environ.setdefault("DOCUMENT_SEARCH_OCR_LANG", "+".join(_ocr_cfg.languages))
    os.environ.setdefault("DOCUMENT_SEARCH_FORCE_OCR", "true" if _ocr_cfg.force_ocr else "false")
```

Use `setdefault` so an explicitly-exported env var (e.g. in Docker) still wins over the config file.

- [ ] **Step 3: Add a config round-trip test**

Append to `tests/test_ocr_service.py`:

```python
def test_ocr_config_parses_force_ocr(tmp_path):
    import json
    from document_search.config import load_config

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "ocr": {"enabled": True, "languages": ["fra"], "force_ocr": True}
    }), encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.ocr.enabled is True
    assert cfg.ocr.force_ocr is True
    assert cfg.ocr.languages == ["fra"]
```

- [ ] **Step 4: Run + commit**

```powershell
$env:PYTHONPATH = "."; pytest -q
git add document_search/config.py document_search/app.py tests/test_ocr_service.py
git commit -m "feat(extractors): export OcrConfig (lang/force) to env at app startup"
```

---

## Definition of Done

- [ ] `pytest -q` green; the new suites `test_csv_extractor`, `test_xlsx_extractor`, `test_html_extractor`, `test_eml_extractor`, `test_extractor_registration`, `test_ocr_service`, `test_pdf_extractor_ocr` all pass, and no existing test regressed.
- [ ] All four new extractors (`.xlsx`, `.csv`, `.html`, `.eml`) implement `TextExtractor`, return `ExtractionResult` with `status` in {`ok`,`error`}, set `extractor == self.__class__.__name__`, and never raise out of `extract()`.
- [ ] Each new extension is registered in `app._EXTRACTORS`, in `main.extractor_for`, in the upload allowlist set (`app.py`), in `AppConfig.supported_extensions`, and in `config.example.json`.
- [ ] Tests build their own fixtures (`.xlsx` via openpyxl, `.csv`/`.html`/`.eml` from strings/`EmailMessage`) — **no binary fixtures committed** to the repo.
- [ ] `requirements.txt` lists `openpyxl`, `beautifulsoup4`, `lxml`; `python -c "import openpyxl, bs4, lxml"` succeeds.
- [ ] OCR: language is resolvable per source (`DOCUMENT_SEARCH_OCR_LANG` overrides config default), `force_ocr` re-runs OCR on scanned PDFs even when native text exists, and OCR-produced blocks carry `metadata["ocr_confidence"]` (float) when Tesseract reports any.
- [ ] `OcrConfig.force_ocr` parses from JSON config; app startup exports `DOCUMENT_SEARCH_OCR_ENABLED/OCR_LANG/FORCE_OCR` via `setdefault` (explicit env still wins).
- [ ] Every task committed with a Conventional-Commit `feat(extractors): ...` message.

---

## Notes for the executing agent

- **Why env-first OCR config instead of passing config into extractors.** The existing extractors already read `DOCUMENT_SEARCH_OCR_ENABLED` from `os.getenv` — they receive no config object. Changing every extractor signature to accept config is a larger, riskier refactor than the roadmap item warrants. This plan keeps the env pattern and adds two more env vars, then makes the app *populate* them from `OcrConfig` at startup. A future plan can introduce a proper `OcrSettings` object threaded through `extractor_for`.
- **Why optional deps are imported inside `extract()`.** A missing `openpyxl`/`bs4` wheel must degrade to a per-file `status="error"`, not crash app import (which would take down search for *all* file types). CSV and EML are stdlib and always present. This mirrors `_load_ocr_dependencies()` already doing lazy, tolerant imports.
- **Why `read_only=True, data_only=True` for xlsx.** `read_only` streams rows without holding the workbook in memory (large spreadsheets), and `data_only` returns cached formula *values* rather than formula strings — search wants the computed value. Remember to `wb.close()` in a `finally` (done in the implementation) because read-only workbooks hold a file handle.
- **Why CSV sniffs the delimiter.** European exports frequently use `;` (because `,` is the decimal separator). `csv.Sniffer` handles `,`/`;`/`\t`/`|`; on failure we fall back to `excel` (comma). Rendering is always tab-separated so downstream FTS/highlighting is delimiter-agnostic and consistent with the docx table convention.
- **Why EML prefers text/plain.** HTML mail bodies are noisy; the plain alternative (when present) indexes cleanly. We only fall back to bs4-stripped HTML when there is no usable plain part. Attachments are listed in `document_metadata["attachments"]` but **not** recursed — recursing into attachment extractors is a separate, larger feature (loop/recursion-depth concerns).
- **OCR confidence sentinel handling.** Tesseract's `image_to_data` returns `conf == -1` for layout/non-text regions; including them would crater the mean. `_mean_confidence` drops negatives and returns `None` when nothing usable remains, so blocks from a blank scan simply omit the `ocr_confidence` key rather than reporting a misleading `0.0`.
- **`force_ocr` does not discard native text.** When forced, OCR pages are *appended* with `block_number` continuing after the native pages, so a PDF with a mix of text and scanned pages yields both. `likely_scanned` in `document_metadata` retains its original "every page empty" meaning.
- **Test hermeticity.** The OCR and PDF tests monkeypatch the Tesseract/`pdf2image`/`PdfReader` boundary (no system Tesseract, no Poppler, no real PDF needed) — the same pattern the CI-and-quality-gates plan uses for `legacy_office`'s `subprocess.run`. Do not add real Tesseract calls to the test suite.
- **Follow-ups (same pattern, deliberately deferred):**
  - **`.epub`** — add `ebooklib>=0.18` to requirements; `EpubTextExtractor` iterates `book.get_items_of_type(ITEM_DOCUMENT)`, feeds each item's HTML through the same bs4 text extraction as `HtmlTextExtractor` (factor a shared `_html_to_blocks` helper if you implement it), one `block_type="chapter"` per spine item, book title/author into `document_metadata`. Register in `app.py`/`main.py`/allowlist/config exactly like Task 6.
  - **`.msg`** — add `extract-msg>=0.48` to requirements; `MsgTextExtractor` mirrors `EmlTextExtractor`: header block (`msg.sender`, `msg.to`, `msg.subject`, `msg.date`) + body paragraph (`msg.body`), attachments listed in metadata. Wrap the `extract_msg.Message(...)` import in the lazy/tolerant pattern.
- **Do not edit `base.py` or `models.py`.** Every new behaviour fits the existing `TextExtractor`/`ContentBlock`/`ExtractionResult` contract. If you feel you need to change them, stop and re-read Task scopes.
- **Run the full `pytest -q` after Task 6 and again after Task 8** — registration touches `app.py` import order, and the OCR env export at startup can interact with other app-level tests; catch regressions early.
