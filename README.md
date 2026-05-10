# Document Search

A fully local crawler, indexer, search engine, and modern web UI for PDF, DOCX, PPTX, TXT, Markdown, and legacy DOC/PPT files.

## Highlights
- Fully local processing (no cloud calls, no telemetry)
- Recursive crawler with include/exclude rules
- Modular extractors per file type
- SQLite + FTS5 full-text index
- Incremental indexing (size + mtime + SHA-256)
- Modern browser UI + JSON API
- Local user account login (default admin account)
- Async indexing jobs with progress polling
- File-open actions from search results
- Query builder filters (filetype/path/date/block type)
- Containerized runtime with Docker Compose (works great on Proxmox VMs/LXCs)

## Quick Start (Docker Compose)
```bash
mkdir -p documents data
cp config.example.json config.json

docker compose up -d --build
```
Open: `http://localhost:8080`

> Place files to index in `./documents` (mounted read-only as `/documents`).

## Quick Start (Local Python)
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn document_search.app:app --host 0.0.0.0 --port 8080
```

## CLI
```bash
python -m document_search.main --db ./document_index.db index ./documents
python -m document_search.main --db ./document_index.db search "wartung AND anlage"
python -m document_search.main --db ./document_index.db status
python -m document_search.main --db ./document_index.db remove-missing
python -m document_search.main --db ./document_index.db reset
```

## Web UI
- Index one or multiple comma-separated folders
- Search with full-text query syntax
- View live index status
- Mark documents (star) per user
- Add per-user tags to documents
- Highlight query keywords directly in result snippets

## Proxmox Deployment Notes
1. Create an Ubuntu 22.04+ VM or Debian 12 VM/LXC.
2. Install Docker + Docker Compose plugin.
3. Clone this repo and run `docker compose up -d --build`.
4. Bind a host dataset for documents and database (`./documents`, `./data`).
5. Optionally place behind Traefik/Caddy/Nginx with TLS.

## Project Structure
- `document_search/crawler.py`: recursive discovery and filtering
- `document_search/extractors/`: format-specific extraction
- `document_search/index/sqlite_store.py`: persistence + FTS
- `document_search/index/search_service.py`: query/search logic
- `document_search/app.py`: FastAPI backend + UI serving
- `document_search/main.py`: CLI entrypoint

## Known Limitations
- OCR pipeline is not implemented yet (architecture prepared)
- Legacy `.doc`/`.ppt` and `.xlsx` not implemented yet
- No auth by default (recommended to run behind reverse proxy auth in shared environments)

## Roadmap
- OCR support
- Rich filter builder in UI
- Saved searches and bookmarks
- Async background indexing jobs
- REST auth and role-based access


## Authentication
- First startup creates a local `admin` user in SQLite.
- Default password: `admin` (override with env `DOCUMENT_SEARCH_DEFAULT_PASSWORD`).

## Async Index Jobs
- Start indexing via `/api/index/start` and poll `/api/index/jobs/{job_id}` for progress.



## Installation Guide
See `INSTALL.md` for full Docker Compose + Proxmox setup, including network share mounting.


## OCR
Enable OCR fallback with environment variable:
`DOCUMENT_SEARCH_OCR_ENABLED=true`

- PDF: OCR fallback if no text is extracted
- DOCX/PPTX: OCR on embedded media images

## Security Hardening
- File-open endpoint now resolves document by indexed `document_id` instead of arbitrary path input.
- Session tokens expire automatically after 8 hours.


## Additional file types
- `.md` supported natively
- `.doc` and `.ppt` supported via external tools (`antiword`, `catppt`) when installed in runtime


## Runtime dependencies
Docker image installs required legacy/OCR runtime tools automatically:
- antiword (for .doc)
- catppt (for .ppt via catdoc package)
- tesseract + language packs (deu/eng)
- poppler-utils (for pdf2image support)

You can verify availability after login via `GET /api/system/dependencies`.


## Document uploader
UI now includes an uploader that supports:
- file upload
- target folder subpath
- tags
- arbitrary metadata JSON

Uploaded files are saved locally and sidecar metadata is stored as `.meta.json`.
A future AI integration point (`AiOrganizer`) is included for Ollama/LLM-based filing suggestions.


## Update Process
See `UPDATE.md` or run `./scripts/update.sh` for automated updates with DB backup + container rebuild.


## Configuration persistence
UI configuration is persisted to `/data/config.json` (mounted from host `./data` in Docker Compose), so settings survive restarts and updates.


## UI update
An authenticated UI action is available via **System Update** (calls `POST /api/update/run`) and executes `scripts/update.sh` in-container.
