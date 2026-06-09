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
    assert "alice@example.com" in md["from"]


def test_eml_html_only_body_is_stripped_to_text(tmp_path: Path):
    msg = EmailMessage()
    msg["From"] = "a@x.com"
    msg["To"] = "b@x.com"
    msg["Subject"] = "HTML mail"
    # HTML-only body (no text/plain alternative): tags must be stripped, text kept.
    msg.set_content(
        "<html><body><p>Hello <b>Bern</b></p>"
        "<script>var ignored = 1;</script><p>and Zürich</p></body></html>",
        subtype="html",
    )
    p = tmp_path / "html.eml"
    p.write_bytes(msg.as_bytes())
    result = EmlTextExtractor().extract(p)
    assert result.status == "ok"
    body = " ".join(b.text for b in result.blocks if b.block_type == "paragraph")
    assert "Hello" in body and "Bern" in body and "Zürich" in body
    # Script content and raw tags must not leak into the indexed text.
    assert "var ignored" not in body
    assert "<b>" not in body and "<script>" not in body


def test_eml_empty_message_is_ok_without_crashing(tmp_path: Path):
    p = tmp_path / "empty.eml"
    p.write_bytes(b"")
    result = EmlTextExtractor().extract(p)
    assert result.status == "ok"
    assert result.blocks == []


def test_eml_lists_attachments_in_metadata_without_recursing(tmp_path: Path):
    msg = EmailMessage()
    msg["From"] = "a@x.com"
    msg["To"] = "b@x.com"
    msg["Subject"] = "With attachment"
    msg.set_content("See attached.")
    msg.add_attachment(
        b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="report.pdf"
    )
    p = tmp_path / "att.eml"
    p.write_bytes(msg.as_bytes())
    result = EmlTextExtractor().extract(p)
    assert result.status == "ok"
    assert result.document_metadata["attachments"] == ["report.pdf"]
    # The attachment body is not recursed into its own blocks.
    body = " ".join(b.text for b in result.blocks if b.block_type == "paragraph")
    assert "See attached." in body
    assert "%PDF" not in body
