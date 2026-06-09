"""Extractor for Outlook `.msg` files (compound OLE format).

`.msg` is a binary OLE container that the standard library cannot parse, so this
extractor depends on the third-party ``extract-msg`` package. The import is lazy
and tolerant: a missing wheel degrades to a per-file ``status="error"`` rather
than crashing app import (mirrors the optional-dependency pattern used by the
OCR service). Output mirrors :class:`EmlTextExtractor`: a header block plus a
body block, with attachments listed in ``document_metadata`` (never recursed).
"""

from __future__ import annotations

from pathlib import Path

from document_search.extractors.base import TextExtractor
from document_search.models import ContentBlock, ExtractionResult

_HEADER_FIELDS = (
    ("From", "sender"),
    ("To", "to"),
    ("Cc", "cc"),
    ("Subject", "subject"),
    ("Date", "date"),
)


def _load_extract_msg():
    """Return the ``extract_msg`` module, or ``None`` if it is not installed."""
    try:
        import extract_msg
    except Exception:
        return None
    return extract_msg


def _attr_str(msg: object, attr: str) -> str | None:
    value = getattr(msg, attr, None)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class MsgTextExtractor(TextExtractor):
    def extract(self, file_path: Path) -> ExtractionResult:
        module = _load_extract_msg()
        if module is None:
            return ExtractionResult(
                file_path=file_path,
                status="error",
                error_message="extract-msg is not installed; cannot read .msg files",
            )
        try:
            msg = module.Message(str(file_path))
        except Exception as ex:
            return ExtractionResult(file_path=file_path, status="error", error_message=str(ex))
        try:
            blocks: list[ContentBlock] = []
            header_lines = [
                f"{label}: {value}"
                for label, attr in _HEADER_FIELDS
                if (value := _attr_str(msg, attr))
            ]
            if header_lines:
                blocks.append(
                    ContentBlock("header", 1, "\n".join(header_lines), self.__class__.__name__, {})
                )

            body = (getattr(msg, "body", None) or "").strip()
            if body:
                blocks.append(
                    ContentBlock("paragraph", len(blocks) + 1, body, self.__class__.__name__, {})
                )

            attachments: list[str] = []
            for att in getattr(msg, "attachments", None) or []:
                name = getattr(att, "longFilename", None) or getattr(att, "shortFilename", None)
                if name:
                    attachments.append(str(name))
            meta = {
                "from": _attr_str(msg, "sender"),
                "to": _attr_str(msg, "to"),
                "subject": _attr_str(msg, "subject"),
                "date": _attr_str(msg, "date"),
                "attachments": attachments,
            }
            return ExtractionResult(
                file_path=file_path, status="ok", document_metadata=meta, blocks=blocks
            )
        except Exception as ex:
            return ExtractionResult(file_path=file_path, status="error", error_message=str(ex))
        finally:
            try:
                msg.close()
            except Exception:
                pass
