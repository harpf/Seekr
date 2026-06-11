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
    monkeypatch.setattr(se, "ocr_pdf_pages", lambda p, pages, languages="eng+deu": {})
    result = extract_for_scan(pdf, languages="deu+eng")
    assert result.blocks[0].text == "echter Text"
    assert result.blocks[0].block_type == "page"


def test_unsupported_extension_returns_error(tmp_path):
    f = tmp_path / "scan.xyz"
    f.write_bytes(b"x")
    result = extract_for_scan(f, languages="deu+eng")
    assert result.status == "error"
    assert "Unsupported" in (result.error_message or "")
