import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from document_search.app import create_app  # noqa: E402


def test_config_page_has_scan_inbox_section(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_CONFIG_PATH", str(tmp_path / "config.json"))
    app = create_app(str(tmp_path / "document_index.db"))
    client = TestClient(app)
    r = client.get("/config")
    assert r.status_code == 200
    assert 'id="scanInboxConfig"' in r.text
    assert "Scan-Eingänge" in r.text
