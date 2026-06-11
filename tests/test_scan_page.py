import pytest
from fastapi.testclient import TestClient

from document_search.app import create_app

pytest.importorskip("fastapi")


def test_scan_page_renders(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_CONFIG_PATH", str(tmp_path / "config.json"))
    app = create_app(str(tmp_path / "document_index.db"))
    client = TestClient(app)
    r = client.get("/scan")
    assert r.status_code == 200
    assert "Scan-Posteingang" in r.text
    assert 'id="scanReviewList"' in r.text
