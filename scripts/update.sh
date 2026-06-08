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
