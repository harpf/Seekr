from pathlib import Path

from document_search.services.hash_service import sha256_file


def test_sha256_file(tmp_path: Path):
    p = tmp_path / "a.txt"
    p.write_text("hello", encoding="utf-8")
    assert sha256_file(p)
