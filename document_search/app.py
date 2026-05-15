from __future__ import annotations
import json
import datetime as dt
import hashlib
import html
import ipaddress
import os
import posixpath
import re
import secrets
import sqlite3
import threading
import uuid
import time
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

try:
    import psutil as _psutil
except ImportError:
    _psutil = None  # type: ignore[assignment]

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from document_search.auth import verify_password
from document_search.config import AppConfig, load_config
from document_search.crawler import iter_documents
from document_search.extractors.docx_extractor import DocxTextExtractor
from document_search.extractors.md_extractor import MdTextExtractor
from document_search.extractors.legacy_office_extractor import LegacyOfficeTextExtractor
from document_search.extractors.pdf_extractor import PdfTextExtractor
from document_search.extractors.pptx_extractor import PptxTextExtractor
from document_search.extractors.txt_extractor import TxtTextExtractor
from document_search.index.search_service import search
from document_search.index.sqlite_store import SqliteStore
from document_search.services.ai_organizer import AiOrganizer
from document_search.services.file_service import fingerprint


# Singletons — instantiated once at import time, not on every request.
_EXTRACTORS: dict[str, object] = {
    ".pdf":  PdfTextExtractor(),
    ".docx": DocxTextExtractor(),
    ".pptx": PptxTextExtractor(),
    ".txt":  TxtTextExtractor(),
    ".md":   MdTextExtractor(),
    ".doc":  LegacyOfficeTextExtractor(),
    ".ppt":  LegacyOfficeTextExtractor(),
}

def extractor_for(ext: str):
    return _EXTRACTORS.get(ext)


# Thread-local store — one SQLite connection per OS thread (uvicorn worker thread).
# Avoids the cost of creating+migrating a new connection on every request.
_thread_local = threading.local()

# Login rate limiting — simple in-memory tracker (ip → list of failure timestamps).
_login_failures: dict[str, list[float]] = {}
_RATE_LIMIT_MAX = 10       # max failures
_RATE_LIMIT_WINDOW = 300   # seconds (5 min)

# Username / password validation.
_USERNAME_RE = re.compile(r'^[a-zA-Z0-9_\-.]{1,64}$')

# API key for Home Assistant and other service integrations (set via env var).
_API_KEY: str = os.getenv("DOCUMENT_SEARCH_API_KEY", "").strip()

# Background update job state.
_update_job: dict = {"status": "idle"}

# Path guard — filesystem roots that must never be indexed.
_BLOCKED_EXACT = {"/", "/proc", "/sys", "/dev"}
_BLOCKED_PREFIXES = ("/proc/", "/sys/", "/dev/")


def _check_api_key(key: str | None) -> bool:
    """Constant-time comparison to prevent timing attacks on the API key."""
    return bool(_API_KEY) and bool(key) and secrets.compare_digest(_API_KEY, key)


def highlight_terms(text: str, query: str) -> str:
    safe = html.escape(text)
    terms = [t for t in re.split(r"\s+", query) if t and t.upper() not in {"AND", "OR", "NOT"} and not t.startswith("-")]
    for term in sorted(set(terms), key=len, reverse=True):
        safe = re.sub(re.escape(html.escape(term)), f"<mark>{html.escape(term)}</mark>", safe, flags=re.IGNORECASE)
    return safe


class LoginRequest(BaseModel):
    username: str
    password: str


class IndexRequest(BaseModel):
    paths: list[str] = Field(min_length=1)
    config_path: str | None = None


class SearchRequest(BaseModel):
    query: str = ""
    tags: list[str] = Field(default_factory=list)
    limit: int = 20
    filetype: str | None = None
    path: str | None = None
    block_type: str | None = None
    modified_from: str | None = None
    modified_to: str | None = None



class SourcePath(BaseModel):
    path: str
    label: str = ""
    type: str = "local"
    mount_point: str | None = None


class UiConfigRequest(BaseModel):
    database_path: str
    supported_extensions: list[str]
    exclude_dirs: list[str]
    exclude_patterns: list[str]
    max_file_size_mb: int
    source_paths: list[SourcePath] = Field(default_factory=list)
    ollama_url: str | None = None
    ollama_model: str | None = None


class UserCreateRequest(BaseModel):
    username: str
    password: str
    role: str = "user"


class UserUpdateRequest(BaseModel):
    role: str


class ChangePasswordRequest(BaseModel):
    new_password: str


class PathTestRequest(BaseModel):
    path: str


class MountRequest(BaseModel):
    remote_path: str
    mount_point: str
    share_type: str = "smb"
    username: str | None = None
    password: str | None = None
    domain: str | None = None


class SslGenerateRequest(BaseModel):
    common_name: str = "seekr.local"
    days: int = 365
    country: str = "DE"
    org: str = "Seekr"
    san_hosts: list[str] = Field(default_factory=list)


class ReorganizeApplyItem(BaseModel):
    document_id: int
    new_subpath: str


class ReorganizeApplyRequest(BaseModel):
    moves: list[ReorganizeApplyItem]


class PullModelRequest(BaseModel):
    model: str | None = None


class HaKeyCreateRequest(BaseModel):
    label: str = Field(min_length=1, max_length=64)
    path_filter: str = Field(min_length=1)
    description: str = ""


class HaSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=5, ge=1, le=20)

class MarkRequest(BaseModel):
    document_id: int
    is_marked: bool = True


class TagsRequest(BaseModel):
    document_id: int
    tags: list[str]


@dataclass
class JobState:
    status: str
    found: int = 0
    indexed: int = 0
    skipped: int = 0
    updated: int = 0
    errors: int = 0
    done: int = 0


def _recommend_tier(available_ram_gb: float) -> dict:
    """Map free RAM to an Ollama model-size tier with concrete examples."""
    if available_ram_gb < 3:
        return {"tier": "tiny",   "max_size_gb": 2.0,  "description": "< 3 GB free RAM",  "examples": ["qwen2.5:0.5b", "tinyllama"]}
    if available_ram_gb < 6:
        return {"tier": "small",  "max_size_gb": 4.0,  "description": "3–6 GB free RAM",  "examples": ["llama3.2:1b", "qwen2.5:3b"]}
    if available_ram_gb < 10:
        return {"tier": "medium", "max_size_gb": 6.0,  "description": "6–10 GB free RAM", "examples": ["llama3.2", "qwen2.5:7b", "mistral"]}
    if available_ram_gb < 20:
        return {"tier": "large",  "max_size_gb": 12.0, "description": "10–20 GB free RAM", "examples": ["llama3.1:8b", "qwen2.5:14b"]}
    return {"tier": "xlarge", "max_size_gb": round(available_ram_gb * 0.75, 1),
            "description": f"{available_ram_gb:.0f} GB free RAM", "examples": ["llama3.1:70b", "qwen2.5:32b"]}


_OPENAPI_TAGS = [
    {"name": "auth",   "description": "Login and session"},
    {"name": "search", "description": "Full-text document search"},
    {"name": "index",  "description": "Crawl and indexing jobs"},
    {"name": "ha",     "description": "Home Assistant integration — authenticate with `X-Api-Key` header"},
    {"name": "ai",     "description": "Ollama AI operations"},
    {"name": "users",  "description": "User management (admin only)"},
    {"name": "config", "description": "Application configuration"},
    {"name": "system", "description": "System diagnostics and maintenance"},
    {"name": "update", "description": "Application update via git + Docker"},
    {"name": "ssl",    "description": "TLS certificate management"},
    {"name": "files",  "description": "File serving"},
]


def create_app(db_path: str = "./document_index.db") -> FastAPI:
    config_path = Path(os.getenv("DOCUMENT_SEARCH_CONFIG_PATH", "./config.json"))
    ssl_dir = Path(os.getenv("DOCUMENT_SEARCH_SSL_DIR", "/data/ssl"))
    app = FastAPI(
        title="Seekr",
        description=(
            "Self-hosted document search with full-text indexing and Home Assistant integration.\n\n"
            "**Home Assistant endpoints** (`/api/ha/*`) authenticate with the `X-Api-Key` header — "
            "no session token required. Create keys via Config → Home Assistant."
        ),
        version="1.5.0",
        openapi_tags=_OPENAPI_TAGS,
    )
    templates = Jinja2Templates(directory="document_search/web/templates")
    app.mount("/static", StaticFiles(directory="document_search/web/static"), name="static")
    sessions: dict[str, tuple[int, float, str]] = {}
    jobs: dict[str, JobState] = {}
    ai_jobs: dict[str, dict] = {}
    upload_root = Path(os.getenv("DOCUMENT_SEARCH_UPLOAD_ROOT", "/documents/uploads"))
    organizer = AiOrganizer()

    def store() -> SqliteStore:
        """Return a per-thread SqliteStore, creating it once on first use."""
        if not getattr(_thread_local, "initialized", False):
            _thread_local.db = SqliteStore(Path(db_path))
            _thread_local.db.ensure_default_admin()
            _thread_local.initialized = True
        return _thread_local.db

    def load_effective_config() -> AppConfig:
        if config_path.exists():
            return load_config(config_path)
        return AppConfig()

    # ── HA key helpers ─────────────────────────────────────────────────

    def _load_ha_keys() -> list[dict]:
        if not config_path.exists():
            return []
        try:
            return json.loads(config_path.read_text(encoding="utf-8")).get("ha_api_keys", [])
        except Exception:
            return []

    def _save_ha_keys(keys: list[dict]) -> None:
        raw: dict = {}
        if config_path.exists():
            try:
                raw = json.loads(config_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        raw["ha_api_keys"] = keys
        config_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")

    def _resolve_ha_key(key_value: str | None) -> dict | None:
        """Return the matching key config, or None if invalid.
        Global env-var key maps to unrestricted access (path_filter=None)."""
        if not key_value:
            return None
        if _check_api_key(key_value):
            return {"id": "__global__", "label": "Global key", "path_filter": None}
        for k in _load_ha_keys():
            stored = k.get("key", "")
            if stored and secrets.compare_digest(stored, key_value):
                return k
        return None

    # ── Security headers ───────────────────────────────────────────────
    class _SecurityHeaders(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next) -> Response:
            response = await call_next(request)
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["X-XSS-Protection"] = "1; mode=block"
            response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
            response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
            # Swagger UI (/docs, /redoc) loads assets from CDN — skip strict CSP there.
            if request.url.path not in ("/docs", "/redoc", "/openapi.json"):
                response.headers["Content-Security-Policy"] = (
                    "default-src 'self'; "
                    "script-src 'self' 'unsafe-inline'; "
                    "style-src 'self' 'unsafe-inline'; "
                    "img-src 'self' data:; "
                    "connect-src 'self'; "
                    "font-src 'self'; "
                    "frame-ancestors 'none'; "
                    "base-uri 'self'; "
                    "form-action 'self'"
                )
            if request.url.path.startswith("/static/"):
                response.headers["Cache-Control"] = "public, max-age=3600, immutable"
            else:
                response.headers["Cache-Control"] = "no-store"
            return response

    app.add_middleware(_SecurityHeaders)

    # ── Auth helpers ───────────────────────────────────────────────────
    def _check_rate_limit(ip: str) -> None:
        now = time.time()
        recent = [t for t in _login_failures.get(ip, []) if now - t < _RATE_LIMIT_WINDOW]
        _login_failures[ip] = recent
        if len(recent) >= _RATE_LIMIT_MAX:
            raise HTTPException(status_code=429, detail="Too many failed login attempts. Try again in 5 minutes.")

    def _record_failure(ip: str) -> None:
        _login_failures.setdefault(ip, []).append(time.time())

    def _clear_failures(ip: str) -> None:
        _login_failures.pop(ip, None)

    def _validate_username(username: str) -> None:
        if not _USERNAME_RE.match(username):
            raise HTTPException(status_code=400, detail="Username must be 1–64 characters: letters, digits, _ - . only")

    def _validate_password(password: str) -> None:
        if len(password) < 8:
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    def _validate_ollama_url(url: str) -> None:
        if not url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="ollama_url must start with http:// or https://")

    def require_user(token: str | None) -> int:
        if not token or token not in sessions:
            raise HTTPException(status_code=401, detail="Unauthorized")
        user_id, issued, _role = sessions[token]
        if time.time() - issued > 60 * 60 * 8:
            sessions.pop(token, None)
            raise HTTPException(status_code=401, detail="Session expired")
        return user_id

    def require_admin(token: str | None) -> int:
        if not token or token not in sessions:
            raise HTTPException(status_code=401, detail="Unauthorized")
        user_id, issued, role = sessions[token]
        if time.time() - issued > 60 * 60 * 8:
            sessions.pop(token, None)
            raise HTTPException(status_code=401, detail="Session expired")
        if role != "admin":
            raise HTTPException(status_code=403, detail="Admin role required")
        return user_id

    @app.get("/", response_class=HTMLResponse)
    def index_page(request: Request):
        return templates.TemplateResponse("index.html", {"request": request})

    @app.get("/config", response_class=HTMLResponse)
    def config_page(request: Request):
        return templates.TemplateResponse("config.html", {"request": request})

    @app.get("/search", response_class=HTMLResponse)
    def search_page(request: Request):
        return templates.TemplateResponse("search.html", {"request": request})

    @app.get("/ingest", response_class=HTMLResponse)
    def ingest_page(request: Request):
        return templates.TemplateResponse("ingest.html", {"request": request})

    @app.get("/wiki", response_class=HTMLResponse)
    def wiki_page(request: Request):
        return templates.TemplateResponse("wiki.html", {"request": request})


    @app.get("/api/config")
    def api_get_config(x_auth_token: str | None = Header(default=None)):
        require_user(x_auth_token)
        defaults: dict = {
            "database_path": db_path,
            "supported_extensions": [".pdf", ".docx", ".pptx", ".txt"],
            "exclude_dirs": [".git", "node_modules", "__pycache__", ".venv", "temp"],
            "exclude_patterns": ["~$*", "*.tmp"],
            "max_file_size_mb": 100,
            "source_paths": [],
            "ollama_url": organizer.base_url,
            "ollama_model": organizer.model,
        }
        if config_path.exists():
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            defaults.update(saved)
        # Always reflect live in-memory values (env-var overrides survive restart)
        defaults["ollama_url"] = organizer.base_url
        defaults["ollama_model"] = organizer.model
        return defaults

    @app.post("/api/config")
    def api_save_config(req: UiConfigRequest, x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        if req.ollama_url:
            _validate_ollama_url(req.ollama_url)
        config_path.write_text(json.dumps(req.model_dump(), indent=2), encoding="utf-8")
        if req.ollama_url:
            organizer.base_url = req.ollama_url.rstrip("/")
        if req.ollama_model:
            organizer.model = req.ollama_model
        return {"status": "saved", "path": str(config_path)}

    @app.post("/api/login")
    def api_login(req: LoginRequest, request: Request):
        ip = request.client.host if request.client else "unknown"
        _check_rate_limit(ip)
        db = store()
        user = db.get_user(req.username)
        if not user or not verify_password(req.password, user["salt"], user["password_hash"]):
            _record_failure(ip)
            raise HTTPException(status_code=401, detail="Invalid credentials")
        _clear_failures(ip)
        token = uuid.uuid4().hex
        role = user["role"] if "role" in user.keys() else "user"
        sessions[token] = (user["id"], time.time(), role)
        return {"token": token, "username": user["username"], "role": role}

    @app.get("/api/me")
    def api_me(x_auth_token: str | None = Header(default=None)):
        if not x_auth_token or x_auth_token not in sessions:
            raise HTTPException(status_code=401, detail="Unauthorized")
        user_id, issued, role = sessions[x_auth_token]
        if time.time() - issued > 60 * 60 * 8:
            sessions.pop(x_auth_token, None)
            raise HTTPException(status_code=401, detail="Session expired")
        db = store()
        user = db.get_user_by_id(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return {"id": user_id, "username": user["username"], "role": role}

    @app.post("/api/documents/mark")
    def api_mark(req: MarkRequest, x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        db = store()
        if not db.get_document_by_id(req.document_id):
            raise HTTPException(status_code=404, detail="Document not found")
        db.set_mark(user_id, req.document_id, req.is_marked)
        return {"status": "ok"}

    @app.post("/api/documents/tags")
    def api_tags(req: TagsRequest, x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        db = store()
        if not db.get_document_by_id(req.document_id):
            raise HTTPException(status_code=404, detail="Document not found")
        db.set_tags(user_id, req.document_id, req.tags)
        return {"status": "ok"}

    @app.get("/api/tags")
    def api_list_tags(x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        db = store()
        return db.get_user_tags(user_id)

    @app.post("/api/documents/{document_id}/reindex")
    def api_reindex_document(document_id: int, x_auth_token: str | None = Header(default=None)):
        require_user(x_auth_token)
        db = store()
        doc = db.get_document_by_id(document_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        p = Path(doc["path"])
        if not p.exists():
            raise HTTPException(status_code=404, detail="File no longer exists on disk")
        extractor = extractor_for(p.suffix.lower())
        if extractor is None:
            raise HTTPException(status_code=400, detail=f"No extractor for extension: {p.suffix}")
        fp = fingerprint(p)
        result = extractor.extract(p)
        db.upsert_document(fp, result)
        return {"status": "reindexed", "document_id": document_id, "blocks": len(result.blocks), "extraction_status": result.status}

    @app.post("/api/index/cleanup")
    def api_index_cleanup(x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        db = store()
        removed = db.remove_missing()
        return {"removed": removed}

    @app.post("/api/ai/suggest-structure")
    def api_ai_suggest_structure(
        sample_size: int = 50,
        x_auth_token: str | None = Header(default=None),
    ):
        require_user(x_auth_token)
        job_id = uuid.uuid4().hex
        ai_jobs[job_id] = {"status": "running", "result": None}

        def runner():
            db = store()
            rows = db.conn.execute(
                """
                SELECT d.id, d.filename, d.extension, d.path,
                       GROUP_CONCAT(ut.name, ', ') AS tags
                FROM documents d
                LEFT JOIN document_tags dt ON dt.document_id = d.id
                LEFT JOIN user_tags ut ON ut.id = dt.tag_id
                GROUP BY d.id
                ORDER BY RANDOM()
                LIMIT ?
                """,
                (min(sample_size, 100),),
            ).fetchall()
            result = organizer.suggest_structure([dict(r) for r in rows])
            ai_jobs[job_id]["status"] = "finished"
            ai_jobs[job_id]["result"] = result

        threading.Thread(target=runner, daemon=True).start()
        return {"job_id": job_id}

    @app.post("/api/upload")
    async def api_upload(
        x_auth_token: str | None = Header(default=None),
        file: UploadFile = File(...),
        target_subpath: str = Form(default=""),
        tags: str = Form(default=""),
        metadata_json: str = Form(default="{}"),
    ):
        user_id = require_user(x_auth_token)
        safe_name = Path(file.filename or "upload.bin").name
        ext = Path(safe_name).suffix.lower()
        allowed = {".pdf", ".docx", ".pptx", ".txt", ".md", ".doc", ".ppt"}
        if ext not in allowed:
            raise HTTPException(status_code=400, detail=f"Unsupported file extension: {ext}")

        if len(metadata_json) > 8192:
            raise HTTPException(status_code=400, detail="metadata_json exceeds 8 KB limit")

        target = (upload_root / target_subpath).resolve()
        if upload_root.resolve() not in target.parents and target != upload_root.resolve():
            raise HTTPException(status_code=400, detail="Invalid target_subpath")
        target.mkdir(parents=True, exist_ok=True)

        content = await file.read()
        cfg = load_effective_config()
        max_bytes = cfg.max_file_size_mb * 1024 * 1024
        if len(content) > max_bytes:
            raise HTTPException(status_code=413, detail=f"File exceeds {cfg.max_file_size_mb} MB limit")

        digest = hashlib.sha256(content).hexdigest()[:8]
        out = target / f"{Path(safe_name).stem}_{digest}{ext}"
        out.write_bytes(content)

        metadata = json.loads(metadata_json or "{}")
        sidecar = out.with_suffix(out.suffix + ".meta.json")
        sidecar.write_text(json.dumps({"tags": tags, "metadata": metadata}, indent=2), encoding="utf-8")

        db = store()
        fp = fingerprint(out)
        extractor = extractor_for(ext)
        result = extractor.extract(out) if extractor else None

        extracted_text = ""
        if result and result.blocks:
            extracted_text = " ".join(b.text[:500] for b in result.blocks[:6])

        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        suggestion = organizer.suggest(
            file_path=out,
            extracted_text=extracted_text,
            tags=tag_list,
            metadata={k: str(v) for k, v in metadata.items()},
        )

        if result:
            doc_id = db.upsert_document(fp, result)
            if tag_list:
                db.set_tags(user_id, doc_id, tag_list)
        else:
            doc_id = None

        return {"status": "uploaded", "path": str(out), "document_id": doc_id, "ai_suggestion": suggestion.__dict__}

    @app.get("/api/folders")
    def api_folders(x_auth_token: str | None = Header(default=None)):
        require_user(x_auth_token)
        root = str(upload_root)
        if not os.path.isdir(root):
            return []
        results = []
        for dirpath, dirnames, _ in os.walk(root):
            depth = os.path.relpath(dirpath, root).count(os.sep)
            if depth >= 2:
                dirnames.clear()
                continue
            rel = os.path.relpath(dirpath, root)
            if rel != ".":
                results.append(rel)
        return results

    @app.get("/api/source-folders")
    def api_source_folders(x_auth_token: str | None = Header(default=None)):
        require_user(x_auth_token)
        raw_source_paths: list[dict] = []
        if config_path.exists():
            try:
                raw_source_paths = json.loads(config_path.read_text(encoding="utf-8")).get("source_paths", [])
                if not isinstance(raw_source_paths, list):
                    raw_source_paths = []
            except Exception:
                pass
        results = []
        for sp in raw_source_paths:
            path = sp.get("path", "")
            label = sp.get("label") or os.path.basename(path.rstrip("/\\"))
            if not path or not os.path.isdir(path):
                continue
            results.append({"path": path, "label": label, "is_root": True})
            try:
                for entry in sorted(os.scandir(path), key=lambda e: e.name):
                    if entry.is_dir():
                        results.append({
                            "path": entry.path,
                            "label": entry.name,
                            "is_root": False,
                        })
            except PermissionError:
                pass
        return results

    @app.post("/api/index/start")
    def api_index_start(req: IndexRequest, x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        for p in req.paths:
            # Normalize to canonical POSIX path and collapse any leading double-slashes
            norm = "/" + posixpath.normpath(p).lstrip("/")
            if norm in _BLOCKED_EXACT or norm.startswith(_BLOCKED_PREFIXES):
                raise HTTPException(
                    status_code=400,
                    detail=f"Path '{p}' is not allowed. Select a specific subdirectory.",
                )
        cfg: AppConfig = load_config(Path(req.config_path)) if req.config_path else load_effective_config()
        job_id = uuid.uuid4().hex
        jobs[job_id] = JobState(status="running")

        def runner():
            db = store()
            for path in iter_documents([Path(p) for p in req.paths], cfg):
                j = jobs[job_id]
                j.found += 1
                fp = fingerprint(path)
                existing = db.get_document(str(fp.path))
                if existing and existing["sha256"] == fp.sha256 and existing["modified_at"] == fp.modified_at.isoformat():
                    j.skipped += 1
                    j.done += 1
                    continue
                extractor = extractor_for(path.suffix.lower())
                if extractor is None:
                    j.done += 1
                    continue
                result = extractor.extract(path)
                db.upsert_document(fp, result)
                if result.status == "error":
                    j.errors += 1
                elif existing:
                    j.updated += 1
                else:
                    j.indexed += 1
                j.done += 1
            jobs[job_id].status = "finished"

        threading.Thread(target=runner, daemon=True).start()
        return {"job_id": job_id}

    @app.get("/api/index/jobs/{job_id}")
    def api_index_job(job_id: str, x_auth_token: str | None = Header(default=None)):
        require_user(x_auth_token)
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return job.__dict__

    @app.get("/api/index/extensions")
    def api_index_extensions(x_auth_token: str | None = Header(default=None)):
        require_user(x_auth_token)
        db = store()
        rows = db.conn.execute(
            "SELECT extension, COUNT(*) AS cnt FROM documents "
            "GROUP BY extension ORDER BY cnt DESC"
        ).fetchall()
        return [row["extension"] for row in rows if row["extension"]]



    @app.get("/api/update/check")
    def api_check_update(x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        if os.getenv("DOCUMENT_SEARCH_UI_UPDATE_ENABLED", "true").lower() != "true":
            raise HTTPException(status_code=403, detail="UI update disabled")

        current_commit: str | None = None
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True, check=False,
                cwd="/app", timeout=10,
            )
            if proc.returncode == 0:
                current_commit = proc.stdout.strip()
        except Exception:
            pass
        if not current_commit:
            current_commit = os.getenv("GIT_COMMIT")

        latest_commit: str | None = None
        check_error: str | None = None
        import urllib.request as _ur
        import urllib.error as _ue
        try:
            req = _ur.Request(
                "https://api.github.com/repos/harpf/Seekr/commits/main",
                headers={
                    "Accept": "application/vnd.github.sha",
                    "User-Agent": "Seekr-update-check/1.0",
                },
            )
            with _ur.urlopen(req, timeout=10) as r:
                latest_commit = r.read().decode().strip()
        except Exception as exc:
            check_error = str(exc)

        update_available: bool | None = None
        if current_commit and latest_commit:
            update_available = current_commit != latest_commit

        return {
            "current_commit": current_commit,
            "latest_commit": latest_commit,
            "update_available": update_available,
            "app_version": app.version,
            "error": check_error,
        }

    @app.post("/api/update/run")
    def api_run_update(x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        if os.getenv("DOCUMENT_SEARCH_UI_UPDATE_ENABLED", "true").lower() != "true":
            raise HTTPException(status_code=403, detail="UI update disabled")
        script = Path("/app/scripts/update.sh")
        if not script.exists():
            raise HTTPException(status_code=404, detail="Update script not found")

        job_id = uuid.uuid4().hex
        _update_job.clear()
        _update_job.update({"job_id": job_id, "status": "running", "stdout": "", "stderr": "", "exit_code": None})

        def _runner():
            proc = subprocess.run(["/bin/sh", str(script)], capture_output=True, text=True, check=False)
            _update_job.update({
                "status": "done" if proc.returncode == 0 else "error",
                "exit_code": proc.returncode,
                "stdout": proc.stdout[-4000:],
                "stderr": proc.stderr[-4000:],
            })

        threading.Thread(target=_runner, daemon=True).start()
        return {"job_id": job_id, "status": "started"}

    @app.get("/api/update/status")
    def api_update_status(x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        return dict(_update_job)

    # ── Home Assistant integration ─────────────────────────────────────

    @app.get("/api/ha/status", tags=["ha"])
    def api_ha_status(x_api_key: str | None = Header(default=None)):
        key_cfg = _resolve_ha_key(x_api_key)
        if not key_cfg:
            raise HTTPException(
                status_code=401,
                detail="Invalid or missing API key. Configure one via Config → Home Assistant.",
            )
        db = store()
        doc_count = db.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        block_count = db.conn.execute("SELECT COUNT(*) FROM content_blocks").fetchone()[0]
        total_size = db.conn.execute("SELECT COALESCE(SUM(file_size), 0) FROM documents").fetchone()[0]
        return {
            "state": "online",
            "documents": doc_count,
            "content_blocks": block_count,
            "total_file_size_bytes": total_size,
            "app_version": app.version,
        }

    @app.get("/api/ha/test", tags=["ha"])
    def api_ha_test(x_api_key: str | None = Header(default=None)):
        """Connectivity probe — returns 200 even on auth failure so HA can show a clear error."""
        key_cfg = _resolve_ha_key(x_api_key)
        if not key_cfg:
            return {
                "connected": False,
                "error": "Invalid or missing API key. Create one via Config → Home Assistant.",
            }
        try:
            db = store()
            doc_count = db.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            return {
                "connected": True,
                "key_label": key_cfg.get("label", ""),
                "path_filter": key_cfg.get("path_filter"),
                "documents": doc_count,
                "app_version": app.version,
            }
        except Exception as e:
            return {"connected": False, "error": str(e)}

    def _ha_search_impl(query: str, limit: int, x_api_key: str | None) -> dict:
        key_cfg = _resolve_ha_key(x_api_key)
        if not key_cfg:
            raise HTTPException(status_code=401, detail="Invalid or missing API key")
        path_filter: str | None = key_cfg.get("path_filter")
        db = store()
        rows = search(db, query, limit, None, path_filter, None, None, None, None, None)
        results = [
            {
                "filename": r["filename"],
                "path": r["path"],
                "extension": r["extension"],
                "modified_at": (r["modified_at"] or "")[:10],
                "snippet": r["snippet"] or "",
                "block_type": r["block_type"],
            }
            for r in rows
        ]

        # Try AI-generated answer; fall back to first snippet excerpt.
        answer: str | None = None
        if results:
            context_parts = [
                f"Source: {r['filename']} ({r['path']})\n{r['snippet']}"
                for r in results[:3]
                if r.get("snippet")
            ]
            if context_parts and organizer.is_available():
                answer = organizer.ask(query, "\n\n".join(context_parts))
            if not answer and results[0].get("snippet"):
                answer = f"Found in {results[0]['filename']}: {results[0]['snippet'][:300]}"

        sources = [
            {"filename": r["filename"], "path": r["path"], "modified_at": r["modified_at"]}
            for r in results
        ]

        return {
            "query": query,
            "key_label": key_cfg.get("label", ""),
            "path_filter": path_filter,
            "count": len(results),
            "answer": answer,
            "sources": sources,
            "results": results,
        }

    @app.post("/api/ha/search", tags=["ha"])
    def api_ha_search_post(req: HaSearchRequest, x_api_key: str | None = Header(default=None)):
        return _ha_search_impl(req.query, req.limit, x_api_key)

    @app.get("/api/ha/search", tags=["ha"])
    def api_ha_search_get(
        query: str,
        limit: int = 5,
        x_api_key: str | None = Header(default=None),
    ):
        limit = max(1, min(limit, 20))
        return _ha_search_impl(query, limit, x_api_key)

    @app.get("/api/ha/keys", tags=["ha"])
    def api_ha_list_keys(x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        return _load_ha_keys()

    @app.post("/api/ha/keys", tags=["ha"])
    def api_ha_create_key(req: HaKeyCreateRequest, x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        path_filter = req.path_filter.strip()
        if not path_filter:
            raise HTTPException(status_code=400, detail="path_filter must not be empty")
        new_key: dict = {
            "id": uuid.uuid4().hex[:8],
            "label": req.label,
            "path_filter": path_filter,
            "description": req.description,
            "key": secrets.token_hex(32),
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        keys = _load_ha_keys()
        keys.append(new_key)
        _save_ha_keys(keys)
        return new_key

    @app.delete("/api/ha/keys/{key_id}", tags=["ha"])
    def api_ha_delete_key(key_id: str, x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        keys = [k for k in _load_ha_keys() if k.get("id") != key_id]
        _save_ha_keys(keys)
        return {"status": "deleted", "id": key_id}

    @app.post("/api/ha/index", tags=["ha"])
    def api_ha_index(x_api_key: str | None = Header(default=None)):
        key_cfg = _resolve_ha_key(x_api_key)
        if not key_cfg:
            raise HTTPException(status_code=401, detail="Invalid or missing API key")
        cfg = load_effective_config()
        raw_cfg: dict = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        paths = [sp["path"] for sp in raw_cfg.get("source_paths", []) if sp.get("path")]
        if not paths:
            raise HTTPException(status_code=400, detail="No source paths configured in config.json")
        job_id = uuid.uuid4().hex
        jobs[job_id] = JobState(status="running")

        def runner():
            db = store()
            for path in iter_documents([Path(p) for p in paths], cfg):
                j = jobs[job_id]
                j.found += 1
                fp = fingerprint(path)
                existing = db.get_document(str(fp.path))
                if existing and existing["sha256"] == fp.sha256 and existing["modified_at"] == fp.modified_at.isoformat():
                    j.skipped += 1
                    j.done += 1
                    continue
                extractor = extractor_for(path.suffix.lower())
                if extractor is None:
                    j.done += 1
                    continue
                result = extractor.extract(path)
                db.upsert_document(fp, result)
                if result.status == "error":
                    j.errors += 1
                elif existing:
                    j.updated += 1
                else:
                    j.indexed += 1
                j.done += 1
            jobs[job_id].status = "finished"

        threading.Thread(target=runner, daemon=True).start()
        return {"job_id": job_id, "paths": paths}

    @app.get("/api/system/dependencies")
    def api_dependencies(x_auth_token: str | None = Header(default=None)):
        require_user(x_auth_token)
        tools = ["antiword", "catppt", "tesseract", "pdftoppm"]
        return {tool: bool(shutil.which(tool)) for tool in tools}

    # ── User management ────────────────────────────────────────────────

    @app.get("/api/users")
    def api_list_users(x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        db = store()
        return db.list_users()

    @app.post("/api/users")
    def api_create_user(req: UserCreateRequest, x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        if req.role not in ("admin", "user"):
            raise HTTPException(status_code=400, detail="Role must be 'admin' or 'user'")
        _validate_username(req.username)
        _validate_password(req.password)
        db = store()
        try:
            user_id = db.create_user(req.username, req.password, req.role)
            return {"id": user_id, "username": req.username, "role": req.role}
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.put("/api/users/{user_id}")
    def api_update_user(user_id: int, req: UserUpdateRequest, x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        if req.role not in ("admin", "user"):
            raise HTTPException(status_code=400, detail="Role must be 'admin' or 'user'")
        db = store()
        if not db.get_user_by_id(user_id):
            raise HTTPException(status_code=404, detail="User not found")
        db.update_user_role(user_id, req.role)
        return {"status": "updated"}

    @app.delete("/api/users/{user_id}")
    def api_delete_user(user_id: int, x_auth_token: str | None = Header(default=None)):
        admin_id = require_admin(x_auth_token)
        if user_id == admin_id:
            raise HTTPException(status_code=400, detail="Cannot delete your own account")
        db = store()
        if not db.get_user_by_id(user_id):
            raise HTTPException(status_code=404, detail="User not found")
        db.delete_user(user_id)
        return {"status": "deleted"}

    @app.post("/api/users/{user_id}/change-password")
    def api_change_password(user_id: int, req: ChangePasswordRequest, x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        _validate_password(req.new_password)
        db = store()
        if not db.get_user_by_id(user_id):
            raise HTTPException(status_code=404, detail="User not found")
        db.change_password(user_id, req.new_password)
        return {"status": "password changed"}

    # ── Path test & network mount ──────────────────────────────────────

    @app.post("/api/paths/test")
    def api_path_test(req: PathTestRequest, x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        p = Path(req.path)
        exists = p.exists()
        readable = False
        writable = False
        entry_count: int | None = None
        if exists and p.is_dir():
            try:
                entries = list(p.iterdir())
                readable = True
                entry_count = len(entries)
            except PermissionError:
                pass
            try:
                test_file = p / f".seekr_write_test_{uuid.uuid4().hex[:6]}"
                test_file.touch()
                test_file.unlink()
                writable = True
            except Exception:
                pass
        elif exists and p.is_file():
            readable = os.access(p, os.R_OK)
        return {
            "path": req.path,
            "exists": exists,
            "is_dir": p.is_dir() if exists else False,
            "readable": readable,
            "writable": writable,
            "entry_count": entry_count,
        }

    @app.post("/api/paths/mount")
    def api_path_mount(req: MountRequest, x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        if os.name == "nt":
            raise HTTPException(status_code=400, detail="Mount via API not supported on Windows; use Docker volume mounts instead")
        mount_point = Path(req.mount_point)
        try:
            mount_point.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Cannot create mount point: {e}")

        if req.share_type == "smb":
            options = ["vers=3.0"]
            if req.username:
                options.append(f"username={req.username}")
            if req.password:
                options.append(f"password={req.password}")
            if req.domain:
                options.append(f"domain={req.domain}")
            cmd = ["mount", "-t", "cifs", req.remote_path, str(mount_point), "-o", ",".join(options)]
        elif req.share_type == "nfs":
            cmd = ["mount", "-t", "nfs", req.remote_path, str(mount_point)]
        else:
            raise HTTPException(status_code=400, detail=f"Unknown share type: {req.share_type}. Use 'smb' or 'nfs'")

        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
        return {
            "mounted": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "mount_point": str(mount_point),
        }

    @app.post("/api/paths/unmount")
    def api_path_unmount(req: PathTestRequest, x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        if os.name == "nt":
            raise HTTPException(status_code=400, detail="Unmount not supported on Windows")
        proc = subprocess.run(["umount", req.path], capture_output=True, text=True, check=False, timeout=30)
        return {
            "unmounted": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }

    # ── Database test ──────────────────────────────────────────────────

    @app.get("/api/system/db-test")
    def api_db_test(x_auth_token: str | None = Header(default=None)):
        require_user(x_auth_token)
        try:
            db = store()
            doc_count = db.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            user_count = db.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            block_count = db.conn.execute("SELECT COUNT(*) FROM content_blocks").fetchone()[0]
            db_path_obj = Path(db_path)
            db_size = db_path_obj.stat().st_size if db_path_obj.exists() else 0
            integrity = db.conn.execute("PRAGMA integrity_check").fetchone()[0]
            return {
                "ok": True,
                "documents": doc_count,
                "users": user_count,
                "content_blocks": block_count,
                "db_path": db_path,
                "db_size_bytes": db_size,
                "integrity": integrity,
                "journal_mode": db.conn.execute("PRAGMA journal_mode").fetchone()[0],
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── SSL / Certificate management ───────────────────────────────────

    @app.get("/api/ssl/status")
    def api_ssl_status(x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        cert_path = ssl_dir / "cert.pem"
        key_path = ssl_dir / "key.pem"
        if not cert_path.exists():
            return {"configured": False, "cert_path": str(cert_path), "key_path": str(key_path)}
        try:
            from cryptography import x509
            from cryptography.hazmat.backends import default_backend
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes(), default_backend())
            return {
                "configured": True,
                "cert_path": str(cert_path),
                "key_path": str(key_path),
                "key_exists": key_path.exists(),
                "subject": cert.subject.rfc4514_string(),
                "issuer": cert.issuer.rfc4514_string(),
                "not_before": cert.not_valid_before_utc.isoformat(),
                "not_after": cert.not_valid_after_utc.isoformat(),
                "serial": str(cert.serial_number),
            }
        except ImportError:
            return {"configured": True, "cert_path": str(cert_path), "key_path": str(key_path), "error": "cryptography package not installed"}
        except Exception as e:
            return {"configured": True, "cert_path": str(cert_path), "key_path": str(key_path), "error": str(e)}

    @app.post("/api/ssl/generate")
    def api_ssl_generate(req: SslGenerateRequest, x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        try:
            from cryptography import x509
            from cryptography.hazmat.backends import default_backend
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.x509.oid import NameOID

            key = rsa.generate_private_key(public_exponent=65537, key_size=2048, backend=default_backend())
            subject = issuer = x509.Name([
                x509.NameAttribute(NameOID.COUNTRY_NAME, (req.country or "DE")[:2]),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, req.org or "Seekr"),
                x509.NameAttribute(NameOID.COMMON_NAME, req.common_name),
            ])

            san_list: list = [x509.DNSName(req.common_name)]
            for host in req.san_hosts:
                try:
                    san_list.append(x509.IPAddress(ipaddress.ip_address(host)))
                except ValueError:
                    san_list.append(x509.DNSName(host))

            now_utc = dt.datetime.now(dt.timezone.utc)
            cert = (
                x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(issuer)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now_utc)
                .not_valid_after(now_utc + dt.timedelta(days=req.days))
                .add_extension(x509.SubjectAlternativeName(san_list), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .sign(key, hashes.SHA256(), default_backend())
            )

            ssl_dir.mkdir(parents=True, exist_ok=True)
            cert_path = ssl_dir / "cert.pem"
            key_path = ssl_dir / "key.pem"
            cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            ))
            if os.name != "nt":
                key_path.chmod(0o600)

            return {
                "ok": True,
                "cert_path": str(cert_path),
                "key_path": str(key_path),
                "common_name": req.common_name,
                "not_after": (now_utc + dt.timedelta(days=req.days)).isoformat(),
            }
        except ImportError:
            raise HTTPException(status_code=501, detail="cryptography package not installed")
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # ── AI / Ollama ────────────────────────────────────────────────────

    @app.get("/api/ai/status")
    def api_ai_status(x_auth_token: str | None = Header(default=None)):
        require_user(x_auth_token)
        available = organizer.is_available()
        models: list[str] = []
        if available:
            models = organizer.list_models()
        return {
            "available": available,
            "base_url": organizer.base_url,
            "configured_model": organizer.model,
            "models": models,
        }

    @app.post("/api/ai/models/pull")
    def api_ai_pull_model(req: PullModelRequest, x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        job_id = uuid.uuid4().hex
        ai_jobs[job_id] = {"status": "pulling", "model": req.model or organizer.model, "result": None}

        def runner():
            result = organizer.pull_model(req.model)
            ai_jobs[job_id]["status"] = "done" if result["ok"] else "error"
            ai_jobs[job_id]["result"] = result

        threading.Thread(target=runner, daemon=True).start()
        return {"job_id": job_id, "model": req.model or organizer.model}

    @app.get("/api/ai/system-info")
    def api_ai_system_info(x_auth_token: str | None = Header(default=None)):
        require_user(x_auth_token)

        # RAM / CPU via psutil
        ram_total_gb: float | None = None
        ram_available_gb: float | None = None
        cpu_cores: int | None = None
        if _psutil:
            mem = _psutil.virtual_memory()
            ram_total_gb = round(mem.total / 1024 ** 3, 2)
            ram_available_gb = round(mem.available / 1024 ** 3, 2)
            cpu_cores = _psutil.cpu_count(logical=False) or _psutil.cpu_count()

        # GPU via nvidia-smi (optional)
        gpu_info: list[dict] | None = None
        try:
            proc = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, check=False, timeout=5,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                gpu_info = []
                for line in proc.stdout.strip().splitlines():
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 3:
                        gpu_info.append({
                            "name": parts[0],
                            "vram_total_mb": int(parts[1]) if parts[1].isdigit() else None,
                            "vram_free_mb": int(parts[2]) if parts[2].isdigit() else None,
                        })
        except Exception:
            pass

        # Models from Ollama with sizes
        models: list[dict] = []
        import urllib.request as _ur
        import urllib.error as _ue
        try:
            with _ur.urlopen(f"{organizer.base_url}/api/tags", timeout=5) as r:
                data = json.loads(r.read())
                for m in data.get("models", []):
                    size_bytes = m.get("size", 0)
                    models.append({
                        "name": m["name"],
                        "size_bytes": size_bytes,
                        "size_gb": round(size_bytes / 1024 ** 3, 2),
                        "modified": m.get("modified_at", "")[:10],
                    })
        except Exception:
            pass

        # Tier recommendation + fit label per model
        recommendation = _recommend_tier(ram_available_gb) if ram_available_gb is not None else None
        if recommendation:
            max_gb = recommendation["max_size_gb"]
            for m in models:
                sg = m["size_gb"]
                m["fit"] = "ok" if sg <= max_gb * 0.85 else ("warn" if sg <= max_gb * 1.1 else "too-large")

        # Currently loaded models
        running = organizer.get_running_models()

        return {
            "ram_total_gb": ram_total_gb,
            "ram_available_gb": ram_available_gb,
            "cpu_cores": cpu_cores,
            "gpu": gpu_info,
            "models": models,
            "running_models": [r.get("name") for r in running],
            "recommendation": recommendation,
            "configured_model": organizer.model,
            "ollama_url": organizer.base_url,
            "ollama_available": organizer.is_available(),
        }

    @app.post("/api/ai/test-connection")
    def api_ai_test_connection(x_auth_token: str | None = Header(default=None)):
        require_user(x_auth_token)
        start = time.time()
        result = organizer.test_connection()
        result["total_ms"] = round((time.time() - start) * 1000)
        return result

    @app.delete("/api/ai/models/{model_name:path}")
    def api_ai_delete_model(model_name: str, x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        return organizer.delete_model(model_name)

    @app.get("/api/ai/jobs/{job_id}")
    def api_ai_job(job_id: str, x_auth_token: str | None = Header(default=None)):
        require_user(x_auth_token)
        job = ai_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="AI job not found")
        return job

    @app.post("/api/ai/reorganize/start")
    def api_ai_reorganize_start(
        limit: int = 10,
        x_auth_token: str | None = Header(default=None),
    ):
        require_admin(x_auth_token)
        job_id = uuid.uuid4().hex
        ai_jobs[job_id] = {"status": "running", "results": [], "total": 0, "done": 0}
        upload_root_str = str(upload_root.resolve())

        def runner():
            db = store()
            rows = db.conn.execute(
                "SELECT id, path, filename, extension FROM documents LIMIT ?", (min(limit, 50),)
            ).fetchall()
            eligible = [r for r in rows if Path(r["path"]).is_relative_to(upload_root.resolve())]
            ai_jobs[job_id]["total"] = len(eligible)

            for doc in eligible:
                blocks = db.conn.execute(
                    "SELECT text FROM content_blocks WHERE document_id=? LIMIT 6", (doc["id"],)
                ).fetchall()
                text = " ".join(b["text"][:500] for b in blocks)
                sug = organizer.suggest(
                    file_path=Path(doc["path"]),
                    extracted_text=text,
                    tags=[],
                    metadata={"filename": doc["filename"], "extension": doc["extension"]},
                )
                ai_jobs[job_id]["results"].append({
                    "document_id": doc["id"],
                    "current_path": doc["path"],
                    "filename": doc["filename"],
                    "suggested_subpath": sug.suggested_subpath,
                    "suggested_tags": sug.suggested_tags,
                    "reason": sug.reason,
                })
                ai_jobs[job_id]["done"] += 1

            ai_jobs[job_id]["status"] = "finished"

        threading.Thread(target=runner, daemon=True).start()
        return {"job_id": job_id}

    @app.post("/api/ai/reorganize/apply")
    def api_ai_reorganize_apply(
        req: ReorganizeApplyRequest,
        x_auth_token: str | None = Header(default=None),
    ):
        require_admin(x_auth_token)
        db = store()
        upload_root_resolved = upload_root.resolve()
        results = []

        for item in req.moves:
            doc = db.get_document_by_id(item.document_id)
            if not doc:
                results.append({"document_id": item.document_id, "status": "not_found"})
                continue

            current = Path(doc["path"])
            # Strip leading slashes; containment check below enforces the boundary.
            target_dir = upload_root / item.new_subpath.strip("/\\")

            try:
                target_resolved = target_dir.resolve()
                if upload_root_resolved not in target_resolved.parents and target_resolved != upload_root_resolved:
                    results.append({"document_id": item.document_id, "status": "error", "detail": "Target outside upload root"})
                    continue
            except Exception:
                results.append({"document_id": item.document_id, "status": "error", "detail": "Invalid path"})
                continue

            new_path = target_dir / current.name
            if new_path.resolve() == current.resolve():
                results.append({"document_id": item.document_id, "status": "unchanged"})
                continue

            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(current), str(new_path))
                sidecar = Path(str(current) + ".meta.json")
                if sidecar.exists():
                    shutil.move(str(sidecar), str(new_path) + ".meta.json")
                db.move_document(item.document_id, str(new_path))
                results.append({"document_id": item.document_id, "status": "moved", "new_path": str(new_path)})
            except Exception as e:
                results.append({"document_id": item.document_id, "status": "error", "detail": str(e)})

        return results

    @app.post("/api/ssl/upload")
    async def api_ssl_upload(
        x_auth_token: str | None = Header(default=None),
        cert_file: UploadFile = File(...),
        key_file: UploadFile = File(...),
    ):
        require_admin(x_auth_token)
        try:
            from cryptography import x509
            from cryptography.hazmat.backends import default_backend

            cert_data = await cert_file.read()
            key_data = await key_file.read()
            x509.load_pem_x509_certificate(cert_data, default_backend())

            ssl_dir.mkdir(parents=True, exist_ok=True)
            cert_path = ssl_dir / "cert.pem"
            key_path = ssl_dir / "key.pem"
            cert_path.write_bytes(cert_data)
            key_path.write_bytes(key_data)
            if os.name != "nt":
                key_path.chmod(0o600)

            return {"ok": True, "cert_path": str(cert_path), "key_path": str(key_path)}
        except ImportError:
            raise HTTPException(status_code=501, detail="cryptography package not installed")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid certificate: {e}")

    @app.post("/api/search")
    def api_search(req: SearchRequest, x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        db = store()
        try:
            rows = search(db, req.query, req.limit, req.filetype, req.path, req.block_type, req.modified_from, req.modified_to, req.tags, user_id)
        except sqlite3.OperationalError as e:
            raise HTTPException(status_code=400, detail=f"Search query error: {e}")
        # Group flat rows by document_id, preserving rank order
        grouped: dict[int, dict] = {}
        order: list[int] = []
        for row in rows:
            r = dict(row)
            doc_id = r["document_id"]
            if doc_id not in grouped:
                order.append(doc_id)
                grouped[doc_id] = {
                    "document_id": doc_id,
                    "filename": r["filename"],
                    "path": r["path"],
                    "extension": r["extension"],
                    "modified_at": r["modified_at"],
                    "hits": [],
                }
            grouped[doc_id]["hits"].append({
                "block_type": r["block_type"],
                "block_number": r["block_number"],
                "snippet_html": highlight_terms(r.get("snippet") or "", req.query) or None,
            })

        marks = db.get_doc_marks_and_tags(user_id, order)
        output = []
        for doc_id in order:
            doc = grouped[doc_id]
            m = marks.get(doc_id, {"is_marked": False, "tags": []})
            output.append({
                **doc,
                "is_marked": m["is_marked"],
                "tags": m["tags"],
                "open_url": f"/api/files/open?document_id={doc_id}",
                "hit_count": len(doc["hits"]),
            })
        return output

    @app.get("/api/status")
    def api_status(x_auth_token: str | None = Header(default=None)):
        require_user(x_auth_token)
        db = store()
        docs = db.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        blocks = db.conn.execute("SELECT COUNT(*) FROM content_blocks").fetchone()[0]
        total_size = db.conn.execute("SELECT COALESCE(SUM(file_size), 0) FROM documents").fetchone()[0]
        return {"documents": docs, "content_blocks": blocks, "total_file_size_bytes": total_size, "db_path": db_path}

    @app.get("/api/files/open")
    def api_files_open(document_id: int, x_auth_token: str | None = Header(default=None)):
        require_user(x_auth_token)
        db = store()
        doc = db.get_document_by_id(document_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        p = Path(doc["path"])
        if not p.exists() or not p.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(p)

    return app


app = create_app(os.getenv("DOCUMENT_SEARCH_DB", "./document_index.db"))
