# Backup, Restore, Export/Import & Update-Rollback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make backup/restore a first-class, WAL-safe feature and give the in-app update a rollback path. Today the *only* backup is a side-effect of `scripts/update.sh` (a raw `cp` of the SQLite file before `git pull`) — which is unsafe under WAL — and there is no restore, no scheduled backup, no data export/import, and no rollback if a rebuild/startup fails. This plan adds: (1) a WAL-safe `BackupService` using sqlite3 `Connection.backup()` plus admin `POST /api/backup/run` and `GET /api/backups`, with optional scheduled backups reusing the persistent job queue worker; (2) a restore endpoint that takes a *pre-restore* safety backup, swaps the DB file, and signals that a process restart is required; (3) an ACL-aware data export/import to a portable zip (documents + tags + ACLs as JSON); (4) update-rollback in `update.sh` that records the previous git commit + image, runs a `/ready` health-check, and auto-reverts on failure.

**Architecture:** All backup logic lives in one new service module `document_search/services/backup_service.py`. It is the only place that calls `sqlite3.Connection.backup()` and the only place that writes/reads the backup directory. The web layer (`app.py`) exposes thin admin endpoints that delegate to it. Scheduled backups reuse the existing `Worker`/`JobStore` from the job-queue plan via a new `backup` job kind and a tiny scheduler tick. Export/import is a separate concern in the same module (it walks the DB and emits/ingests a zip). Update rollback is pure bash in `scripts/update.sh`, tested by mocking `subprocess`/`git` through a thin Python wrapper-free shell test harness.

**Tech Stack:** Python 3.11, SQLite (WAL; `sqlite3.Connection.backup()` is in the stdlib), `zipfile`/`json` from stdlib, pytest. No new third-party dependencies. Bash for `update.sh` (runs in the Linux container; tests stub `git`/`docker`/`curl`).

**Scope boundaries:**

In scope:
- `BackupService` with `create_backup()` (WAL-safe online backup), `list_backups()`, `restore_backup()` (with mandatory pre-restore safety backup), `export_archive()`, `import_archive()` (ACL-aware).
- Admin endpoints: `POST /api/backup/run`, `GET /api/backups`, `POST /api/backup/restore`, `GET /api/backup/export`, `POST /api/backup/import`.
- A `backup` job kind + a scheduler that enqueues a backup every `DOCUMENT_SEARCH_BACKUP_INTERVAL_HOURS` (0 = disabled).
- Retention: keep the newest N backups (`DOCUMENT_SEARCH_BACKUP_KEEP`, default 14).
- `update.sh` rollback: record prev commit + image tag, post-update `/ready` health-check, auto-revert (`git checkout` prev + rebuild) on failure.

Out of scope (deferred):
- A `/ready` *implementation* — this plan **consumes** `/ready` (added by the observability plan). A minimal fallback `/ready` is added here only if it does not already exist (Task 7 Step 0 checks).
- Cross-database (Postgres) backups, S3/remote backup targets, encryption-at-rest of backups.
- Incremental/differential backups (only full snapshots).
- A backup/restore UI (endpoints only; a dashboard is a later plan).
- Restoring *into a live process* (we copy the file and require a restart — documented explicitly).

---

## File Structure

**Create:**
- `document_search/services/backup_service.py` — backup/restore/export/import logic.
- `tests/test_backup_service.py` — unit tests (backup is a valid openable SQLite file; export/import round-trips; restore takes a safety backup; retention prune).
- `tests/test_app_backup.py` — integration tests for the five endpoints + scheduled-backup job.
- `tests/test_update_rollback.py` — shell-level tests for `update.sh` rollback with `git`/`docker`/`curl` stubbed on `PATH`.

**Modify:**
- `document_search/app.py` — instantiate `BackupService`; register the `backup` job handler + scheduler tick at startup; add the five endpoints; ensure a `/ready` endpoint exists (fallback only).
- `scripts/update.sh` — replace the unsafe `cp` backup with a call into the Python backup service, capture prev commit/image, add post-update health-check + auto-revert.

**Untouched:**
- The job-queue internals (`job_store.py`, `job_worker.py`) — reused as-is.
- ACL service (`acl_service.py`) — export/import reads/writes the ACL tables directly; it does not change the filter logic.

---

## Key design decisions (locked)

- **WAL-safe backups use `sqlite3.Connection.backup()`, never `cp`.** Under WAL, the `-wal` sidecar holds uncommitted-to-main pages; a raw file copy of just `document_index.db` can produce a torn/stale snapshot. `Connection.backup()` performs a consistent online copy across the live connection, including WAL content, into a fresh single-file DB. (Ref: Python docs `sqlite3.Connection.backup`.)
- **Backups are timestamped single files** named `document_index_YYYYMMDD_HHMMSS.db` in `DOCUMENT_SEARCH_BACKUP_DIR` (default: sibling `backups/` next to the DB). This matches the existing `update.sh` naming so old backups remain discoverable.
- **Restore requires a process restart.** We never mutate the live connection's file out from under it. `restore_backup()` (a) takes a pre-restore safety backup of the *current* DB, (b) copies the chosen backup over `DOCUMENT_SEARCH_DB` (and removes stale `-wal`/`-shm` sidecars), and (c) returns a flag telling the caller a restart is required. The endpoint returns `restart_required: true`; the operator (or the container orchestrator) restarts. We do **not** attempt a hot reload of the SQLite store — that is unsafe with open per-thread connections.
- **Export is a zip with a manifest + JSON tables**, not a binary DB copy, so it is portable across schema-compatible versions and human-inspectable. It includes `documents`, `content_blocks`, `user_tags`, `document_tags`, `principals`, `user_groups`, `document_acl`. It does **not** include `users` password hashes or `content_fts` (FTS is rebuilt on import from `content_blocks`).
- **Import is additive + idempotent on `documents.path`** (the UNIQUE key). Re-importing the same archive does not duplicate rows. ACL rows are matched by `(document_id, principal_id, permission)`; principals by `(type, external_id)`.
- **Scheduled backups reuse the queue.** A `backup` job kind runs `create_backup()` then prunes retention. A scheduler tick (called from the worker poll loop wrapper, or a lightweight timer) enqueues one when the interval has elapsed. Default interval `0` = disabled (opt-in), so existing deployments are unchanged.
- **Update rollback is bash-only** and self-contained in `update.sh`, so it works even if the new image fails to start Python. It records `PREV_COMMIT` and the running image id *before* pulling/rebuilding, health-checks `/ready` after `up -d`, and on failure does `git checkout PREV_COMMIT` + rebuild + `up -d` again.

---

## Task 1: `BackupService.create_backup()` — WAL-safe online backup

**Files:**
- Create: `document_search/services/backup_service.py`
- Test: `tests/test_backup_service.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_backup_service.py`:

```python
import sqlite3
from pathlib import Path

import pytest

from document_search.index.sqlite_store import SqliteStore
from document_search.services.backup_service import BackupService


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "document_index.db")


@pytest.fixture
def svc(tmp_path, store):
    backup_dir = tmp_path / "backups"
    return BackupService(store, backup_dir=backup_dir, keep=14)


def _seed_one_document(store: SqliteStore) -> None:
    from datetime import UTC, datetime
    now = datetime.now(tz=UTC).isoformat()
    store.conn.execute(
        "INSERT INTO documents(path, filename, extension, file_size, modified_at, "
        "sha256, indexed_at, status) VALUES(?,?,?,?,?,?,?,?)",
        ("/d/a.pdf", "a.pdf", ".pdf", 10, now, "h1", now, "ok"),
    )
    store.conn.commit()


def test_create_backup_writes_timestamped_file(svc):
    info = svc.create_backup()
    p = Path(info["path"])
    assert p.exists()
    assert p.name.startswith("document_index_")
    assert p.suffix == ".db"
    assert info["size_bytes"] > 0


def test_backup_is_a_valid_openable_sqlite_db(svc, store):
    _seed_one_document(store)
    info = svc.create_backup()
    # The crux: the backup must open as SQLite AND contain the committed data.
    conn = sqlite3.connect(info["path"])
    try:
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        assert ok == "ok"
        count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        assert count == 1
        # Schema check: a representative ACL table came across too
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"documents", "principals", "document_acl"}.issubset(tables)
    finally:
        conn.close()


def test_backup_captures_data_written_through_wal(svc, store):
    # Write a row but DO NOT checkpoint — it lives in the -wal file.
    # Connection.backup() must still see it (a raw cp of the .db would not).
    from datetime import UTC, datetime
    now = datetime.now(tz=UTC).isoformat()
    store.conn.execute(
        "INSERT INTO documents(path, filename, extension, file_size, modified_at, "
        "sha256, indexed_at, status) VALUES(?,?,?,?,?,?,?,?)",
        ("/d/wal.pdf", "wal.pdf", ".pdf", 10, now, "h2", now, "ok"),
    )
    store.conn.commit()  # commit goes to WAL under journal_mode=WAL
    info = svc.create_backup()
    conn = sqlite3.connect(info["path"])
    try:
        row = conn.execute(
            "SELECT 1 FROM documents WHERE path='/d/wal.pdf'"
        ).fetchone()
        assert row is not None
    finally:
        conn.close()
```

- [ ] **Step 2: Run, expect ImportError**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_backup_service.py -v
```

Expected: ImportError — `backup_service` does not exist yet.

- [ ] **Step 3: Implement `create_backup()`**

Create `document_search/services/backup_service.py`:

```python
"""Backup, restore, and portable export/import for the Seekr SQLite store.

This module is the ONLY place that:
  * calls `sqlite3.Connection.backup()` (the WAL-safe online backup primitive), and
  * reads/writes the backup directory.

WAL note: the database runs in `journal_mode=WAL`. A plain file copy of
`document_index.db` can capture a torn or stale snapshot because committed
pages may still live in the `-wal` sidecar. `Connection.backup()` performs a
consistent page-by-page online copy through the live connection, so the
resulting file is a complete, single-file, integrity-clean database.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from document_search.index.sqlite_store import SqliteStore

_BACKUP_PREFIX = "document_index_"
_BACKUP_SUFFIX = ".db"
_TS_FMT = "%Y%m%d_%H%M%S"


class BackupService:
    def __init__(
        self,
        store: SqliteStore,
        backup_dir: Path | str | None = None,
        keep: int = 14,
    ) -> None:
        self.store = store
        self.db_path = Path(store.db_path)
        if backup_dir is None:
            backup_dir = os.getenv(
                "DOCUMENT_SEARCH_BACKUP_DIR",
                str(self.db_path.parent / "backups"),
            )
        self.backup_dir = Path(backup_dir)
        self.keep = keep

    # ── helpers ────────────────────────────────────────────────────────
    @staticmethod
    def _timestamp() -> str:
        return datetime.now(tz=UTC).strftime(_TS_FMT)

    def _new_backup_path(self, ts: str | None = None) -> Path:
        ts = ts or self._timestamp()
        return self.backup_dir / f"{_BACKUP_PREFIX}{ts}{_BACKUP_SUFFIX}"

    # ── create ─────────────────────────────────────────────────────────
    def create_backup(self) -> dict[str, Any]:
        """Write a WAL-safe online backup to a timestamped file.

        Returns a dict: {"path", "filename", "size_bytes", "created_at"}.
        """
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        dest = self._new_backup_path()
        # If two backups land in the same second, disambiguate.
        n = 1
        while dest.exists():
            dest = self.backup_dir / f"{_BACKUP_PREFIX}{self._timestamp()}_{n}{_BACKUP_SUFFIX}"
            n += 1

        dest_conn = sqlite3.connect(dest)
        try:
            # backup() reads from the live source connection (incl. WAL pages)
            # and writes a consistent snapshot into dest_conn.
            self.store.conn.backup(dest_conn)
        finally:
            dest_conn.close()

        size = dest.stat().st_size
        return {
            "path": str(dest),
            "filename": dest.name,
            "size_bytes": size,
            "created_at": datetime.now(tz=UTC).isoformat(),
        }
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_backup_service.py -v
```

Expected: 3 passing.

- [ ] **Step 5: Full-suite check**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: baseline + 3 new, zero failures.

- [ ] **Step 6: Commit**

```powershell
git add document_search/services/backup_service.py tests/test_backup_service.py
git commit -m "feat(backup): WAL-safe online backup via sqlite3 Connection.backup()"
```

---

## Task 2: `list_backups()` + retention prune

**Files:**
- Modify: `document_search/services/backup_service.py`
- Test: `tests/test_backup_service.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_backup_service.py`:

```python
def test_list_backups_returns_newest_first(svc):
    a = svc.create_backup()
    b = svc.create_backup()
    rows = svc.list_backups()
    names = [r["filename"] for r in rows]
    assert a["filename"] in names and b["filename"] in names
    # Sorted by filename descending == newest timestamp first.
    assert names == sorted(names, reverse=True)
    for r in rows:
        assert "filename" in r and "size_bytes" in r and "created_at" in r


def test_list_backups_ignores_non_backup_files(svc):
    svc.backup_dir.mkdir(parents=True, exist_ok=True)
    (svc.backup_dir / "notes.txt").write_text("hi", encoding="utf-8")
    (svc.backup_dir / "random.db").write_text("x", encoding="utf-8")
    svc.create_backup()
    names = [r["filename"] for r in svc.list_backups()]
    assert "notes.txt" not in names
    assert "random.db" not in names  # wrong prefix


def test_prune_keeps_only_newest_n(tmp_path, store):
    svc = BackupService(store, backup_dir=tmp_path / "backups", keep=2)
    import time
    created = []
    for _ in range(4):
        created.append(svc.create_backup()["filename"])
        time.sleep(1.05)  # ensure distinct second-resolution timestamps
    remaining = {r["filename"] for r in svc.list_backups()}
    assert len(remaining) == 2
    # The two newest survive.
    assert set(sorted(created)[-2:]) == remaining
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_backup_service.py -v
```

Expected: `test_list_backups_*` and `test_prune_*` fail (methods missing).

- [ ] **Step 3: Implement `list_backups()` and prune; call prune from `create_backup()`**

In `document_search/services/backup_service.py`, add these methods to the class:

```python
    # ── list / prune ───────────────────────────────────────────────────
    def list_backups(self) -> list[dict[str, Any]]:
        if not self.backup_dir.exists():
            return []
        out: list[dict[str, Any]] = []
        for p in self.backup_dir.iterdir():
            if not p.is_file():
                continue
            if not (p.name.startswith(_BACKUP_PREFIX) and p.name.endswith(_BACKUP_SUFFIX)):
                continue
            st = p.stat()
            out.append(
                {
                    "filename": p.name,
                    "path": str(p),
                    "size_bytes": st.st_size,
                    "created_at": datetime.fromtimestamp(st.st_mtime, tz=UTC).isoformat(),
                }
            )
        # Filename embeds the timestamp, so a reverse string sort is newest-first.
        out.sort(key=lambda r: r["filename"], reverse=True)
        return out

    def prune(self) -> list[str]:
        """Delete all but the newest `self.keep` backups. Returns deleted names."""
        if self.keep <= 0:
            return []
        backups = self.list_backups()
        deleted: list[str] = []
        for r in backups[self.keep :]:
            try:
                Path(r["path"]).unlink()
                deleted.append(r["filename"])
            except OSError:
                pass
        return deleted
```

Then make `create_backup()` prune after writing. Change the `return` block of `create_backup()` to:

```python
        size = dest.stat().st_size
        self.prune()
        return {
            "path": str(dest),
            "filename": dest.name,
            "size_bytes": size,
            "created_at": datetime.now(tz=UTC).isoformat(),
        }
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_backup_service.py -v
```

Expected: 6 passing.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

- [ ] **Step 6: Commit**

```powershell
git add document_search/services/backup_service.py tests/test_backup_service.py
git commit -m "feat(backup): list backups and retention prune (keep newest N)"
```

---

## Task 3: `restore_backup()` — safety backup + file swap + restart flag

**Files:**
- Modify: `document_search/services/backup_service.py`
- Test: `tests/test_backup_service.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_backup_service.py`:

```python
def test_restore_takes_pre_restore_safety_backup(svc, store):
    _seed_one_document(store)
    snapshot = svc.create_backup()  # backup containing 1 doc

    # Mutate the live DB so we can prove restore reverts it.
    store.conn.execute("DELETE FROM documents")
    store.conn.commit()

    before = {r["filename"] for r in svc.list_backups()}
    result = svc.restore_backup(snapshot["filename"])
    after = {r["filename"] for r in svc.list_backups()}

    # A new "pre-restore" safety backup must have been created.
    new_files = after - before
    assert len(new_files) >= 1, "restore must take a pre-restore safety backup"
    assert result["restart_required"] is True
    assert result["restored_from"] == snapshot["filename"]


def test_restore_swaps_db_file_contents(tmp_path):
    # Use a fresh store we fully control so we can re-open after the swap.
    db = tmp_path / "document_index.db"
    s = SqliteStore(db)
    _seed_one_document(s)
    svc = BackupService(s, backup_dir=tmp_path / "backups", keep=14)
    snap = svc.create_backup()

    # Delete everything, then restore.
    s.conn.execute("DELETE FROM documents")
    s.conn.commit()
    s.conn.close()  # simulate the restart: close the live connection first

    svc.restore_backup(snap["filename"])

    # Re-open: the restored file must contain the original row again.
    s2 = SqliteStore(db)
    count = s2.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    assert count == 1


def test_restore_unknown_backup_raises(svc):
    with pytest.raises(FileNotFoundError):
        svc.restore_backup("document_index_19990101_000000.db")


def test_restore_rejects_path_traversal(svc):
    with pytest.raises(ValueError):
        svc.restore_backup("../../etc/passwd")
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_backup_service.py -v
```

Expected: the four restore tests fail (`restore_backup` missing).

- [ ] **Step 3: Implement `restore_backup()`**

Add to the `BackupService` class in `document_search/services/backup_service.py`:

```python
    # ── restore ────────────────────────────────────────────────────────
    def _resolve_backup(self, filename: str) -> Path:
        """Resolve a backup filename safely (no path traversal)."""
        if "/" in filename or "\\" in filename or filename in ("", ".", ".."):
            raise ValueError("Invalid backup filename")
        if not (filename.startswith(_BACKUP_PREFIX) and filename.endswith(_BACKUP_SUFFIX)):
            raise ValueError("Not a backup file")
        candidate = (self.backup_dir / filename).resolve()
        if candidate.parent != self.backup_dir.resolve():
            raise ValueError("Backup escapes backup directory")
        if not candidate.exists():
            raise FileNotFoundError(f"Backup not found: {filename}")
        return candidate

    def restore_backup(self, filename: str) -> dict[str, Any]:
        """Restore the chosen backup over the live DB file.

        Safety contract:
          1. Take a *pre-restore* safety backup of the CURRENT database first.
          2. Validate the chosen backup opens cleanly (integrity_check).
          3. Copy it over `db_path`, removing stale -wal/-shm sidecars.
          4. Return {"restart_required": True}. The caller MUST restart the
             process; we never hot-swap the file under an open connection.
        """
        source = self._resolve_backup(filename)

        # 1. Safety backup of the current state (best-effort; uses the online API).
        try:
            safety = self.create_backup()
        except Exception:
            safety = None

        # 2. Validate the chosen backup is a clean SQLite database.
        check_conn = sqlite3.connect(source)
        try:
            ok = check_conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            check_conn.close()
        if ok != "ok":
            raise ValueError(f"Backup failed integrity_check: {ok}")

        # 3. Swap the file. Remove WAL/SHM sidecars so the restored DB is used.
        shutil.copyfile(source, self.db_path)
        for sidecar in (
            self.db_path.with_name(self.db_path.name + "-wal"),
            self.db_path.with_name(self.db_path.name + "-shm"),
        ):
            try:
                sidecar.unlink()
            except FileNotFoundError:
                pass

        return {
            "restart_required": True,
            "restored_from": source.name,
            "safety_backup": safety["filename"] if safety else None,
        }
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_backup_service.py -v
```

Expected: 10 passing.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

- [ ] **Step 6: Commit**

```powershell
git add document_search/services/backup_service.py tests/test_backup_service.py
git commit -m "feat(backup): restore with mandatory pre-restore safety backup + restart flag"
```

---

## Task 4: ACL-aware export/import to a portable zip

**Files:**
- Modify: `document_search/services/backup_service.py`
- Test: `tests/test_backup_service.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_backup_service.py`:

```python
def _seed_doc_with_tag_and_acl(store):
    """Create one user (alice), one doc, a tag, and a non-public ACL grant."""
    from datetime import UTC, datetime
    now = datetime.now(tz=UTC).isoformat()
    alice_id = store.create_user("alice", "pw-alice")
    cur = store.conn.execute(
        "INSERT INTO documents(path, filename, extension, file_size, modified_at, "
        "sha256, indexed_at, status) VALUES(?,?,?,?,?,?,?,?)",
        ("/d/report.pdf", "report.pdf", ".pdf", 20, now, "h-report", now, "ok"),
    )
    doc_id = cur.lastrowid
    store.conn.execute(
        "INSERT INTO content_blocks(document_id, block_type, block_number, text, "
        "extractor, text_length) VALUES(?,?,?,?,?,?)",
        (doc_id, "paragraph", 1, "quarterly numbers", "txt", 17),
    )
    tcur = store.conn.execute(
        "INSERT INTO user_tags(user_id, name) VALUES(?, 'finance')", (alice_id,)
    )
    store.conn.execute(
        "INSERT INTO document_tags(user_id, document_id, tag_id, created_at) "
        "VALUES(?,?,?,?)",
        (alice_id, doc_id, tcur.lastrowid, now),
    )
    alice_p = store.conn.execute(
        "SELECT principal_id FROM users WHERE id=?", (alice_id,)
    ).fetchone()["principal_id"]
    store.conn.execute(
        "INSERT INTO document_acl(document_id, principal_id, permission, granted_at) "
        "VALUES(?,?, 'read', ?)",
        (doc_id, alice_p, now),
    )
    store.conn.commit()
    return alice_id, doc_id


def test_export_creates_zip_with_manifest_and_tables(svc, store, tmp_path):
    _seed_doc_with_tag_and_acl(store)
    out = tmp_path / "export.zip"
    info = svc.export_archive(out)
    assert Path(info["path"]).exists()
    import zipfile, json
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        assert "manifest.json" in names
        for table in ("documents", "content_blocks", "user_tags",
                      "document_tags", "principals", "user_groups", "document_acl"):
            assert f"tables/{table}.json" in names
        manifest = json.loads(z.read("manifest.json"))
        assert manifest["format"] == "seekr-export"
        assert manifest["counts"]["documents"] == 1
        docs = json.loads(z.read("tables/documents.json"))
        assert docs[0]["path"] == "/d/report.pdf"


def test_import_round_trips_documents_tags_and_acls(tmp_path, store):
    src_alice, src_doc = _seed_doc_with_tag_and_acl(store)
    svc = BackupService(store, backup_dir=tmp_path / "b", keep=14)
    archive = tmp_path / "export.zip"
    svc.export_archive(archive)

    # Fresh, empty target DB.
    target = SqliteStore(tmp_path / "target.db")
    target.create_user("alice", "pw-alice")  # same principal external_id
    target_svc = BackupService(target, backup_dir=tmp_path / "tb", keep=14)
    result = target_svc.import_archive(archive)

    # Document came across.
    doc = target.conn.execute(
        "SELECT id FROM documents WHERE path='/d/report.pdf'"
    ).fetchone()
    assert doc is not None
    # Tag + tag link came across.
    tag = target.conn.execute(
        "SELECT id FROM user_tags WHERE name='finance'"
    ).fetchone()
    assert tag is not None
    link = target.conn.execute(
        "SELECT 1 FROM document_tags WHERE document_id=? AND tag_id=?",
        (doc["id"], tag["id"]),
    ).fetchone()
    assert link is not None
    # ACL grant came across, mapped to alice's principal in the TARGET db.
    alice_p = target.conn.execute(
        "SELECT principal_id FROM users WHERE username='alice'"
    ).fetchone()["principal_id"]
    acl = target.conn.execute(
        "SELECT permission FROM document_acl WHERE document_id=? AND principal_id=?",
        (doc["id"], alice_p),
    ).fetchone()
    assert acl is not None and acl["permission"] == "read"
    assert result["imported"]["documents"] == 1


def test_import_is_idempotent_on_path(tmp_path, store):
    _seed_doc_with_tag_and_acl(store)
    svc = BackupService(store, backup_dir=tmp_path / "b", keep=14)
    archive = tmp_path / "export.zip"
    svc.export_archive(archive)

    target = SqliteStore(tmp_path / "target.db")
    target.create_user("alice", "pw-alice")
    tsvc = BackupService(target, backup_dir=tmp_path / "tb", keep=14)
    tsvc.import_archive(archive)
    tsvc.import_archive(archive)  # second import must not duplicate

    count = target.conn.execute(
        "SELECT COUNT(*) FROM documents WHERE path='/d/report.pdf'"
    ).fetchone()[0]
    assert count == 1
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_backup_service.py -v
```

Expected: the three export/import tests fail (`export_archive`/`import_archive` missing).

- [ ] **Step 3: Implement `export_archive()` and `import_archive()`**

Add to the `BackupService` class in `document_search/services/backup_service.py`:

```python
    # ── export / import ────────────────────────────────────────────────
    _EXPORT_TABLES = (
        "documents",
        "content_blocks",
        "user_tags",
        "document_tags",
        "principals",
        "user_groups",
        "document_acl",
    )

    def _dump_table(self, table: str) -> list[dict[str, Any]]:
        rows = self.store.conn.execute(f"SELECT * FROM {table}").fetchall()
        return [dict(r) for r in rows]

    def export_archive(self, out_path: Path | str) -> dict[str, Any]:
        """Write a portable zip: manifest.json + tables/<name>.json.

        Excludes `users` (password hashes) and `content_fts` (rebuilt on import).
        """
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        counts: dict[str, int] = {}
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
            for table in self._EXPORT_TABLES:
                data = self._dump_table(table)
                counts[table] = len(data)
                z.writestr(f"tables/{table}.json", json.dumps(data, default=str))
            manifest = {
                "format": "seekr-export",
                "version": 1,
                "created_at": datetime.now(tz=UTC).isoformat(),
                "counts": counts,
            }
            z.writestr("manifest.json", json.dumps(manifest, indent=2))
        return {"path": str(out_path), "counts": counts}

    def import_archive(self, archive_path: Path | str) -> dict[str, Any]:
        """Import documents + tags + ACLs from a seekr-export zip.

        Strategy (additive, idempotent):
          * principals matched by (type, external_id); inserted if absent.
          * documents matched by `path` (UNIQUE); inserted if absent.
          * Foreign keys are remapped from source IDs to target IDs via lookup
            on the natural keys above, so IDs need not align across databases.
          * content_blocks, user_tags, document_tags, document_acl are linked
            through the remapped document/principal IDs.
        """
        archive_path = Path(archive_path)
        with zipfile.ZipFile(archive_path) as z:
            manifest = json.loads(z.read("manifest.json"))
            if manifest.get("format") != "seekr-export":
                raise ValueError("Not a seekr-export archive")
            tables = {
                t: json.loads(z.read(f"tables/{t}.json"))
                for t in self._EXPORT_TABLES
            }

        conn = self.store.conn
        now = datetime.now(tz=UTC).isoformat()
        imported = {t: 0 for t in self._EXPORT_TABLES}

        # 1. principals: (type, external_id) -> target principal id
        principal_map: dict[int, int] = {}
        for p in tables["principals"]:
            existing = conn.execute(
                "SELECT id FROM principals WHERE type=? AND external_id=?",
                (p["type"], p["external_id"]),
            ).fetchone()
            if existing:
                principal_map[p["id"]] = existing["id"]
            else:
                cur = conn.execute(
                    "INSERT INTO principals(type, external_id, display_name, created_at) "
                    "VALUES(?,?,?,?)",
                    (p["type"], p["external_id"], p.get("display_name"),
                     p.get("created_at") or now),
                )
                principal_map[p["id"]] = cur.lastrowid
                imported["principals"] += 1

        # 2. documents: path -> target document id
        doc_map: dict[int, int] = {}
        doc_cols = (
            "path", "filename", "extension", "mime_type", "file_size",
            "modified_at", "created_at", "sha256", "indexed_at", "status",
            "error_message", "page_count", "slide_count", "word_count",
            "metadata_json", "owner_principal_id",
        )
        for d in tables["documents"]:
            existing = conn.execute(
                "SELECT id FROM documents WHERE path=?", (d["path"],)
            ).fetchone()
            if existing:
                doc_map[d["id"]] = existing["id"]
                continue
            owner = d.get("owner_principal_id")
            mapped_owner = principal_map.get(owner) if owner is not None else None
            placeholders = ",".join("?" * len(doc_cols))
            values = [d.get(c) for c in doc_cols]
            values[doc_cols.index("owner_principal_id")] = mapped_owner
            cur = conn.execute(
                f"INSERT INTO documents({','.join(doc_cols)}) VALUES({placeholders})",
                values,
            )
            doc_map[d["id"]] = cur.lastrowid
            imported["documents"] += 1

        # 3. content_blocks (+ FTS) for newly-imported docs only
        for b in tables["content_blocks"]:
            tgt_doc = doc_map.get(b["document_id"])
            if tgt_doc is None:
                continue
            already = conn.execute(
                "SELECT 1 FROM content_blocks WHERE document_id=? AND block_number=? "
                "AND block_type=?",
                (tgt_doc, b["block_number"], b["block_type"]),
            ).fetchone()
            if already:
                continue
            bcur = conn.execute(
                "INSERT INTO content_blocks(document_id, block_type, block_number, "
                "text, extractor, text_length, metadata_json) VALUES(?,?,?,?,?,?,?)",
                (tgt_doc, b["block_type"], b["block_number"], b["text"],
                 b["extractor"], b["text_length"], b.get("metadata_json")),
            )
            drow = conn.execute(
                "SELECT path, filename, extension FROM documents WHERE id=?",
                (tgt_doc,),
            ).fetchone()
            conn.execute(
                "INSERT INTO content_fts(document_id, block_id, path, filename, "
                "extension, block_type, block_number, text) VALUES(?,?,?,?,?,?,?,?)",
                (tgt_doc, bcur.lastrowid, drow["path"], drow["filename"],
                 drow["extension"], b["block_type"], str(b["block_number"]), b["text"]),
            )
            imported["content_blocks"] += 1

        # 4. user_tags: (user_id, name) — user_id is NOT remapped (users excluded).
        #    We match the importing DB's existing users by id where present.
        tag_map: dict[int, int] = {}
        for t in tables["user_tags"]:
            existing = conn.execute(
                "SELECT id FROM user_tags WHERE user_id=? AND name=?",
                (t["user_id"], t["name"]),
            ).fetchone()
            if existing:
                tag_map[t["id"]] = existing["id"]
                continue
            user_exists = conn.execute(
                "SELECT 1 FROM users WHERE id=?", (t["user_id"],)
            ).fetchone()
            if not user_exists:
                continue  # skip tags whose owning user wasn't imported
            cur = conn.execute(
                "INSERT INTO user_tags(user_id, name) VALUES(?,?)",
                (t["user_id"], t["name"]),
            )
            tag_map[t["id"]] = cur.lastrowid
            imported["user_tags"] += 1

        # 5. document_tags
        for dt in tables["document_tags"]:
            tgt_doc = doc_map.get(dt["document_id"])
            tgt_tag = tag_map.get(dt["tag_id"])
            if tgt_doc is None or tgt_tag is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO document_tags(user_id, document_id, tag_id, "
                "created_at) VALUES(?,?,?,?)",
                (dt["user_id"], tgt_doc, tgt_tag, dt.get("created_at") or now),
            )
            imported["document_tags"] += 1

        # 6. user_groups (membership) — remap principal, keep user_id if present
        for ug in tables["user_groups"]:
            tgt_principal = principal_map.get(ug["principal_id"])
            if tgt_principal is None:
                continue
            user_exists = conn.execute(
                "SELECT 1 FROM users WHERE id=?", (ug["user_id"],)
            ).fetchone()
            if not user_exists:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO user_groups(user_id, principal_id) VALUES(?,?)",
                (ug["user_id"], tgt_principal),
            )
            imported["user_groups"] += 1

        # 7. document_acl — remap both document and principal
        for a in tables["document_acl"]:
            tgt_doc = doc_map.get(a["document_id"])
            tgt_principal = principal_map.get(a["principal_id"])
            if tgt_doc is None or tgt_principal is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO document_acl(document_id, principal_id, "
                "permission, granted_at) VALUES(?,?,?,?)",
                (tgt_doc, tgt_principal, a["permission"], a.get("granted_at") or now),
            )
            imported["document_acl"] += 1

        conn.commit()
        return {"imported": imported}
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_backup_service.py -v
```

Expected: 13 passing.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

- [ ] **Step 6: Commit**

```powershell
git add document_search/services/backup_service.py tests/test_backup_service.py
git commit -m "feat(backup): ACL-aware export/import to portable zip archive"
```

---

## Task 5: Admin endpoints `POST /api/backup/run` + `GET /api/backups`

**Files:**
- Modify: `document_search/app.py`
- Test: `tests/test_app_backup.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_app_backup.py`:

```python
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from document_search.app import create_app


def _login(client, username="admin", password="admin"):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_backup_run_creates_file_and_lists_it(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_BACKUP_DIR", str(tmp_path / "backups"))
    app = create_app(str(tmp_path / "document_index.db"))
    with TestClient(app) as client:
        token = _login(client)
        r = client.post("/api/backup/run", headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        info = r.json()
        assert info["filename"].startswith("document_index_")
        # The created file must be an openable SQLite DB.
        created = Path(info["path"])
        assert created.exists()
        conn = sqlite3.connect(created)
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            conn.close()

        # And it must appear in the listing.
        r = client.get("/api/backups", headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        names = [b["filename"] for b in r.json()["backups"]]
        assert info["filename"] in names


def test_backup_run_requires_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_BACKUP_DIR", str(tmp_path / "backups"))
    app = create_app(str(tmp_path / "document_index.db"))
    with TestClient(app) as client:
        token = _login(client)
        # Create a non-admin user, log in as them.
        client.post(
            "/api/admin/users",
            headers={"X-Auth-Token": token},
            json={"username": "bob", "password": "password123", "role": "user"},
        )
        bob = client.post(
            "/api/login", json={"username": "bob", "password": "password123"}
        ).json()["token"]
        r = client.post("/api/backup/run", headers={"X-Auth-Token": bob})
        assert r.status_code == 403
        r = client.get("/api/backups", headers={"X-Auth-Token": bob})
        assert r.status_code == 403
```

> If the create-user endpoint path differs in this codebase, adjust the `/api/admin/users` call to the real admin user-creation route (search `app.py` for `create_user`). The 403 assertions are the load-bearing part.

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_backup.py -v
```

Expected: 404 on `/api/backup/run` (route not defined yet).

- [ ] **Step 3: Wire `BackupService` + add the two endpoints**

In `document_search/app.py`, inside `create_app`, find the persistent-job-queue block (the lines after `organizer = AiOrganizer()`):

```python
    # Persistent job queue
    from document_search.services.job_store import JobStore
    from document_search.services.job_worker import Worker
    _startup_db = SqliteStore(Path(db_path))
    job_store = JobStore(_startup_db)
    worker = Worker(job_store, max_concurrent=4, poll_interval_s=1.0)
    app.state.job_store = job_store
    app.state.worker = worker
```

Immediately **after** `app.state.worker = worker`, insert:

```python
    # Backup / restore / export service
    from document_search.services.backup_service import BackupService
    backup_service = BackupService(
        _startup_db,
        keep=int(os.getenv("DOCUMENT_SEARCH_BACKUP_KEEP", "14")),
    )
    app.state.backup_service = backup_service
```

Then add the endpoints. A good home is right after the `/api/update/status` endpoint (around line 914). Insert:

```python
    # ── Backup / restore / export ──────────────────────────────────────
    @app.post("/api/backup/run", tags=["backup"])
    def api_backup_run(x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        try:
            return backup_service.create_backup()
        except Exception as exc:  # pragma: no cover - defensive
            raise HTTPException(status_code=500, detail=f"Backup failed: {exc}")

    @app.get("/api/backups", tags=["backup"])
    def api_backup_list(x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        return {"backups": backup_service.list_backups()}
```

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_backup.py -v
```

Expected: 2 passing.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

- [ ] **Step 6: Commit**

```powershell
git add document_search/app.py tests/test_app_backup.py
git commit -m "feat(backup): admin POST /api/backup/run and GET /api/backups endpoints"
```

---

## Task 6: Restore, export, import endpoints + scheduled-backup job

**Files:**
- Modify: `document_search/app.py`
- Test: `tests/test_app_backup.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_app_backup.py`:

```python
def test_restore_endpoint_returns_restart_required(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_BACKUP_DIR", str(tmp_path / "backups"))
    app = create_app(str(tmp_path / "document_index.db"))
    with TestClient(app) as client:
        token = _login(client)
        made = client.post("/api/backup/run", headers={"X-Auth-Token": token}).json()
        r = client.post(
            "/api/backup/restore",
            headers={"X-Auth-Token": token},
            json={"filename": made["filename"]},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["restart_required"] is True
        assert body["restored_from"] == made["filename"]


def test_restore_rejects_bad_filename(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_BACKUP_DIR", str(tmp_path / "backups"))
    app = create_app(str(tmp_path / "document_index.db"))
    with TestClient(app) as client:
        token = _login(client)
        r = client.post(
            "/api/backup/restore",
            headers={"X-Auth-Token": token},
            json={"filename": "../../etc/passwd"},
        )
        assert r.status_code == 400


def test_export_then_import_endpoints(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEARCH_BACKUP_DIR", str(tmp_path / "backups"))
    app = create_app(str(tmp_path / "document_index.db"))
    with TestClient(app) as client:
        token = _login(client)
        # Seed a document directly via the store so export has content.
        from datetime import UTC, datetime
        now = datetime.now(tz=UTC).isoformat()
        app.state.backup_service.store.conn.execute(
            "INSERT INTO documents(path, filename, extension, file_size, modified_at, "
            "sha256, indexed_at, status) VALUES(?,?,?,?,?,?,?,?)",
            ("/d/x.pdf", "x.pdf", ".pdf", 5, now, "hx", now, "ok"),
        )
        app.state.backup_service.store.conn.commit()

        # Export returns a zip download.
        r = client.get("/api/backup/export", headers={"X-Auth-Token": token})
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] in (
            "application/zip", "application/octet-stream",
        )
        archive = tmp_path / "dl.zip"
        archive.write_bytes(r.content)

        # Import the same archive into a SECOND app/db; expect 200 + counts.
        app2 = create_app(str(tmp_path / "second.db"))
        with TestClient(app2) as client2:
            t2 = _login(client2)
            with open(archive, "rb") as fh:
                r = client2.post(
                    "/api/backup/import",
                    headers={"X-Auth-Token": t2},
                    files={"file": ("dl.zip", fh, "application/zip")},
                )
            assert r.status_code == 200, r.text
            assert r.json()["imported"]["documents"] == 1
            got = app2.state.backup_service.store.conn.execute(
                "SELECT 1 FROM documents WHERE path='/d/x.pdf'"
            ).fetchone()
            assert got is not None


def test_scheduled_backup_job_runs(tmp_path, monkeypatch):
    """Enqueueing a 'backup' job results in a file being created by the worker."""
    monkeypatch.setenv("DOCUMENT_SEARCH_BACKUP_DIR", str(tmp_path / "backups"))
    app = create_app(str(tmp_path / "document_index.db"))
    import time
    with TestClient(app):
        job_id = app.state.job_store.enqueue("backup", {})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = app.state.job_store.get(job_id)
            if job["state"] in ("succeeded", "failed"):
                break
            time.sleep(0.05)
        assert job["state"] == "succeeded", job.get("error_message")
        assert app.state.backup_service.list_backups()
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_backup.py -v
```

Expected: the four new tests fail (routes + `backup` handler missing).

- [ ] **Step 3: Add the restore/export/import endpoints + register the `backup` handler**

In `document_search/app.py`, first add a request model near the other Pydantic models (search for `class IndexRequest`):

```python
class RestoreRequest(BaseModel):
    filename: str
```

Register the `backup` job handler. Put it next to the other `@worker.handler(...)` registrations (after `_handle_ai_reorganize`):

```python
    @worker.handler("backup")
    def _handle_backup(payload: dict, progress_cb):
        info = app.state.backup_service.create_backup()
        return info
```

Now add the endpoints, right after `api_backup_list` from Task 5:

```python
    @app.post("/api/backup/restore", tags=["backup"])
    def api_backup_restore(
        req: RestoreRequest, x_auth_token: str | None = Header(default=None)
    ):
        require_admin(x_auth_token)
        try:
            return backup_service.restore_backup(req.filename)
        except (ValueError,) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.get("/api/backup/export", tags=["backup"])
    def api_backup_export(x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        import tempfile
        tmp = Path(tempfile.gettempdir()) / f"seekr_export_{uuid.uuid4().hex}.zip"
        backup_service.export_archive(tmp)
        return FileResponse(
            str(tmp),
            media_type="application/zip",
            filename="seekr_export.zip",
        )

    @app.post("/api/backup/import", tags=["backup"])
    async def api_backup_import(
        file: UploadFile = File(...),
        x_auth_token: str | None = Header(default=None),
    ):
        require_admin(x_auth_token)
        import tempfile
        data = await file.read()
        tmp = Path(tempfile.gettempdir()) / f"seekr_import_{uuid.uuid4().hex}.zip"
        tmp.write_bytes(data)
        try:
            return backup_service.import_archive(tmp)
        except (ValueError, KeyError, zipfile.BadZipFile) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid archive: {exc}")
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
```

`zipfile` must be importable at module scope. Add to the imports at the top of `app.py` (with the other stdlib imports):

```python
import zipfile
```

Finally, add the **scheduler** so backups can run on an interval. After the `@worker.handler("backup")` registration, add a tiny background timer that enqueues a backup job when the interval elapses:

```python
    _backup_interval_h = float(os.getenv("DOCUMENT_SEARCH_BACKUP_INTERVAL_HOURS", "0"))
    _backup_scheduler_stop = threading.Event()

    def _backup_scheduler() -> None:
        if _backup_interval_h <= 0:
            return  # disabled
        interval_s = _backup_interval_h * 3600
        # Wait first so we don't snapshot an empty DB on every boot.
        while not _backup_scheduler_stop.wait(interval_s):
            try:
                job_store.enqueue("backup", {})
            except Exception:
                log.exception("Failed to enqueue scheduled backup")

    @app.on_event("startup")
    def _start_backup_scheduler() -> None:
        if _backup_interval_h > 0:
            threading.Thread(
                target=_backup_scheduler, name="BackupScheduler", daemon=True
            ).start()

    @app.on_event("shutdown")
    def _stop_backup_scheduler() -> None:
        _backup_scheduler_stop.set()
```

> `log` may not exist in `app.py`. If `grep -n "^log = " document_search/app.py` finds nothing, add `import logging` and `log = logging.getLogger(__name__)` near the top imports.

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_app_backup.py -v
```

Expected: 6 passing.

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

- [ ] **Step 6: Commit**

```powershell
git add document_search/app.py tests/test_app_backup.py
git commit -m "feat(backup): restore/export/import endpoints + scheduled-backup job"
```

---

## Task 7: Update rollback in `scripts/update.sh`

**Files:**
- Modify: `scripts/update.sh`
- Test: `tests/test_update_rollback.py` (new)

The rollback strategy in bash:
1. Record `PREV_COMMIT` (`git rev-parse HEAD`) **before** pulling.
2. Take a WAL-safe backup by invoking the Python backup service (replaces the old `cp`).
3. `git pull`, `docker compose build/up`.
4. Health-check `GET /ready` (the observability plan's readiness probe) with retries.
5. On failure: `git checkout PREV_COMMIT`, rebuild, `up -d` again, exit non-zero so the caller (`/api/update/run`) reports an error.

- [ ] **Step 0: Confirm `/ready` exists (fallback only)**

```powershell
$env:PYTHONPATH = "."; Select-String -Path document_search/app.py -Pattern '"/ready"'
```

If nothing matches, add a minimal readiness endpoint (the observability plan will replace it). In `document_search/app.py`, near the other small GET endpoints, add:

```python
    @app.get("/ready", tags=["ops"])
    def api_ready():
        try:
            store().conn.execute("SELECT 1").fetchone()
        except Exception:
            raise HTTPException(status_code=503, detail="not ready")
        alive = getattr(app.state, "worker", None) is not None
        return {"status": "ready", "worker": alive}
```

If `/ready` already exists, skip this and reference it.

- [ ] **Step 1: Write the failing shell test**

Create `tests/test_update_rollback.py`. It runs the real `update.sh` against a throwaway git repo with `git`/`docker`/`curl` shadowed by stub scripts on `PATH`, so no real network/docker is touched:

```python
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"

pytestmark = pytest.mark.skipif(
    os.name == "nt" and not Path("/bin/sh").exists(),
    reason="needs a POSIX shell (run in the Linux container or WSL/git-bash)",
)


def _make_stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _init_repo(root: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "marker.txt").write_text("v1", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "v1"], cwd=root, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()


def _run_update(root: Path, ready_exit: str):
    """Run update.sh with stubbed git/docker/curl. `ready_exit` is the shell
    exit code the curl stub returns for the /ready probe (0 = healthy)."""
    bindir = root / "stubbin"
    bindir.mkdir()
    # git: real git for rev-parse/checkout, but 'pull' is a no-op that advances HEAD.
    _make_stub(bindir / "git", f"""
real_git() {{ command "{_which_git()}" "$@"; }}
case "$1" in
  pull) real_git commit --allow-empty -qm "pulled"; ;;
  fetch) : ;;
  *) real_git "$@" ;;
esac
""")
    _make_stub(bindir / "docker", "exit 0\n")
    # curl returns ready_exit for the health probe.
    _make_stub(bindir / "curl", f"exit {ready_exit}\n")

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}" + env["PATH"]
    env["DOCUMENT_SEARCH_SKIP_BACKUP"] = "1"  # don't invoke python backup in tests
    return subprocess.run(
        ["/bin/sh", str(UPDATE_SH)],
        cwd=root, env=env, capture_output=True, text=True,
    )


def _which_git() -> str:
    return subprocess.run(
        ["sh", "-c", "command -v git"], capture_output=True, text=True
    ).stdout.strip()


def _copy_update_script(root: Path) -> None:
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "update.sh").write_text(
        UPDATE_SH.read_text(encoding="utf-8"), encoding="utf-8"
    )


def test_update_succeeds_when_ready_ok(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    start = _init_repo(root)
    _copy_update_script(root)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "add script"], cwd=root, check=True)
    res = _run_update(root, ready_exit="0")
    assert res.returncode == 0, res.stderr + res.stdout
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
    ).stdout.strip()
    # HEAD advanced past the starting commit (the pull added a commit).
    assert head != start


def test_update_rolls_back_when_ready_fails(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    _copy_update_script(root)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "add script"], cwd=root, check=True)
    pre_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
    ).stdout.strip()

    res = _run_update(root, ready_exit="1")  # /ready never becomes healthy
    # Non-zero exit signals the rollback path ran and reported failure.
    assert res.returncode != 0, res.stdout
    assert "rollback" in (res.stdout + res.stderr).lower()
    # HEAD must be back at the pre-update commit.
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
    ).stdout.strip()
    assert head == pre_commit
```

- [ ] **Step 2: Run, expect FAIL**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_update_rollback.py -v
```

Expected: `test_update_rolls_back_when_ready_fails` fails — the current `update.sh` has no rollback and always exits 0.

> On native Windows without a POSIX `/bin/sh`, these tests skip. Run them in the Linux container or WSL/git-bash. The bash itself is what ships to production.

- [ ] **Step 3: Rewrite `scripts/update.sh` with rollback**

Replace the entire contents of `scripts/update.sh` with:

```sh
#!/bin/sh
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT_DIR"

# ── Config ───────────────────────────────────────────────────────────
READY_URL="${DOCUMENT_SEARCH_READY_URL:-http://localhost:8080/ready}"
READY_RETRIES="${DOCUMENT_SEARCH_READY_RETRIES:-30}"
READY_INTERVAL="${DOCUMENT_SEARCH_READY_INTERVAL:-2}"

log() { echo "[update] $*"; }

# ── 1. WAL-safe backup (replaces the old `cp`) ───────────────────────
# Use the Python backup service so the snapshot is consistent under WAL.
# Tests set DOCUMENT_SEARCH_SKIP_BACKUP=1 to avoid spawning Python.
if [ "${DOCUMENT_SEARCH_SKIP_BACKUP:-0}" != "1" ]; then
  if command -v python >/dev/null 2>&1; then
    DB_PATH="${DOCUMENT_SEARCH_DB:-data/document_index.db}"
    if [ -f "$DB_PATH" ]; then
      log "Creating WAL-safe backup of $DB_PATH"
      python - "$DB_PATH" <<'PY' || log "WARNING: backup step failed, continuing"
import sys
from pathlib import Path
from document_search.index.sqlite_store import SqliteStore
from document_search.services.backup_service import BackupService

db = Path(sys.argv[1])
store = SqliteStore(db)
info = BackupService(store).create_backup()
print("[update] Backup created:", info["filename"])
PY
    fi
  fi
fi

# ── 2. Record the rollback point BEFORE changing anything ────────────
PREV_COMMIT=""
if [ -d .git ]; then
  PREV_COMMIT="$(git rev-parse HEAD)"
  log "Pre-update commit: $PREV_COMMIT"
fi

# ── 3. Health-check helper ───────────────────────────────────────────
wait_for_ready() {
  i=0
  while [ "$i" -lt "$READY_RETRIES" ]; do
    if curl -fsS "$READY_URL" >/dev/null 2>&1; then
      return 0
    fi
    i=$((i + 1))
    sleep "$READY_INTERVAL"
  done
  return 1
}

# ── 4. Pull + rebuild ────────────────────────────────────────────────
if [ -d .git ]; then
  git fetch --all --tags
  git pull
fi

rebuild() {
  if command -v docker >/dev/null 2>&1; then
    docker compose down
    docker compose build --no-cache
    docker compose up -d
    docker image prune -f >/dev/null 2>&1 || true
  fi
}

rebuild

# ── 5. Health-check; roll back on failure ────────────────────────────
if wait_for_ready; then
  log "Update completed and /ready is healthy."
  exit 0
fi

log "ERROR: /ready did not become healthy. Starting rollback."
if [ -n "$PREV_COMMIT" ] && [ -d .git ]; then
  git checkout -f "$PREV_COMMIT"
  log "Rolled back to $PREV_COMMIT; rebuilding previous version."
  rebuild
  if wait_for_ready; then
    log "Rollback succeeded; previous version is healthy."
  else
    log "Rollback rebuild still unhealthy — manual intervention required."
  fi
else
  log "No previous commit recorded; cannot roll back automatically."
fi
# Always signal failure so /api/update/run reports an error to the operator.
exit 1
```

Key points the tests rely on: the script records `PREV_COMMIT` before `git pull`; it probes `/ready` via `curl`; on failure it prints a line containing "rollback", does `git checkout -f $PREV_COMMIT`, and exits non-zero.

- [ ] **Step 4: Run, expect PASS**

```powershell
$env:PYTHONPATH = "."; pytest -q tests/test_update_rollback.py -v
```

Expected: 2 passing (or skipped on native Windows without `/bin/sh` — run in the container/WSL).

- [ ] **Step 5: Full suite**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

- [ ] **Step 6: Commit**

```powershell
git add scripts/update.sh tests/test_update_rollback.py document_search/app.py
git commit -m "feat(backup): update.sh rollback on failed /ready health-check + WAL-safe backup"
```

---

## Task 8: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Full suite clean**

```powershell
$env:PYTHONPATH = "."; pytest -q
```

Expected: all green, zero failures, no flaky reruns.

- [ ] **Step 2: Manual smoke test of backup + restore + export round-trip**

```powershell
$env:PYTHONPATH = "."; python -c "
from fastapi.testclient import TestClient
from document_search.app import create_app
import tempfile, sqlite3, pathlib
tmp = pathlib.Path(tempfile.mkdtemp())
app = create_app(str(tmp / 'document_index.db'))
with TestClient(app) as c:
    tok = c.post('/api/login', json={'username':'admin','password':'admin'}).json()['token']
    h = {'X-Auth-Token': tok}
    b = c.post('/api/backup/run', headers=h).json()
    print('backup =', b['filename'])
    conn = sqlite3.connect(b['path'])
    assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    conn.close()
    lst = c.get('/api/backups', headers=h).json()['backups']
    assert any(x['filename'] == b['filename'] for x in lst)
    r = c.post('/api/backup/restore', headers=h, json={'filename': b['filename']}).json()
    assert r['restart_required'] is True
    exp = c.get('/api/backup/export', headers=h)
    assert exp.status_code == 200 and len(exp.content) > 0
print('OK')
"
```

Expected: prints `backup = document_index_...`, then `OK`.

- [ ] **Step 3: Verify the backup directory on disk (real deployment only)**

```powershell
Get-ChildItem -Path (Join-Path (Split-Path $env:DOCUMENT_SEARCH_DB) 'backups') -Filter 'document_index_*.db' -ErrorAction SilentlyContinue
```

(No assertion in CI — the smoke test used a temp dir. In a real deployment, confirm `backups/` contains `document_index_*.db` files and that each passes `PRAGMA integrity_check = ok`.)

- [ ] **Step 4: No commit if no changes**

This task makes no changes.

---

## Definition of Done

- [ ] `pytest -q` green; all new tests in `tests/test_backup_service.py`, `tests/test_app_backup.py`, and (on POSIX) `tests/test_update_rollback.py` pass.
- [ ] `BackupService.create_backup()` produces a single timestamped file that passes `PRAGMA integrity_check = ok` and contains data committed through WAL — proven by `test_backup_is_a_valid_openable_sqlite_db` and `test_backup_captures_data_written_through_wal`.
- [ ] `BackupService` is the only module calling `sqlite3.Connection.backup()` (grep enforcement: no `.backup(` elsewhere) and the only writer of the backup directory.
- [ ] `GET /api/backups` lists backups newest-first; retention prunes to `DOCUMENT_SEARCH_BACKUP_KEEP` (default 14).
- [ ] `POST /api/backup/restore` takes a pre-restore safety backup, validates `integrity_check`, swaps the DB file (removing `-wal`/`-shm`), and returns `restart_required: true`. Path traversal is rejected (400).
- [ ] `GET /api/backup/export` downloads a `seekr-export` zip containing `documents`, `content_blocks`, `user_tags`, `document_tags`, `principals`, `user_groups`, `document_acl`; `POST /api/backup/import` ingests it idempotently and ACL-aware (principals matched by `(type, external_id)`, docs by `path`, FTS rebuilt). Round-trip proven by `test_import_round_trips_documents_tags_and_acls`.
- [ ] All five backup endpoints require admin (`require_admin`) — non-admins get 403.
- [ ] A `backup` job kind runs `create_backup()` via the worker; an opt-in scheduler (`DOCUMENT_SEARCH_BACKUP_INTERVAL_HOURS`, default 0 = off) enqueues it periodically.
- [ ] `scripts/update.sh` replaces the raw `cp` with a WAL-safe Python backup, records `PREV_COMMIT` before pulling, health-checks `/ready`, and on failure rolls back (`git checkout` prev + rebuild) and exits non-zero. Proven by `tests/test_update_rollback.py`.
- [ ] `/ready` exists (consumed from the observability plan, or a minimal fallback added here).
- [ ] Manual smoke test passes.

---

## Notes for the executing agent

- **Why `Connection.backup()` and not `shutil.copyfile()`:** the DB runs in `journal_mode=WAL` (`sqlite_store.py:_configure_connection`). Under WAL, recently-committed pages live in the `-wal` sidecar until a checkpoint folds them into the main file. Copying only `document_index.db` can yield a database missing the latest commits or — worse — a torn page mix. `Connection.backup()` is the SQLite online-backup API; it copies a consistent snapshot through the live connection including WAL content. The dedicated `test_backup_captures_data_written_through_wal` test is the regression guard for this exact failure mode.
- **Why restore requires a restart and never hot-swaps:** the app keeps per-thread `SqliteStore` connections (`app.py:store()` via `_thread_local`) plus the worker's `_startup_db`. Replacing the file under live open handles risks `SQLITE_CORRUPT`/stale-cache reads. `restore_backup()` copies the file and returns `restart_required: true`; the operator (or `docker compose restart`) brings the process back on the restored DB. This is documented in the endpoint response and in the DoD.
- **Pre-restore safety backup is mandatory and best-effort-logged:** restore first snapshots the *current* state so a mistaken restore is itself recoverable. If that snapshot fails (e.g. disk full), restore still proceeds but reports `safety_backup: null` — the test `test_restore_takes_pre_restore_safety_backup` asserts the happy path.
- **Export excludes `users` and `content_fts` on purpose:** password hashes must not travel in a portable archive, and FTS is a derived index rebuilt from `content_blocks` on import (the import handler writes `content_fts` rows itself). This keeps archives smaller and avoids FTS-version coupling.
- **Import ID remapping:** never trust source primary keys. Principals are matched by `(type, external_id)`, documents by `path`, tags by `(user_id, name)`; every foreign key is rewritten to the *target* IDs. This is what makes import idempotent and safe to run against a populated DB. `user_id` on tags/memberships is intentionally NOT remapped (users aren't exported) — rows whose owning user is absent in the target are skipped rather than orphaned.
- **Scheduler is opt-in (`interval=0` disables it)** so existing single-node deployments see no behaviour change until an operator sets `DOCUMENT_SEARCH_BACKUP_INTERVAL_HOURS`. It enqueues onto the existing persistent queue rather than spawning its own backup thread, so scheduled and manual backups share one code path and retention policy.
- **`update.sh` rollback uses `/ready`, not `/health`:** liveness (`/health`) only proves the process is up; readiness (`/ready`) proves the DB is reachable and the worker is alive — exactly what a failed rebuild would break. If the observability plan hasn't landed, Task 7 Step 0 adds a minimal `/ready`; when that plan lands, delete the fallback and rely on its richer probe.
- **`update.sh` is the production artifact; the Python test only exercises its logic.** The test shadows `git`/`docker`/`curl` with stub scripts on `PATH` so no network or docker is touched, and sets `DOCUMENT_SEARCH_SKIP_BACKUP=1` to skip the Python backup spawn. The two cases that matter — healthy `/ready` → exit 0, unhealthy `/ready` → `git checkout` prev + exit 1 — are both covered. On native Windows the test self-skips; run it in the Linux container or WSL/git-bash, which is the environment the script actually runs in.
- **Conventional commits:** every commit in this plan is `feat(backup): ...` (or `test(backup): ...` if a step is test-only). Keep diffs per-task small and reviewable, per `CLAUDE.md`.
