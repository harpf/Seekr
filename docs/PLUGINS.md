# Writing Seekr Extractor Plugins

Seekr can index new file types through third-party **extractor plugins** — no
fork required. A plugin provides a `TextExtractor` subclass and declares which
file suffixes it handles. Seekr discovers it at startup and routes matching
files through it automatically (web indexing, the `index_paths` job, and the
CLI `index` command all resolve extractors through the same registry).

> **Security warning:** plugin code runs **in-process with full trust** — the
> same privileges as Seekr itself. An entry-point plugin runs at `ep.load()`; a
> drop-in file runs when it is imported at startup. There is **no** sandbox.
> Only install or drop in extractors you trust.

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

Subclass `TextExtractor`, declare the suffixes you handle in a `suffixes`
attribute, and implement `extract`:

```python
from pathlib import Path

from document_search.extractors import (
    ContentBlock,
    ExtractionResult,
    TextExtractor,
)


class MyExtractor(TextExtractor):
    suffixes = (".myext",)                # suffixes this plugin claims

    def extract(self, file_path: Path) -> ExtractionResult:
        text = file_path.read_text(encoding="utf-8", errors="ignore")
        block = ContentBlock(
            block_type="myext",           # free-form label
            block_number=1,               # 1-based ordering
            text=text,                    # the searchable text
            extractor=type(self).__name__,
            metadata={},                  # optional per-block dict
        )
        return ExtractionResult(file_path=file_path, status="ok", blocks=[block])
```

On failure, **return** `ExtractionResult(file_path=..., status="error",
error_message="...")` instead of raising — Seekr records the error and moves on.

Suffixes are normalised to lower-case and forced to start with a dot, so
`"csv"`, `".CSV"`, and `".csv"` all mean `.csv`.

## Registration channel A — installed package (entry point)

If you ship your extractor as a pip-installable package, declare an entry point
in the `document_search.extractors` group:

```toml
# pyproject.toml of your plugin package
[project]
name = "seekr-csv-extractor"
version = "0.1.0"
dependencies = []           # plus anything your extractor imports

[project.entry-points."document_search.extractors"]
csv = "seekr_csv_extractor.extractor:CsvExtractor"
```

The entry-point value may point at either:

- a `TextExtractor` subclass carrying a `suffixes` attribute (the registry
  instantiates it), **or**
- a module exposing a `register(register_extractor)` hook:

```python
def register(register_extractor):
    register_extractor(".csv", CsvExtractor())
    # register_extractor(suffix, extractor, *, override=False)
```

Install it into the same environment as Seekr (`pip install seekr-csv-extractor`).
Seekr enumerates `entry_points(group="document_search.extractors")` at startup.

## Registration channel B — drop-in directory

For local / quick plugins, drop a `.py` file into
`document_search/extractors/plugins/`. Files whose names start with `_` are
ignored.

```
document_search/extractors/plugins/
  __init__.py
  csv_extractor_example.py     # <- auto-imported at startup
```

A drop-in module registers its extractor(s) through a module-level
`register(register_extractor)` hook:

```python
class CsvExtractor(TextExtractor):
    suffixes = (".csv",)
    def extract(self, file_path): ...

def register(register_extractor):
    register_extractor(".csv", CsvExtractor())
```

See the shipped `document_search/extractors/plugins/csv_extractor_example.py`
for a complete, working `.csv` example.

## Conflicts and precedence

- **Built-in suffixes win.** A plugin that claims a built-in suffix
  (`.pdf .docx .pptx .txt .md .doc .ppt`) is **rejected** — `register_extractor`
  raises `ValueError` unless you pass `override=True`. During discovery that
  rejection is caught, logged at WARNING, and the plugin is skipped, so a buggy
  plugin cannot silently hijack a core format.
- **Two plugins, same new suffix:** the last one registered wins.

## Error isolation

A plugin that fails to import, load, or register is **logged at WARNING and
skipped** — it never crashes Seekr's startup, and other plugins still load. Run
Seekr with logging at INFO/WARNING to see which plugins were skipped and why.

## Verifying your plugin

```python
from document_search.extractors import (
    load_plugins,
    supported_extensions,
    extractor_for,
)

load_plugins(force=True)
print(".myext" in supported_extensions())   # True if your suffix registered
print(extractor_for(".myext"))              # your extractor instance
```
