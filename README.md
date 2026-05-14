# Seekr

A self-hosted document indexing and search system with optional local AI assistance via Ollama. Runs entirely on your own infrastructure — no cloud dependencies.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Quick Start](#quick-start)
- [First-Run Checklist](#first-run-checklist)
- [Environment Variables](#environment-variables)
- [Configuration File](#configuration-file)
- [User Management](#user-management)
- [Indexing Documents](#indexing-documents)
- [Search](#search)
- [Tags](#tags)
- [File Upload & AI Filing](#file-upload--ai-filing)
- [AI Features](#ai-features)
- [SSL / TLS](#ssl--tls)
- [GPU Acceleration (Ollama)](#gpu-acceleration-ollama)
- [Development Setup](#development-setup)
- [Running Tests](#running-tests)
- [Upgrading](#upgrading)
- [Troubleshooting](#troubleshooting)

---

## Overview

Seekr crawls file system paths, extracts text from PDFs, Word documents, PowerPoint files, plain text, and Markdown, then stores everything in a full-text search index backed by SQLite FTS5. A web UI lets you search, tag, upload, and manage documents. An optional Ollama container adds AI-powered filing suggestions, folder structure recommendations, and document reorganisation.

**Supported file types**

| Extension | Extractor | Notes |
|-----------|-----------|-------|
| `.pdf` | pypdf + Tesseract OCR | OCR is optional, see config |
| `.docx` | python-docx | |
| `.pptx` | python-pptx | |
| `.doc` / `.ppt` | antiword / catppt | Requires system packages |
| `.txt` / `.md` | Built-in | |

---

## Architecture

```
┌──────────────────────────────────────────────┐
│  Browser                                     │
└────────────────────┬─────────────────────────┘
                     │ HTTPS :443
┌────────────────────▼─────────────────────────┐
│  tls-proxy  (nginx:1.27-alpine)              │
│  Terminates TLS, proxies to document-search  │
└────────────────────┬─────────────────────────┘
                     │ HTTP :8080 (internal)
┌────────────────────▼─────────────────────────┐
│  document-search  (Python 3.11, FastAPI)     │
│  ┌─────────────┐  ┌──────────────────────┐  │
│  │  Web UI     │  │  REST API            │  │
│  │  (Jinja2 +  │  │  /api/*              │  │
│  │   Vanilla   │  │                      │  │
│  │   JS)       │  │                      │  │
│  └─────────────┘  └──────────────────────┘  │
│  ┌───────────────────────────────────────┐   │
│  │  SQLite + FTS5 index  (/data/)        │   │
│  └───────────────────────────────────────┘   │
└────────────────────┬─────────────────────────┘
                     │ HTTP :11434 (internal)
┌────────────────────▼─────────────────────────┐
│  ollama  (ollama/ollama:latest)              │
│  Local LLM inference — optional              │
└──────────────────────────────────────────────┘
```

**Data flow for indexing:**
Crawler → Extractor (text + metadata) → SqliteStore (documents + content\_blocks + FTS5 index)

**Data flow for search:**
Query → FTS5 MATCH → JOIN documents + content\_blocks → Snippet highlighting → JSON response

---

## Requirements

### Docker deployment (recommended)

- Docker Engine 24+ with Compose V2 (`docker compose`)
- 2 GB RAM minimum (4 GB+ recommended if using Ollama)
- Disk space: ~500 MB for the image + space for your documents and model weights

### Local development

- Python 3.11+
- System packages: `antiword`, `catdoc`, `poppler-utils`, `tesseract-ocr`
- pip packages from `requirements.txt`

---

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/harpf/Seekr.git
cd Seekr
```

### 2. Create the data directory

```bash
mkdir -p data documents
```

The `data/` directory holds the SQLite database and configuration. The `documents/` directory is mounted read-only into the container as `/documents` and is the default indexing root.

### 3. Configure the TLS proxy

Edit `docker-compose.yml` and set your hostname and IP for the TLS proxy:

```yaml
  tls-proxy:
    environment:
      TLS_DOMAIN: seekr.yourdomain.local   # hostname for the self-signed cert
      TLS_IP: 192.168.1.100               # IP to include in the SAN
```

If you already have certificates, place them at `certs/tls.crt` and `certs/tls.key` before starting — the entrypoint script only generates a self-signed cert when those files are absent.

### 4. Start the stack

```bash
docker compose up -d
```

This starts three containers:

| Container | Role | Port |
|-----------|------|------|
| `document-search` | Application | 8080 (internal only) |
| `seekr-ollama` | Local LLM | 11434 (internal only) |
| `document-search-tls` | TLS reverse proxy | **443 (host)** |

### 5. Open the UI

Navigate to `https://<your-host>/` and accept the self-signed certificate warning.
Default credentials: **admin / admin**

> **Change the default password immediately.** Go to Config → Users → click "Password" next to the admin account.

---

## First-Run Checklist

1. **Change the admin password** — Config → Users → Password (minimum 8 characters)
2. **Add source paths** — Config → Paths → add the directories you want indexed
3. **Pull an AI model** (optional) — Config → AI → enter a model name → Pull
4. **Run the first index** — Ingest → Index Documents → enter your paths → Start Indexing
5. **Search** — Search → enter a query

---

## Environment Variables

Set these in `docker-compose.yml` under the `document-search` service or in a `.env` file.

| Variable | Default | Description |
|---|---|---|
| `DOCUMENT_SEARCH_DB` | `/data/document_index.db` | Path to the SQLite database file |
| `DOCUMENT_SEARCH_CONFIG_PATH` | `./config.json` | Path to the JSON configuration file |
| `DOCUMENT_SEARCH_SSL_DIR` | `/data/ssl` | Directory for self-service TLS certificates |
| `DOCUMENT_SEARCH_UPLOAD_ROOT` | `/data/uploads` | Root directory for uploaded files |
| `DOCUMENT_SEARCH_OLLAMA_URL` | `http://ollama:11434` | Ollama API base URL |
| `DOCUMENT_SEARCH_OLLAMA_MODEL` | `llama3.2` | Default model for AI features |
| `DOCUMENT_SEARCH_DEFAULT_PASSWORD` | `admin` | Password for the auto-created admin on first start |
| `DOCUMENT_SEARCH_UI_UPDATE_ENABLED` | `true` | Set to `false` to disable the in-app update button |

**Ollama environment variables** (under the `ollama` service):

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_KEEP_ALIVE` | `24h` | How long to keep a model loaded in memory after the last request |

---

## Configuration File

The configuration is stored at the path set by `DOCUMENT_SEARCH_CONFIG_PATH` (default `/data/config.json`). It is managed through the UI under **Config → General** (admin only) but can also be edited manually.

```json
{
  "database_path": "/data/document_index.db",
  "supported_extensions": [".pdf", ".docx", ".pptx", ".txt", ".md"],
  "exclude_dirs": [".git", "node_modules", "__pycache__", ".venv", "temp"],
  "exclude_patterns": ["~$*", "*.tmp"],
  "max_file_size_mb": 100,
  "source_paths": [
    {
      "path": "/documents/invoices",
      "label": "Invoices",
      "type": "local"
    }
  ],
  "ollama_url": "http://ollama:11434",
  "ollama_model": "llama3.2"
}
```

| Field | Type | Description |
|---|---|---|
| `database_path` | string | SQLite database file location |
| `supported_extensions` | list | File extensions to index |
| `exclude_dirs` | list | Directory names to skip during crawling |
| `exclude_patterns` | list | Glob patterns for files to skip |
| `max_file_size_mb` | int | Maximum file size for uploads (default 100 MB) |
| `source_paths` | list | Named source path entries shown in the Paths tab |
| `ollama_url` | string | Ollama HTTP API URL — must start with `http://` or `https://` |
| `ollama_model` | string | Model name to use for all AI features |

---

## User Management

User management is available to admins at **Config → Users**.

| Action | How |
|---|---|
| Create user | Enter username, password, role → Create |
| Change role | Use the role dropdown in the user table |
| Reset password | Click "Password" → enter new password |
| Delete user | Click "Delete" — cannot delete your own account |

**Roles:**

| Role | Capabilities |
|---|---|
| `user` | Search, view documents, manage own tags and marks, upload files |
| `admin` | Everything above + user management, config, indexing, cleanup, SSL, AI config, path testing, system update |

**Username rules:** 1–64 characters; letters, digits, `_`, `-`, `.` only.
**Password rules:** minimum 8 characters.

Sessions expire after 8 hours of inactivity. Login is rate-limited to 10 failed attempts per 5-minute window per IP address.

---

## Indexing Documents

### Bulk indexing via the UI

1. Go to **Ingest → Index Documents**
2. Enter one or more absolute paths (comma-separated), e.g. `/documents/archive, /documents/invoices`
3. Click **Start Indexing** — a progress bar tracks the job

The indexer skips files whose SHA-256 hash and modification time match the stored record (no re-extraction unless the file actually changed).

### Index maintenance

| Action | Location | Description |
|---|---|---|
| Reindex a single file | Ingest → Index Maintenance → Reindex by ID | Re-extracts a document by its numeric ID |
| Reindex from search | Search result → Reindex button | One-click re-extraction without leaving search |
| Remove missing files | Ingest → Index Maintenance → Clean up index | Removes entries whose files no longer exist on disk |
| Full re-scan | Ingest → Index Maintenance → Run Full Re-scan | Runs `update.sh` to pull latest code and restart |

### Adding volume mounts

The `documents/` directory on the host is mounted at `/documents` inside the container as read-only. To index additional paths, add volume mounts in `docker-compose.yml`:

```yaml
  document-search:
    volumes:
      - ./data:/data
      - ./documents:/documents:ro
      - /mnt/nas/archive:/archive:ro     # additional mount
      - /srv/projects:/projects:ro
```

Then re-run `docker compose up -d`. The new paths become available to the indexer immediately.

### Network shares (admin only)

Admins can mount SMB and NFS shares through the UI at **Config → Paths → Mount network share**. The mount persists until explicitly unmounted or the container restarts.

> SMB mounts require `cap_add: [SYS_ADMIN]` in `docker-compose.yml` and the `cifs-utils` package inside the container.

---

## Search

Navigate to **Search** and enter a query. SQLite FTS5 full-text search supports:

| Syntax | Example | Meaning |
|---|---|---|
| Plain term | `invoice` | Documents containing "invoice" |
| AND | `invoice AND customer` | Both terms present |
| OR | `invoice OR receipt` | Either term present |
| Phrase | `"annual report"` | Exact phrase |
| Prefix | `maint*` | Terms starting with "maint" |
| Negation | `invoice -draft` | Contains "invoice" but not "draft" |

**Filters** (expand "Show filters"):

| Filter | Description |
|---|---|
| File type | Match by extension, e.g. `pdf` or `.docx` |
| Path filter | Prefix match on the stored file path |
| Block type | `page`, `paragraph`, `table`, or `slide` |
| Modified from / to | ISO 8601 date range on file modification time |
| Tag filter | Restrict results to a specific tag |

**Tag cloud:** A browsable tag cloud appears above the results when you have tagged documents. Click any chip to filter instantly.

---

## Tags

Tags are per-user labels that can be attached to any indexed document.

| Where | How |
|---|---|
| Search results | Edit the tag input in any result row → Save tags |
| Tag cloud | Click a chip to filter all results by that tag |
| On upload | Enter comma-separated tags in the Tags field |
| AI suggestion | Ollama proposes tags after each upload |

Tags are stored under your user account — each user has their own independent tag space.

---

## File Upload & AI Filing

Go to **Ingest → Upload Document**:

1. Drag-and-drop a file or click the drop zone to browse
2. Set an optional target subpath (subdirectory within the upload root)
3. Add comma-separated tags
4. Add metadata as a JSON object (optional, max 8 KB)
5. Click **Upload**

Uploaded files are stored in `DOCUMENT_SEARCH_UPLOAD_ROOT` (default `/data/uploads`).

**Allowed extensions:** `.pdf`, `.docx`, `.pptx`, `.doc`, `.ppt`, `.txt`, `.md`
**File size limit:** controlled by `max_file_size_mb` in config (default 100 MB)

### AI Filing Suggestion

If Ollama is running and a model is loaded, an **AI Filing Suggestion** card appears automatically after a successful upload. It shows a suggested destination path, suggested tags, and the model's reasoning.

- **Apply & move file** — physically moves the file and updates the index record
- **Dismiss** — ignores the suggestion

---

## AI Features

All AI features require Ollama to be running and a model to be pulled. Configure at **Config → AI** (admin only).

### Setting up Ollama

1. Go to **Config → AI → Connection & Model**
2. Verify the Ollama URL matches your deployment (default `http://ollama:11434`)
3. Enter the model name you want (e.g. `llama3.2`, `qwen2.5:7b`, `mistral`)
4. Click **Save & Apply**, then **Pull** to download the model
5. Click **Test connection** to verify the model responds correctly

### Choosing a model

The **System Resources** card shows your available RAM and GPU VRAM with a tier recommendation:

| Tier | Free RAM | Max model size | Recommended models |
|---|---|---|---|
| Tiny | < 3 GB | ~2 GB | `qwen2.5:0.5b`, `tinyllama` |
| Small | 3–6 GB | ~4 GB | `llama3.2:1b`, `qwen2.5:3b` |
| Medium | 6–10 GB | ~6 GB | `llama3.2`, `qwen2.5:7b`, `mistral` |
| Large | 10–20 GB | ~12 GB | `llama3.1:8b`, `qwen2.5:14b` |
| XLarge | 20+ GB | 75% of free RAM | `llama3.1:70b`, `qwen2.5:32b` |

Each model in the library is shown with a **✓ ok**, **⚠ warn**, or **✗ too-large** fit indicator.

### AI features overview

| Feature | Location | Who | Description |
|---|---|---|---|
| Filing suggestion | Ingest → after upload | All users | Suggests folder + tags for an uploaded file |
| AI Reorganizer | Ingest → AI Reorganizer | Admin | Batch-analyses uploaded documents and suggests bulk moves |
| Folder Structure Suggestions | Ingest → AI: Folder Structure | Admin | Analyses the full index and proposes an optimal folder taxonomy |

---

## SSL / TLS

### Automatic self-signed certificate (default)

The `tls-proxy` container generates a 2048-bit RSA certificate on first start using the `TLS_DOMAIN` and `TLS_IP` environment variables. The certificate is written to `certs/tls.crt` and `certs/tls.key` on the host.

To regenerate it, delete those two files and restart `tls-proxy`:

```bash
rm certs/tls.crt certs/tls.key
docker compose restart tls-proxy
```

### Bring your own certificate

Place your certificate files at `certs/tls.crt` (full chain, PEM) and `certs/tls.key` (private key, PEM) before starting the stack. The entrypoint script skips generation when both files already exist.

### Self-service certificate via the UI

Admins can also generate or replace the application-layer certificate at **Config → SSL**:

- **Generate** — creates a new RSA-2048 self-signed cert with configurable CN, SANs, and validity period
- **Upload** — upload a PEM certificate + key pair (e.g. from your internal CA or Let's Encrypt)

This certificate lands in `DOCUMENT_SEARCH_SSL_DIR` (`/data/ssl`) and is independent of the nginx proxy certificate.

---

## GPU Acceleration (Ollama)

To enable NVIDIA GPU inference, uncomment the `deploy` block under the `ollama` service in `docker-compose.yml`:

```yaml
  ollama:
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

**Prerequisites:**

- NVIDIA driver installed on the host
- [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) installed and configured
- Docker daemon configured to use the NVIDIA runtime

Verify GPU access inside the container:

```bash
docker exec seekr-ollama nvidia-smi
```

---

## Development Setup

### 1. Install system dependencies

**Debian / Ubuntu:**

```bash
sudo apt-get install antiword catdoc poppler-utils \
    tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng
```

**macOS (Homebrew):**

```bash
brew install antiword poppler tesseract
```

### 2. Create a virtual environment

```bash
python3.11 -m venv .venv
source .venv/bin/activate        # Linux / macOS
.venv\Scripts\activate           # Windows
```

### 3. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 4. Start the application

```bash
uvicorn document_search.app:app --reload --port 8080
```

The app is now available at `http://localhost:8080`.

### 5. Optional: run Ollama locally

```bash
docker run -d -p 11434:11434 --name ollama ollama/ollama:latest
DOCUMENT_SEARCH_OLLAMA_URL=http://localhost:11434 uvicorn document_search.app:app --reload
```

### Project structure

```
Seekr/
├── document_search/
│   ├── app.py                  # FastAPI application and all API routes
│   ├── auth.py                 # PBKDF2-HMAC-SHA256 password hashing
│   ├── config.py               # AppConfig dataclass, JSON/YAML loader
│   ├── crawler.py              # Filesystem walker
│   ├── models.py               # FileFingerprint, ExtractionResult, ContentBlock
│   ├── extractors/             # One module per supported file type
│   ├── index/
│   │   ├── sqlite_store.py     # All DB operations, schema, migrations
│   │   └── search_service.py   # FTS5 query builder and result fetcher
│   └── services/
│       ├── ai_organizer.py     # Ollama HTTP client (suggest, suggest_structure)
│       ├── file_service.py     # SHA-256 + mtime fingerprinting
│       └── ocr_service.py      # Tesseract wrapper
├── document_search/web/
│   ├── templates/              # Jinja2 HTML templates (index, search, ingest, config)
│   └── static/
│       ├── app.js              # All frontend logic — vanilla JS, no build step
│       └── styles.css          # UI stylesheet
├── tests/                      # pytest test suite
├── deploy/
│   ├── nginx.conf              # nginx TLS proxy configuration
│   └── entrypoint.sh           # Certificate generation and nginx start
├── scripts/
│   └── update.sh               # In-app update: backup + git pull + docker compose up
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## Running Tests

```bash
# Full suite
pytest -q

# Single file
pytest -q tests/test_search_service.py

# With explicit PYTHONPATH (if imports fail locally)
PYTHONPATH=. pytest -q
```

The two FastAPI integration tests (`test_app_status.py`) are skipped automatically when FastAPI is not installed in the active environment.

---

## Upgrading

### Via the UI

Go to **Ingest → Index Maintenance → Run Full Re-scan** (admin only). This runs `scripts/update.sh`, which:

1. Creates a timestamped backup of the SQLite database in `backups/`
2. Runs `git pull --ff-only` to fetch the latest code
3. Runs `docker compose up -d --build` to rebuild and restart all containers
4. Prunes dangling images

### Manually

```bash
# From the project root on the host
./scripts/update.sh
```

Or step by step:

```bash
git pull
docker compose up -d --build
docker image prune -f
```

> The SQLite schema uses `CREATE TABLE IF NOT EXISTS` and additive `ALTER TABLE` migrations wrapped in `try/except`. Existing data is preserved across upgrades.

---

## Troubleshooting

### Web UI returns 502 Bad Gateway

The `tls-proxy` is up but `document-search` is still starting.

```bash
docker compose logs document-search
```

### Login fails with "Invalid credentials"

The default password is `admin`. If `DOCUMENT_SEARCH_DEFAULT_PASSWORD` was changed but the database already exists, the env var no longer applies — use the admin UI to reset the password, or delete `data/document_index.db` to start fresh (all indexed data will be lost).

### Login blocked — "Too many failed login attempts"

The rate limiter allows 10 failures per 5 minutes per IP. Wait 5 minutes or restart the `document-search` container to clear the in-memory counter.

### No results after indexing

1. Check **Config → System → Database Test** — confirm document count is greater than 0
2. Verify your query syntax (unmatched quotes cause FTS parse errors)
3. Confirm the indexed path contains files of a supported extension

### OCR or legacy `.doc` / `.ppt` files not extracted

These require system packages. Verify inside the container:

```bash
docker exec document-search antiword -h 2>&1 | head -1
docker exec document-search tesseract --version
```

If running outside Docker, install the packages listed in [Development Setup](#development-setup).

### Ollama model pull fails or times out

- Check internet access from the Ollama container: `docker exec seekr-ollama curl -s https://registry.ollama.ai`
- Check available disk space — model weights range from ~500 MB (tiny) to 40+ GB (large)
- Increase the pull timeout if needed; large models can take many minutes on a slow connection

### "File too large" on upload

Increase `max_file_size_mb` in **Config → General**. The default is 100 MB.

### Self-signed certificate warning in browser

Expected behaviour with a self-signed cert. Options:

- Accept the warning once in the browser for local use
- Upload a certificate signed by your internal CA via **Config → SSL → Upload**
- Add `certs/tls.crt` to your OS or browser trust store

### Database locked errors in logs

The application uses per-thread SQLite connections in WAL mode, which supports concurrent reads and serialised writes. Brief locking during simultaneous indexing jobs resolves automatically at the SQLite level without data loss.
