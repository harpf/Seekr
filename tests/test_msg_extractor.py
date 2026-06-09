"""Tests for the Outlook `.msg` extractor.

`.msg` is a binary OLE format and the ``extract-msg`` library is read-only, so
(matching this repo's legacy-office / OCR convention) we never touch a real
``.msg`` file or require the dependency at test time. Instead we monkeypatch the
``_load_extract_msg`` boundary with a fake module exposing a ``Message`` class.
"""

from pathlib import Path

import document_search.extractors.msg_extractor as msg_mod
from document_search.extractors.msg_extractor import MsgTextExtractor
from document_search.models import ExtractionResult


class _FakeAttachment:
    def __init__(self, name):
        self.longFilename = name
        self.shortFilename = name


class _FakeMessage:
    def __init__(self, path, *, sender, to, subject, date, body, attachments=()):
        self._path = path
        self.sender = sender
        self.to = to
        self.cc = None
        self.subject = subject
        self.date = date
        self.body = body
        self.attachments = [_FakeAttachment(a) for a in attachments]
        self.closed = False

    def close(self):
        self.closed = True


def _fake_module(**kwargs):
    captured = {}

    class _Module:
        @staticmethod
        def Message(path):
            m = _FakeMessage(path, **kwargs)
            captured["msg"] = m
            return m

    return _Module, captured


def test_msg_extracts_header_and_body(monkeypatch, tmp_path: Path):
    mod, captured = _fake_module(
        sender="alice@example.com",
        to="bob@example.com",
        subject="Quarterly numbers",
        date="Mon, 01 Jun 2026 09:00:00 +0000",
        body="Revenue grew in Bern.\nCosts fell in Zürich.",
        attachments=["report.pdf"],
    )
    monkeypatch.setattr(msg_mod, "_load_extract_msg", lambda: mod)

    p = tmp_path / "mail.msg"
    p.write_bytes(b"\xd0\xcf\x11\xe0fake-ole")  # OLE magic; content irrelevant (mocked)
    result = MsgTextExtractor().extract(p)

    assert isinstance(result, ExtractionResult)
    assert result.status == "ok"

    header = next(b for b in result.blocks if b.block_type == "header")
    body = next(b for b in result.blocks if b.block_type == "paragraph")
    assert header.extractor == "MsgTextExtractor"
    assert "alice@example.com" in header.text
    assert "Quarterly numbers" in header.text
    assert "Revenue grew in Bern." in body.text

    md = result.document_metadata
    assert md["subject"] == "Quarterly numbers"
    assert md["attachments"] == ["report.pdf"]
    # All metadata must be JSON-serialisable (the store persists it as JSON).
    import json
    json.dumps(md)
    # The message handle is closed after extraction.
    assert captured["msg"].closed is True


def test_msg_missing_dependency_is_error(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(msg_mod, "_load_extract_msg", lambda: None)
    p = tmp_path / "x.msg"
    p.write_bytes(b"\xd0\xcf\x11\xe0")
    result = MsgTextExtractor().extract(p)
    assert result.status == "error"
    assert "extract-msg" in (result.error_message or "")


def test_msg_corrupt_file_returns_error(monkeypatch, tmp_path: Path):
    class _Boom:
        @staticmethod
        def Message(path):
            raise ValueError("not a valid msg")

    monkeypatch.setattr(msg_mod, "_load_extract_msg", lambda: _Boom)
    p = tmp_path / "broken.msg"
    p.write_bytes(b"garbage")
    result = MsgTextExtractor().extract(p)
    assert result.status == "error"
    assert "not a valid msg" in (result.error_message or "")
