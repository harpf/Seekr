from pathlib import Path

from document_search.services.scan_inbox_config import ScanInbox
from document_search.services.scan_watcher import (
    is_stable,
    scan_once,
    staging_dir_for,
)


def _inbox(tmp_path) -> ScanInbox:
    inbox = tmp_path / "in"
    target = tmp_path / "out"
    inbox.mkdir()
    target.mkdir()
    return ScanInbox(id="b", label="B", inbox_path=str(inbox), target_root=str(target),
                     stability_seconds=300)


def test_is_stable_uses_mtime_age(tmp_path):
    f = tmp_path / "a.pdf"
    f.write_text("x")
    assert not is_stable(f, stability_seconds=300, now=f.stat().st_mtime + 10)
    assert is_stable(f, stability_seconds=300, now=f.stat().st_mtime + 301)


def test_scan_once_moves_stable_file_and_enqueues(tmp_path):
    ib = _inbox(tmp_path)
    src = Path(ib.inbox_path) / "scan001.pdf"
    src.write_bytes(b"%PDF fake")

    enqueued = []

    def fake_enqueue(inbox_id, staging_path, original_filename):
        enqueued.append((inbox_id, staging_path, original_filename))

    moved = scan_once(ib, data_dir=tmp_path / "data", enqueue=fake_enqueue,
                      now=src.stat().st_mtime + 400)

    assert moved == 1
    assert not src.exists()
    staged = list((staging_dir_for(tmp_path / "data", ib.id)).glob("*.pdf"))
    assert len(staged) == 1
    assert enqueued and enqueued[0][0] == "b"
    assert enqueued[0][2] == "scan001.pdf"


def test_scan_once_skips_unstable_file(tmp_path):
    ib = _inbox(tmp_path)
    src = Path(ib.inbox_path) / "growing.pdf"
    src.write_bytes(b"partial")
    moved = scan_once(ib, data_dir=tmp_path / "data", enqueue=lambda *a: None,
                      now=src.stat().st_mtime + 10)
    assert moved == 0
    assert src.exists()


def test_scan_once_dedups_same_name_collision(tmp_path):
    ib = _inbox(tmp_path)
    staging = staging_dir_for(tmp_path / "data", ib.id)
    staging.mkdir(parents=True)
    (staging / "dup.pdf").write_bytes(b"old")
    src = Path(ib.inbox_path) / "dup.pdf"
    src.write_bytes(b"new")
    scan_once(ib, data_dir=tmp_path / "data", enqueue=lambda *a: None,
              now=src.stat().st_mtime + 400)
    assert len(list(staging.glob("dup*.pdf"))) == 2


def test_scan_once_ignores_unsupported_extension(tmp_path):
    ib = _inbox(tmp_path)
    src = Path(ib.inbox_path) / "notes.txt"
    src.write_bytes(b"hello")
    moved = scan_once(ib, data_dir=tmp_path / "data", enqueue=lambda *a: None,
                      now=src.stat().st_mtime + 400)
    assert moved == 0
    assert src.exists()
