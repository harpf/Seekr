# Update Process

Yes — an update process is now included.

## Docker Compose deployment update

Run:

```bash
./scripts/update.sh
```

This script will:
1. Backup `data/document_index.db` into `backups/`.
2. Pull latest git changes (if repo is a git checkout).
3. Pull/rebuild containers and restart the stack (`docker compose up -d --build`).
4. Prune unused Docker images.

## Manual update (alternative)

```bash
git pull --ff-only
docker compose up -d --build
```

## Rollback

If needed, restore an old DB backup:

```bash
cp backups/document_index_YYYYMMDD_HHMMSS.db data/document_index.db
docker compose restart document-search
```


## UI update
An authenticated UI action is available via **System Update** (calls `POST /api/update/run`) and executes `scripts/update.sh` in-container.
