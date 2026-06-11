from pathlib import Path

from document_search.services.scan_inbox_config import ScanInbox
from document_search.services.scan_watcher import (
    ScanWatcherManager,
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


def test_scan_once_missing_inbox_returns_zero(tmp_path):
    ib = ScanInbox(id="x", label="X", inbox_path=str(tmp_path / "nonexistent"),
                   target_root=str(tmp_path / "out"), stability_seconds=300)
    assert scan_once(ib, data_dir=tmp_path / "data", enqueue=lambda *a: None) == 0


def test_manager_reconfigure_tracks_enabled_inboxes(tmp_path):
    enq = []
    mgr = ScanWatcherManager(
        data_dir=tmp_path / "data",
        enqueue=lambda i, s, o: enq.append((i, s, o)),
    )
    ib_on = ScanInbox(id="on", label="On", inbox_path=str(tmp_path),
                      target_root=str(tmp_path / "t1"), enabled=True)
    ib_off = ScanInbox(id="off", label="Off", inbox_path=str(tmp_path),
                       target_root=str(tmp_path / "t2"), enabled=False)
    mgr.reconfigure([ib_on, ib_off])
    assert mgr.active_inbox_ids() == {"on"}
    mgr.reconfigure([])
    assert mgr.active_inbox_ids() == set()
    mgr.stop_all()


def test_manager_recover_reenqueues_orphan_staging_files(tmp_path):
    data = tmp_path / "data"
    staging = staging_dir_for(data, "b")
    staging.mkdir(parents=True)
    orphan = staging / "left.pdf"
    orphan.write_bytes(b"x")
    enq = []
    mgr = ScanWatcherManager(data_dir=data, enqueue=lambda i, s, o: enq.append((i, s, o)))
    ib = ScanInbox(id="b", label="B", inbox_path=str(tmp_path), target_root=str(tmp_path / "t"))
    mgr.recover_orphans([ib], known_staging_paths=set())
    assert enq == [("b", str(orphan), "left.pdf")]


def test_manager_recover_skips_files_with_existing_rows(tmp_path):
    data = tmp_path / "data"
    staging = staging_dir_for(data, "b")
    staging.mkdir(parents=True)
    tracked = staging / "tracked.pdf"
    tracked.write_bytes(b"x")
    enq = []
    mgr = ScanWatcherManager(data_dir=data, enqueue=lambda i, s, o: enq.append((i, s, o)))
    ib = ScanInbox(id="b", label="B", inbox_path=str(tmp_path), target_root=str(tmp_path / "t"))
    mgr.recover_orphans([ib], known_staging_paths={str(tracked)})
    assert enq == []
