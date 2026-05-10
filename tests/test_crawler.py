from pathlib import Path

from document_search.config import AppConfig
from document_search.crawler import iter_documents


def test_iter_documents_filters(tmp_path: Path):
    (tmp_path / "ok.txt").write_text("x", encoding="utf-8")
    (tmp_path / "~$temp.docx").write_text("x", encoding="utf-8")
    cfg = AppConfig(supported_extensions=[".txt", ".docx"])
    files = list(iter_documents([tmp_path], cfg))
    assert any(f.name == "ok.txt" for f in files)
    assert all(not f.name.startswith("~$") for f in files)
