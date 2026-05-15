#!/bin/sh
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT_DIR"

if [ -f "data/document_index.db" ]; then
  TS="$(date +%Y%m%d_%H%M%S)"
  mkdir -p backups
  cp "data/document_index.db" "backups/document_index_${TS}.db"
  echo "Backup created: backups/document_index_${TS}.db"
fi

# Pull latest repository changes
if [ -d .git ]; then
  git fetch --all --tags
  git pull
fi

# Full rebuild: stop, rebuild without cache, restart
if command -v docker >/dev/null 2>&1; then
  docker compose down
  docker compose build --no-cache
  docker compose up -d
  docker image prune -f >/dev/null 2>&1 || true
fi

echo "Update completed."
