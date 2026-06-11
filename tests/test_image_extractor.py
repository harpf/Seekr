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
