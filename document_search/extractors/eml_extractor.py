"""Extractor for RFC-822 e-mail files (`.eml`).

Uses only the Python standard library (`email`): a header block (From/To/Cc/
Subject/Date) plus a body block. The body prefers the ``text/plain`` part and
falls back to a tag-stripped ``text/html`` part. Attachments are listed in
``document_metadata`` but never recursed into — that is a separate concern.
"""

from __future__ import annotations

from email import message_from_bytes
from email.message import Message
from email.policy import default as default_policy
from html.parser import HTMLParser
from pathlib import Path

from document_search.extractors.base import TextExtractor
from document_search.models import ContentBlock, ExtractionResult

_HEADER_FIELDS = ("From", "To", "Cc", "Subject", "Date")
_BLOCK_TAGS = {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}
_SKIP_TAGS = {"script", "style", "head", "title", "noscript"}


class _HtmlToText(HTMLParser):
    """Minimal, dependency-free HTML→text: keeps visible text, drops markup."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def _html_to_text(html: str) -> str:
    parser = _HtmlToText()
    try:
        parser.feed(html)
    except Exception:
        return html
    return parser.text()


def _best_body(msg: Message) -> str:
    """Prefer the text/plain part; fall back to a tag-stripped text/html part."""
    plain: str | None = None
    html_body: str | None = None
    for part in msg.walk():
        if part.is_multipart():
            continue
        disp = (part.get("Content-Disposition") or "").lower()
        if "attachment" in disp:
            continue
        ctype = part.get_content_type()
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
        return _html_to_text(html_body)
    return ""


def _header_str(msg: Message, field: str) -> str | None:
    value = msg.get(field)
    return str(value) if value is not None else None


class EmlTextExtractor(TextExtractor):
    def extract(self, file_path: Path) -> ExtractionResult:
        try:
            raw = file_path.read_bytes()
            msg = message_from_bytes(raw, policy=default_policy)

            blocks: list[ContentBlock] = []
            header_lines = [
                f"{field}: {value}"
                for field in _HEADER_FIELDS
                if (value := _header_str(msg, field))
            ]
            if header_lines:
                blocks.append(
                    ContentBlock("header", 1, "\n".join(header_lines), self.__class__.__name__, {})
                )

            body = _best_body(msg).strip()
            if body:
                blocks.append(
                    ContentBlock("paragraph", len(blocks) + 1, body, self.__class__.__name__, {})
                )

            attachments = [
                part.get_filename()
                for part in msg.walk()
                if (part.get("Content-Disposition") or "").lower().startswith("attachment")
                and part.get_filename()
            ]
            meta = {
                "from": _header_str(msg, "From"),
                "to": _header_str(msg, "To"),
                "subject": _header_str(msg, "Subject"),
                "date": _header_str(msg, "Date"),
                "attachments": attachments,
            }
            return ExtractionResult(
                file_path=file_path, status="ok", document_metadata=meta, blocks=blocks
            )
        except Exception as ex:
            return ExtractionResult(file_path=file_path, status="error", error_message=str(ex))
