import pytest

from document_search.services.scan_inbox_config import (
    ScanInbox,
    ScanInboxConfigError,
    parse_scan_inboxes,
    slugify_id,
    validate_inbox_paths,
)


def test_slugify_id_is_stable_and_safe():
    assert slugify_id("Scan-Buchhaltung 2024!") == "scan-buchhaltung-2024"
    assert slugify_id("   ") == ""


def test_parse_fills_defaults_and_derives_id():
    raw = [{"label": "Scan HR", "inbox_path": "/in/hr", "target_root": "/docs/HR"}]
    inboxes = parse_scan_inboxes(raw)
    assert len(inboxes) == 1
    ib = inboxes[0]
    assert ib.id == "scan-hr"
    assert ib.stability_seconds == 300
    assert ib.poll_interval_seconds == 60
    assert ib.enabled is True
    assert ib.reviewers_groups == [] and ib.reviewers_users == []


def test_parse_preserves_explicit_id_and_reviewers():
    raw = [{
        "id": "fixed", "label": "X", "inbox_path": "/in", "target_root": "/out",
        "reviewers": {"groups": ["accounting"], "users": ["m.muster"]},
        "stability_seconds": 30, "poll_interval_seconds": 10, "enabled": False,
    }]
    ib = parse_scan_inboxes(raw)[0]
    assert ib.id == "fixed"
    assert ib.reviewers_groups == ["accounting"]
    assert ib.reviewers_users == ["m.muster"]
    assert ib.enabled is False


def test_duplicate_ids_rejected():
    raw = [
        {"label": "A", "inbox_path": "/a", "target_root": "/x"},
        {"label": "A", "inbox_path": "/b", "target_root": "/y"},
    ]
    with pytest.raises(ScanInboxConfigError, match="duplicate"):
        parse_scan_inboxes(raw)


def test_stability_below_minimum_rejected():
    raw = [{"label": "A", "inbox_path": "/a", "target_root": "/x", "stability_seconds": 5}]
    with pytest.raises(ScanInboxConfigError, match="stability_seconds"):
        parse_scan_inboxes(raw)


def test_validate_inbox_paths_rejects_inbox_inside_target(tmp_path):
    target = tmp_path / "docs"
    inbox = target / "incoming"
    inbox.mkdir(parents=True)
    ib = ScanInbox(id="x", label="X", inbox_path=str(inbox), target_root=str(target))
    with pytest.raises(ScanInboxConfigError, match="inside"):
        validate_inbox_paths(ib)


def test_validate_inbox_paths_rejects_missing(tmp_path):
    ib = ScanInbox(id="x", label="X", inbox_path=str(tmp_path / "nope"),
                   target_root=str(tmp_path))
    with pytest.raises(ScanInboxConfigError, match="does not exist"):
        validate_inbox_paths(ib)


import json

from document_search.config import load_config


def test_load_config_reads_scan_inboxes(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({
        "scan_inboxes": [
            {"id": "b", "label": "B", "inbox_path": "/in", "target_root": "/out"}
        ]
    }), encoding="utf-8")
    cfg = load_config(cfg_file)
    assert isinstance(cfg.scan_inboxes, list)
    assert cfg.scan_inboxes[0]["id"] == "b"


def test_load_config_defaults_scan_inboxes_empty(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text("{}", encoding="utf-8")
    assert load_config(cfg_file).scan_inboxes == []
