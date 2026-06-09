"""The OCR config block must survive a UI config save and be applied to the env
the extractors read (so it is steuerbar over the UI, not silently dropped)."""

import os

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from document_search.app import create_app  # noqa: E402

_OCR_ENV = ("DOCUMENT_SEARCH_OCR_ENABLED", "DOCUMENT_SEARCH_OCR_LANG", "DOCUMENT_SEARCH_FORCE_OCR")


def _admin(client):
    tok = client.post("/api/login", json={"username": "admin", "password": "admin"}).json()["token"]
    return {"X-Auth-Token": tok}


def test_config_save_preserves_and_applies_ocr(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_CONFIG_PATH", str(tmp_path / "config.json"))
    # Pin the OCR env vars so monkeypatch reverts whatever the handler writes
    # (the handler sets os.environ directly) — no cross-test leakage.
    for key in _OCR_ENV:
        monkeypatch.setenv(key, "false")

    with TestClient(create_app(str(tmp_path / "t.db"))) as client:
        headers = _admin(client)
        cfg = client.get("/api/config", headers=headers).json()
        assert "ocr" in cfg  # GET exposes the block

        cfg["ocr"] = {"enabled": True, "languages": ["deu"], "force_ocr": True}
        r = client.post("/api/config", json=cfg, headers=headers)
        assert r.status_code == 200, r.text

        # Preserved across a fresh GET (not dropped on save).
        got = client.get("/api/config", headers=headers).json()
        assert got["ocr"]["enabled"] is True
        assert got["ocr"]["force_ocr"] is True
        assert got["ocr"]["languages"] == ["deu"]

        # Applied to the env the extractors read.
        assert os.environ["DOCUMENT_SEARCH_OCR_ENABLED"] == "true"
        assert os.environ["DOCUMENT_SEARCH_OCR_LANG"] == "deu"
        assert os.environ["DOCUMENT_SEARCH_FORCE_OCR"] == "true"


def test_config_save_without_ocr_still_works(tmp_path, monkeypatch):
    """Older clients that omit the ocr block must still save (default applies)."""
    monkeypatch.setenv("DOCUMENT_SEARCH_CONFIG_PATH", str(tmp_path / "config.json"))
    for key in _OCR_ENV:
        monkeypatch.setenv(key, "false")
    with TestClient(create_app(str(tmp_path / "t.db"))) as client:
        headers = _admin(client)
        payload = {
            "database_path": "x.db",
            "supported_extensions": [".pdf"],
            "exclude_dirs": [],
            "exclude_patterns": [],
            "max_file_size_mb": 100,
        }
        r = client.post("/api/config", json=payload, headers=headers)
        assert r.status_code == 200, r.text
        got = client.get("/api/config", headers=headers).json()
        assert got["ocr"]["enabled"] is False
