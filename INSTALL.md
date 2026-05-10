# Installation Guide

## Quick Start with Docker Compose

```bash
mkdir -p data documents
docker compose up -d --build
```

Open UI: `http://localhost:8080`

Default login:
- Username: `admin`
- Password: `admin`

## What Docker Compose starts
- `document-search` app container
- `mariadb` database container (for future/extended metadata integrations and infrastructure standardization)

> Current full-text index remains SQLite FTS5 (`/data/document_index.db`) for high-quality local full-text search. MariaDB is provisioned and ready in the stack.

## Initial configuration via UI
After login:
1. Open **Initial Configuration** card.
2. Load existing config.
3. Set extensions/exclusions/max file size.
4. Save configuration.
5. Start indexing `/documents`.

## Proxmox deployment
1. Create Debian 12 / Ubuntu 22.04 VM or LXC.
2. Install Docker Engine + Compose plugin.
3. Clone repo and run `docker compose up -d --build`.
4. Expose port `8080` via firewall/reverse proxy.

## Network share support (CIFS/NFS)
Mount the share on the host first and bind-mount into the container read-only.

Example (CIFS):
```bash
sudo mkdir -p /mnt/company_docs
sudo mount -t cifs //NAS-SERVER/Documents /mnt/company_docs \
  -o username=YOUR_USER,password=YOUR_PASS,uid=1000,gid=1000,iocharset=utf8
```

Then map it in compose as `/documents`.

## Security baseline
- Change `DOCUMENT_SEARCH_DEFAULT_PASSWORD` before first production start.
- Use TLS reverse proxy (Traefik/Caddy/Nginx) for remote access.
- Keep document mounts read-only.


## TLS / HTTPS (Self-signed)
`tls-proxy` automatically generates a self-signed certificate at first start for:
- DNS: `Seekr.domain.tld`
- IP: value from `TLS_IP` in compose (set this to your server IP)

Access:
- `https://Seekr.domain.tld:443`
- `https://<server-ip>:443`

Add DNS or local hosts entry for `Seekr.domain.tld` to your server IP.


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
