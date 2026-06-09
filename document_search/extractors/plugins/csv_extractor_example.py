"""Example Seekr extractor plugin: index ``.csv`` files as searchable text.

This is a *drop-in* plugin: it lives under ``document_search/extractors/plugins/``
and is auto-discovered at startup. To ship the same extractor as an installable
package instead, declare an entry point in the ``document_search.extractors``
group::

    [project.entry-points."document_search.extractors"]
    csv = "your_package.csv_extractor:CsvExtractor"

A drop-in module registers its extractor(s) through the module-level
``register(register_extractor)`` hook at the bottom of this file. See
``docs/PLUGINS.md`` for the full guide.
"""

from __future__ import annotations

import csv
from pathlib import Path

# Import the public, versioned contract — never a private internal.
from document_search.extractors import (
    EXTRACTOR_API_VERSION,
    ContentBlock,
    ExtractionResult,
    TextExtractor,
)

assert EXTRACTOR_API_VERSION >= 1, "This plugin requires Seekr extractor API v1+"


class CsvExtractor(TextExtractor):
    """Flatten a CSV into one searchable text block per row (header-prefixed)."""

    suffixes = (".csv",)

    def extract(self, file_path: Path) -> ExtractionResult:
        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            return ExtractionResult(
                file_path=file_path, status="error", error_message=str(exc)
            )

        rows = list(csv.reader(text.splitlines()))
        if not rows:
            return ExtractionResult(file_path=file_path, status="ok", blocks=[])

        header = rows[0]
        name = type(self).__name__
        # Block 1: the header line, useful for matching column names.
        blocks: list[ContentBlock] = [
            ContentBlock(
                block_type="csv_header",
                block_number=1,
                text=", ".join(header),
                extractor=name,
            )
        ]
        # One block per data row, rendered as "col: value" pairs so search hits
        # read naturally and carry their column context.
        for i, row in enumerate(rows[1:], start=2):
            pairs = []
            for col_idx, value in enumerate(row):
                col = header[col_idx] if col_idx < len(header) else f"col{col_idx + 1}"
                pairs.append(f"{col}: {value}")
            line = " | ".join(pairs)
            if line.strip():
                blocks.append(
                    ContentBlock(
                        block_type="csv_row",
                        block_number=i,
                        text=line,
                        extractor=name,
                        metadata={"row": i},
                    )
                )

        return ExtractionResult(
            file_path=file_path,
            status="ok",
            document_metadata={"row_count": max(len(rows) - 1, 0), "columns": header},
            blocks=blocks,
        )


def register(register_extractor) -> None:
    """Drop-in registration hook called once by the plugin loader at startup."""
    register_extractor(".csv", CsvExtractor())
