from __future__ import annotations

import datetime as dt
import hashlib
import html
import ipaddress
import json
import logging
import os
import posixpath
import re
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
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
from document_search.extractors.legacy_office_extractor import LegacyOfficeTextExtractor
from document_search.extractors.md_extractor import MdTextExtractor
from document_search.extractors.pdf_extractor import PdfTextExtractor
from document_search.extractors.pptx_extractor import PptxTextExtractor
from document_search.extractors.txt_extractor import TxtTextExtractor
from document_search.index.search_service import (
    FtsQueryError,
    count_documents,
    search,
)
from document_search.index.sqlite_store import SqliteStore
from document_search.logging_config import configure_logging
from document_search.services.acl_service import can_write
from document_search.services.ai_organizer import AiOrganizer
from document_search.services.file_service import fingerprint

log = logging.getLogger(__name__)

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
_SESSION_TTL_S = 60 * 60 * 8     # 8 hours — single source of truth for session lifetime
_SESSION_MAX = 10_000            # hard cap on tracked sessions (DoS guard)
_FAILURE_IP_MAX = 10_000         # hard cap on tracked rate-limit IPs (DoS guard)

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


def _resolve_default_owner_principal_id(db) -> int | None:
    """Resolve the configurable default owner for crawled documents.

    Controlled by SEEKR_DEFAULT_OWNER_PRINCIPAL (a principal external_id, group
    or user). Unset/blank/unknown -> None, preserving legacy behaviour (no owner;
    visible via the public group's read ACL).
    """
    external_id = (os.getenv("SEEKR_DEFAULT_OWNER_PRINCIPAL") or "").strip()
    if not external_id:
        return None
    row = db.conn.execute(
        "SELECT id FROM principals WHERE external_id=? ORDER BY (type='group') DESC LIMIT 1",
        (external_id,),
    ).fetchone()
    return row["id"] if row else None


def _sample_documents_for_user(db, user_id: int, sample_size: int):
    """Random sample of documents the given user may read, with tags. Used by the
    AI suggest-structure / reorganize handlers so sampling never leaks docs the
    requesting user cannot see."""
    from document_search.services.acl_service import visible_document_ids_subquery
    acl_sql, acl_params = visible_document_ids_subquery(user_id)
    sql = f"""
        SELECT d.id, d.filename, d.extension, d.path,
               GROUP_CONCAT(ut.name, ', ') AS tags
        FROM documents d
        LEFT JOIN document_tags dt ON dt.document_id = d.id
        LEFT JOIN user_tags ut ON ut.id = dt.tag_id
        WHERE d.id IN ({acl_sql})
        GROUP BY d.id
        ORDER BY RANDOM()
        LIMIT ?
    """
    params = list(acl_params) + [min(sample_size, 100)]
    return db.conn.execute(sql, params).fetchall()


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
    offset: int = 0
    filetype: str | None = None
    path: str | None = None
    block_type: str | None = None
    modified_from: str | None = None
    modified_to: str | None = None
    mode: str = "keyword"



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


class SavedSearchRequest(BaseModel):
    name: str
    query: str = ""
    filters: dict = Field(default_factory=dict)


class GroupCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    display_name: str | None = None


class GroupMemberRequest(BaseModel):
    user_id: int


class GrantRequest(BaseModel):
    document_id: int
    principal_id: int
    permission: str  # 'read' | 'write'


class RemoveDuplicatesRequest(BaseModel):
    keep_id: int
    remove_ids: list[int]


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
    configure_logging()
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
    app.state.sessions = sessions
    jobs: dict[str, JobState] = {}
    upload_root = Path(os.getenv("DOCUMENT_SEARCH_UPLOAD_ROOT", "/documents/uploads"))
    organizer = AiOrganizer()
    from document_search.services.embedding_service import EmbeddingService
    embedder = EmbeddingService()

    # Persistent job queue
    from document_search.services.job_store import JobStore
    from document_search.services.job_worker import Worker
    _startup_db = SqliteStore(Path(db_path))
    job_store = JobStore(_startup_db)
    worker = Worker(job_store, max_concurrent=4, poll_interval_s=1.0)
    app.state.job_store = job_store
    app.state.worker = worker

    def _scheduled_paths() -> list[str]:
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        except Exception:
            return []
        return [sp["path"] for sp in raw.get("source_paths", []) if sp.get("path")]

    from document_search.services.job_worker import Scheduler
    _reindex_minutes = 0
    try:
        if config_path.exists():
            _reindex_minutes = int(
                json.loads(config_path.read_text(encoding="utf-8")).get("scheduled_reindex", 0)
            )
    except Exception:
        _reindex_minutes = 0
    scheduler = (
        Scheduler(job_store, _scheduled_paths, interval_s=_reindex_minutes * 60, owner_user_id=None)
        if _reindex_minutes > 0
        else None
    )
    app.state.scheduler = scheduler

    @app.on_event("startup")
    def _start_worker() -> None:
        job_store.mark_interrupted_running_jobs()
        worker.start()
        if scheduler is not None:
            scheduler.start()

    @app.on_event("shutdown")
    def _stop_worker() -> None:
        if scheduler is not None:
            scheduler.stop(timeout=5.0)
        worker.stop(timeout=5.0)

    @worker.handler("index_paths")
    def _handle_index_paths(payload: dict, progress_cb):
        from document_search.config import load_config

        paths = payload["paths"]
        config_path_override = payload.get("config_path")
        if config_path_override:
            cfg = load_config(Path(config_path_override))
        elif config_path.exists():
            cfg = load_config(config_path)
        else:
            cfg = AppConfig()

        counts = {"found": 0, "indexed": 0, "skipped": 0, "updated": 0, "errors": 0, "done": 0}
        # Use a worker-thread-owned SqliteStore (do NOT call `store()` which is request-thread-local)
        db = SqliteStore(Path(db_path))
        default_owner_pid = _resolve_default_owner_principal_id(db)
        for path in iter_documents([Path(p) for p in paths], cfg):
            counts["found"] += 1
            fp = fingerprint(path)
            existing = db.get_document(str(fp.path))
            if existing and existing["sha256"] == fp.sha256 and existing["modified_at"] == fp.modified_at.isoformat():
                counts["skipped"] += 1
                counts["done"] += 1
                progress_cb(dict(counts))
                continue
            extr = extractor_for(path.suffix.lower())
            if extr is None:
                counts["done"] += 1
                progress_cb(dict(counts))
                continue
            result = extr.extract(path)
            db.upsert_document(fp, result, owner_principal_id=default_owner_pid)
            if result.status == "error":
                counts["errors"] += 1
            elif existing:
                counts["updated"] += 1
            else:
                counts["indexed"] += 1
            counts["done"] += 1
            progress_cb(dict(counts))
        return counts

    @worker.handler("ai_suggest_structure")
    def _handle_ai_suggest_structure(payload: dict, progress_cb):
        sample_size = payload.get("sample_size", 50)
        user_id = payload["user_id"]
        db = SqliteStore(Path(db_path))
        rows = _sample_documents_for_user(db, user_id, sample_size)
        result = organizer.suggest_structure([dict(r) for r in rows])
        return result

    @worker.handler("ai_reorganize")
    def _handle_ai_reorganize(payload: dict, progress_cb):
        limit = payload.get("limit", 10)
        user_id = payload["user_id"]
        db = SqliteStore(Path(db_path))
        from document_search.services.acl_service import visible_document_ids_subquery
        acl_sql, acl_params = visible_document_ids_subquery(user_id)
        rows = db.conn.execute(
            f"SELECT d.id, d.path, d.filename, d.extension FROM documents d "
            f"WHERE d.id IN ({acl_sql}) LIMIT ?",
            list(acl_params) + [min(limit, 50)],
        ).fetchall()
        eligible = [r for r in rows if Path(r["path"]).is_relative_to(upload_root.resolve())]
        state = {"total": len(eligible), "done": 0, "results": []}
        progress_cb(dict(state))
        for doc in eligible:
            blocks = db.conn.execute(
                "SELECT text FROM content_blocks WHERE document_id=? LIMIT 6",
                (doc["id"],),
            ).fetchall()
            text = " ".join(b["text"][:500] for b in blocks)
            sug = organizer.suggest(
                file_path=Path(doc["path"]),
                extracted_text=text,
                tags=[],
                metadata={"filename": doc["filename"], "extension": doc["extension"]},
            )
            state["results"].append({
                "document_id": doc["id"],
                "current_path": doc["path"],
                "filename": doc["filename"],
                "suggested_subpath": sug.suggested_subpath,
                "suggested_tags": sug.suggested_tags,
                "reason": sug.reason,
            })
            state["done"] += 1
            progress_cb({
                "total": state["total"],
                "done": state["done"],
                "results": list(state["results"]),
            })
        return {
            "total": state["total"],
            "done": state["done"],
            "results": state["results"],
        }

    @worker.handler("ai_bulk_tag")
    def _handle_ai_bulk_tag(payload: dict, progress_cb):
        import hashlib

        from document_search.services.acl_service import visible_document_ids_subquery
        from document_search.services.ai_validation import (
            AiValidationError,
            validate_tag_suggestion,
        )

        owner_id = payload["owner_user_id"]
        limit = min(int(payload.get("limit", 20)), 200)
        apply = bool(payload.get("apply", False))

        db = SqliteStore(Path(db_path))
        acl_sql, acl_params = visible_document_ids_subquery(owner_id)
        rows = db.conn.execute(
            f"SELECT id, path, filename, extension FROM documents "
            f"WHERE id IN ({acl_sql}) ORDER BY id LIMIT ?",
            (*acl_params, limit),
        ).fetchall()

        state = {"total": len(rows), "done": 0, "results": []}
        progress_cb(dict(state))

        for doc in rows:
            blocks = db.conn.execute(
                "SELECT text FROM content_blocks WHERE document_id=? LIMIT 6",
                (doc["id"],),
            ).fetchall()
            text = " ".join(b["text"][:500] for b in blocks)
            sug = organizer.suggest(
                file_path=Path(doc["path"]),
                extracted_text=text,
                tags=[],
                metadata={"filename": doc["filename"], "extension": doc["extension"]},
            )
            raw = {"suggested_tags": sug.suggested_tags or []}
            prompt_hash = hashlib.sha256(
                f"{doc['id']}|{doc['path']}|{text[:2000]}".encode()
            ).hexdigest()

            try:
                validated = validate_tag_suggestion(raw)
            except AiValidationError as e:
                state["results"].append({
                    "document_id": doc["id"],
                    "filename": doc["filename"],
                    "status": "skipped",
                    "reason": f"validation: {e}",
                    "applied_tags": [],
                })
                state["done"] += 1
                progress_cb({"total": state["total"], "done": state["done"], "results": list(state["results"])})
                continue

            if apply:
                db.set_tags(owner_id, doc["id"], validated.suggested_tags)
            try:
                db.record_ai_decision(
                    kind="bulk_tag",
                    model=sug.model,
                    prompt_sha256=prompt_hash,
                    document_id=doc["id"],
                    output={"suggested_tags": validated.suggested_tags},
                    applied=1 if apply else 0,
                    user_id=owner_id,
                )
            except Exception:  # provenance is secondary to the tag write
                pass

            state["results"].append({
                "document_id": doc["id"],
                "filename": doc["filename"],
                "status": "applied" if apply else "proposed",
                "reason": sug.reason,
                "applied_tags": validated.suggested_tags if apply else [],
                "proposed_tags": validated.suggested_tags,
            })
            state["done"] += 1
            progress_cb({"total": state["total"], "done": state["done"], "results": list(state["results"])})

        return {"total": state["total"], "done": state["done"], "results": state["results"]}

    @worker.handler("ai_pull")
    def _handle_ai_pull(payload: dict, progress_cb):
        model = payload.get("model")
        last = {"ok": False}
        for evt in organizer.pull_model_stream(model):
            if "ok" in evt:
                last = evt
                break
            progress_cb({
                "status": evt.get("status", "pulling"),
                "completed": evt.get("completed", 0),
                "total": evt.get("total", 0),
            })
        if not last.get("ok"):
            return {"ok": False, "model": model or organizer.model,
                    "error": last.get("error", "pull failed")}
        return {"ok": True, "model": last.get("model") or model or organizer.model,
                "status": last.get("status", "success")}

    @worker.handler("embed_index")
    def _handle_embed_index(payload: dict, progress_cb):
        cfg = load_config(config_path) if config_path.exists() else AppConfig()
        if not cfg.semantic_search_enabled:
            return {"skipped": True, "reason": "semantic_search_enabled is false", "embedded": 0}
        embedder.model = cfg.embed_model
        batch = int(payload.get("batch", 500))
        db = SqliteStore(Path(db_path))
        pending = db.get_blocks_without_embedding(limit=batch)
        total = len(pending)
        embedded = 0
        progress_cb({"total": total, "embedded": 0})
        for b in pending:
            vec = embedder.embed(b["text"])
            if vec:
                db.upsert_block_embedding(b["block_id"], b["document_id"], vec, model=cfg.embed_model)
                embedded += 1
            progress_cb({"total": total, "embedded": embedded})
        return {"skipped": False, "embedded": embedded, "total": total}

    def store() -> SqliteStore:
        """Return a per-thread SqliteStore, creating it once on first use."""
        if not getattr(_thread_local, "initialized", False):
            _thread_local.db = SqliteStore(Path(db_path))
            _thread_local.db.ensure_default_admin()
            _thread_local.initialized = True
        return _thread_local.db

    import logging as _logging
    _audit_log = _logging.getLogger("seekr.audit")

    def _client_ip(request: Request | None) -> str | None:
        if request is None or request.client is None:
            return None
        return request.client.host

    def _audit(
        actor_user_id: int | None,
        action: str,
        target_type: str | None = None,
        target_id: str | int | None = None,
        detail: dict | None = None,
        request: Request | None = None,
    ) -> None:
        """Best-effort audit write. Never raises into the calling route."""
        try:
            store().record_audit(
                actor_user_id=actor_user_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                detail=detail,
                ip=_client_ip(request),
            )
        except Exception:
            _audit_log.exception("Failed to write audit row for action=%s", action)

    def load_effective_config() -> AppConfig:
        if config_path.exists():
            return load_config(config_path)
        return AppConfig()

    # ── Error sanitisation ─────────────────────────────────────────────
    def _client_error(
        public_message: str,
        exc: BaseException,
        status_code: int = 400,
        *,
        server_error: bool = False,
    ) -> HTTPException:
        """Log the real exception server-side and return a generic HTTPException.

        Never echo `str(exc)` to the client — it can leak filesystem paths,
        SQL constraint text, or stack details. `server_error=True` uses
        log.exception (full traceback) for 5xx; otherwise log.warning.
        """
        if server_error:
            log.exception("%s", public_message)
        else:
            log.warning("%s: %s", public_message, exc)
        return HTTPException(status_code=status_code, detail=public_message)

    def _validate_remote_path(remote_path: str, share_type: str) -> None:
        """Reject empty / option-injecting / malformed remote share paths."""
        rp = (remote_path or "").strip()
        if not rp:
            raise HTTPException(status_code=400, detail="remote_path must not be empty")
        if "\x00" in rp or "," in rp:
            raise HTTPException(status_code=400, detail="remote_path contains illegal characters")
        if rp.startswith("-"):
            raise HTTPException(status_code=400, detail="remote_path must not start with '-'")
        if share_type == "smb" and not rp.startswith("//"):
            raise HTTPException(status_code=400, detail="SMB remote_path must be a //server/share UNC path")
        if share_type == "nfs" and ":" not in rp:
            raise HTTPException(status_code=400, detail="NFS remote_path must be host:/export")

    # ── HA key helpers ─────────────────────────────────────────────────

    def _load_ha_keys() -> list[dict]:
        if not config_path.exists():
            return []
        try:
            return json.loads(config_path.read_text(encoding="utf-8")).get("ha_api_keys", [])
        except Exception:
            log.warning("Failed to parse HA keys from config %s; treating as empty", config_path, exc_info=True)
            return []

    def _save_ha_keys(keys: list[dict]) -> None:
        raw: dict = {}
        if config_path.exists():
            try:
                raw = json.loads(config_path.read_text(encoding="utf-8"))
            except Exception:
                log.warning("Existing config %s is unreadable; overwriting with fresh keys", config_path, exc_info=True)
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
    def _evict_stale_failures(now: float) -> None:
        """Drop IP keys whose failures have all aged out, and enforce a cap."""
        stale = [
            ip for ip, ts in _login_failures.items()
            if not any(now - t < _RATE_LIMIT_WINDOW for t in ts)
        ]
        for ip in stale:
            _login_failures.pop(ip, None)
        if len(_login_failures) > _FAILURE_IP_MAX:
            overflow = len(_login_failures) - _FAILURE_IP_MAX
            # Evict the IPs with the oldest most-recent failure first.
            oldest = sorted(_login_failures.items(), key=lambda kv: max(kv[1]))[:overflow]
            for ip, _ in oldest:
                _login_failures.pop(ip, None)
            log.warning("Rate-limit IP cap %d exceeded; evicted %d oldest IP(s)", _FAILURE_IP_MAX, overflow)

    def _check_rate_limit(ip: str) -> None:
        now = time.time()
        _evict_stale_failures(now)
        recent = [t for t in _login_failures.get(ip, []) if now - t < _RATE_LIMIT_WINDOW]
        if recent:
            _login_failures[ip] = recent
        else:
            _login_failures.pop(ip, None)
        if len(recent) >= _RATE_LIMIT_MAX:
            raise HTTPException(status_code=429, detail="Too many failed login attempts. Try again in 5 minutes.")

    def _record_failure(ip: str) -> None:
        _login_failures.setdefault(ip, []).append(time.time())

    def _clear_failures(ip: str) -> None:
        _login_failures.pop(ip, None)

    def _evict_expired_sessions() -> None:
        """Prune expired sessions and enforce the hard cap.

        Called opportunistically on each login and auth check. O(n) over the
        sessions dict, but n is bounded by `_SESSION_MAX`.
        """
        now = time.time()
        expired = [t for t, (_uid, issued, _role) in sessions.items() if now - issued > _SESSION_TTL_S]
        for t in expired:
            sessions.pop(t, None)
        if expired:
            log.info("Evicted %d expired session(s)", len(expired))
        if len(sessions) > _SESSION_MAX:
            # Drop the oldest sessions (smallest issued-at) down to the cap.
            overflow = len(sessions) - _SESSION_MAX
            oldest = sorted(sessions.items(), key=lambda kv: kv[1][1])[:overflow]
            for t, _ in oldest:
                sessions.pop(t, None)
            log.warning("Session cap %d exceeded; evicted %d oldest session(s)", _SESSION_MAX, overflow)

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
        _evict_expired_sessions()
        if not token or token not in sessions:
            raise HTTPException(status_code=401, detail="Unauthorized")
        user_id, issued, _role = sessions[token]
        if time.time() - issued > _SESSION_TTL_S:
            sessions.pop(token, None)
            raise HTTPException(status_code=401, detail="Session expired")
        return user_id

    def require_admin(token: str | None) -> int:
        _evict_expired_sessions()
        if not token or token not in sessions:
            raise HTTPException(status_code=401, detail="Unauthorized")
        user_id, issued, role = sessions[token]
        if time.time() - issued > _SESSION_TTL_S:
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

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request):
        return templates.TemplateResponse("jobs.html", {"request": request})


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
    def api_save_config(req: UiConfigRequest, request: Request, x_auth_token: str | None = Header(default=None)):
        admin_id = require_admin(x_auth_token)
        if req.ollama_url:
            _validate_ollama_url(req.ollama_url)
        config_path.write_text(json.dumps(req.model_dump(), indent=2), encoding="utf-8")
        if req.ollama_url:
            organizer.base_url = req.ollama_url.rstrip("/")
        if req.ollama_model:
            organizer.model = req.ollama_model
        _audit(
            admin_id,
            "config.save",
            target_type="config",
            target_id=None,
            detail={"keys": sorted(req.model_dump(exclude_none=True).keys())},
            request=request,
        )
        return {"status": "saved", "path": str(config_path)}

    @app.post("/api/login")
    def api_login(req: LoginRequest, request: Request):
        ip = request.client.host if request.client else "unknown"
        _check_rate_limit(ip)
        _evict_expired_sessions()
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
        if time.time() - issued > _SESSION_TTL_S:
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
        if not db.user_can_read_document(user_id, req.document_id):
            raise HTTPException(status_code=403, detail="Not permitted to read this document")
        db.set_mark(user_id, req.document_id, req.is_marked)
        return {"status": "ok"}

    @app.post("/api/documents/tags")
    def api_tags(req: TagsRequest, x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        db = store()
        if not db.get_document_by_id(req.document_id):
            raise HTTPException(status_code=404, detail="Document not found")
        if not db.user_can_read_document(user_id, req.document_id):
            raise HTTPException(status_code=403, detail="Not permitted to read this document")
        db.set_tags(user_id, req.document_id, req.tags)
        return {"status": "ok"}

    @app.get("/api/tags")
    def api_list_tags(x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        db = store()
        return db.get_user_tags(user_id)

    @app.post("/api/documents/{document_id}/reindex")
    def api_reindex_document(document_id: int, request: Request, x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        db = store()
        doc = db.get_document_by_id(document_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        is_admin = (db.get_user_by_id(user_id) or {})["role"] == "admin"
        if not is_admin and not can_write(db.conn, user_id, document_id):
            raise HTTPException(status_code=403, detail="You do not have write access to this document")
        p = Path(doc["path"])
        if not p.exists():
            raise HTTPException(status_code=404, detail="File no longer exists on disk")
        extractor = extractor_for(p.suffix.lower())
        if extractor is None:
            raise HTTPException(status_code=400, detail=f"No extractor for extension: {p.suffix}")
        fp = fingerprint(p)
        result = extractor.extract(p)
        db.upsert_document(fp, result)
        _audit(
            user_id,
            "document.reindex",
            target_type="document",
            target_id=document_id,
            detail={"path": doc["path"], "extraction_status": result.status},
            request=request,
        )
        return {"status": "reindexed", "document_id": document_id, "blocks": len(result.blocks), "extraction_status": result.status}

    @app.post("/api/index/cleanup")
    def api_index_cleanup(request: Request, x_auth_token: str | None = Header(default=None)):
        admin_id = require_admin(x_auth_token)
        db = store()
        removed = db.remove_missing()
        _audit(
            admin_id,
            "documents.cleanup",
            target_type="documents",
            target_id=None,
            detail={"removed": removed},
            request=request,
        )
        return {"removed": removed}

    @app.get("/api/documents/duplicates")
    def api_documents_duplicates(x_auth_token: str | None = Header(default=None)):
        admin_id = require_admin(x_auth_token)
        db = store()
        from document_search.services.acl_service import visible_document_ids_subquery

        acl_sql, acl_params = visible_document_ids_subquery(admin_id)
        visible_ids = {
            r["document_id"]
            for r in db.conn.execute(
                f"SELECT document_id FROM ({acl_sql})", acl_params
            ).fetchall()
        }

        def _filter_groups(groups: list[dict]) -> list[dict]:
            out = []
            for g in groups:
                members = [d for d in g["documents"] if d["id"] in visible_ids]
                if len(members) > 1:
                    out.append(
                        {"hash": g["hash"], "count": len(members), "documents": members}
                    )
            return out

        return {
            "exact": _filter_groups(db.find_exact_duplicate_groups()),
            "content": _filter_groups(db.find_content_duplicate_groups()),
        }

    @app.post("/api/documents/duplicates/remove")
    def api_documents_duplicates_remove(
        req: RemoveDuplicatesRequest,
        x_auth_token: str | None = Header(default=None),
    ):
        admin_id = require_admin(x_auth_token)
        db = store()
        from document_search.services.acl_service import visible_document_ids_subquery

        acl_sql, acl_params = visible_document_ids_subquery(admin_id)
        visible_ids = {
            r["document_id"]
            for r in db.conn.execute(
                f"SELECT document_id FROM ({acl_sql})", acl_params
            ).fetchall()
        }

        # Load the keep doc; it must exist and be visible to the admin.
        keep = db.get_document_by_id(req.keep_id)
        if keep is None or req.keep_id not in visible_ids:
            raise HTTPException(status_code=404, detail="Keep document not found")

        # Re-derive the duplicate group SERVER-SIDE from the keep doc's hashes.
        # Never trust the client's notion of which docs form the group.
        group_rows = db.conn.execute(
            "SELECT id FROM documents "
            "WHERE sha256 = ? OR (content_hash IS NOT NULL AND content_hash = ?)",
            (keep["sha256"], keep["content_hash"]),
        ).fetchall()
        group_ids = {r["id"] for r in group_rows}

        for rid in req.remove_ids:
            if rid == req.keep_id:
                raise HTTPException(
                    status_code=400, detail=f"Cannot remove the keep document {rid}"
                )
            if rid not in group_ids:
                raise HTTPException(
                    status_code=400,
                    detail=f"Document {rid} is not in the same duplicate group as {req.keep_id}",
                )
            if rid not in visible_ids:
                raise HTTPException(
                    status_code=403, detail=f"Not permitted to remove document {rid}"
                )

        removed = db.delete_documents(list(req.remove_ids))
        return {"removed": removed, "kept": req.keep_id}

    @app.post("/api/ai/suggest-structure")
    def api_ai_suggest_structure(
        sample_size: int = 50,
        x_auth_token: str | None = Header(default=None),
    ):
        user_id = require_user(x_auth_token)
        job_id = job_store.enqueue(
            "ai_suggest_structure",
            payload={"sample_size": sample_size, "user_id": user_id},
            owner_user_id=user_id,
            max_retries=0,
        )
        return {"job_id": str(job_id)}

    @app.post("/api/ai/summarize")
    def api_ai_summarize(
        req: dict,
        x_auth_token: str | None = Header(default=None),
    ):
        user_id = require_user(x_auth_token)
        query = str(req.get("query", "")).strip()
        if not query:
            raise HTTPException(status_code=400, detail="query must not be empty")
        try:
            k = int(req.get("k", 5))
        except (TypeError, ValueError):
            k = 5
        k = max(1, min(k, 10))

        if not organizer.is_available():
            raise HTTPException(status_code=502, detail="AI model is not available")

        db = store()
        try:
            rows = search(db, query, k, 0, None, None, None, None, None, None, user_id)
        except FtsQueryError as e:
            raise HTTPException(
                status_code=400,
                detail="Could not parse your search query. Check quotes and operators.",
            ) from e
        except sqlite3.OperationalError as e:
            raise HTTPException(
                status_code=400, detail="Search query error — check your query syntax."
            ) from e

        sources: list[dict] = []
        for i, row in enumerate(rows, start=1):
            r = dict(row)
            sources.append({
                "label": f"S{i}",
                "document_id": r["document_id"],
                "block_number": r["block_number"],
                "filename": r["filename"],
                "path": r["path"],
                "text": r.get("snippet") or "",
            })

        if not sources:
            return {"summary": "No matching documents were found.", "citations": [], "sources": []}

        from document_search.services.ai_validation import AiValidationError
        try:
            result = organizer.summarize_with_citations(query=query, sources=sources)
        except AiValidationError as e:
            raise HTTPException(status_code=422, detail=f"AI output validation failed: {e}") from e
        except Exception as e:
            raise HTTPException(
                status_code=502, detail=f"AI summarisation failed: {type(e).__name__}"
            ) from e

        cited = set(result.citations)
        return {
            "summary": result.summary,
            "citations": result.citations,
            "sources": [
                {
                    "label": s["label"],
                    "document_id": s["document_id"],
                    "block_number": s["block_number"],
                    "filename": s["filename"],
                    "path": s["path"],
                    "cited": s["label"] in cited,
                }
                for s in sources
            ],
        }

    @app.post("/api/upload")
    async def api_upload(
        request: Request,
        x_auth_token: str | None = Header(default=None),
        file: UploadFile = File(...),
        target_subpath: str = Form(default=""),
        tags: str = Form(default=""),
        metadata_json: str = Form(default="{}"),
    ):
        user_id = require_user(x_auth_token)
        from document_search.services.upload_validation import (
            is_within,
            magic_matches_extension,
            reject_traversal,
        )
        safe_name = Path(file.filename or "upload.bin").name
        ext = Path(safe_name).suffix.lower()
        allowed = {".pdf", ".docx", ".pptx", ".txt", ".md", ".doc", ".ppt"}
        if ext not in allowed:
            raise HTTPException(status_code=400, detail=f"Unsupported file extension: {ext}")

        if len(metadata_json) > 8192:
            raise HTTPException(status_code=400, detail="metadata_json exceeds 8 KB limit")

        if reject_traversal(target_subpath):
            raise HTTPException(status_code=400, detail="Invalid target_subpath")
        target = (upload_root / target_subpath).resolve()
        if not is_within(upload_root, target):
            raise HTTPException(status_code=400, detail="Invalid target_subpath")
        target.mkdir(parents=True, exist_ok=True)

        content = await file.read()
        cfg = load_effective_config()
        max_bytes = cfg.max_file_size_mb * 1024 * 1024
        if len(content) > max_bytes:
            raise HTTPException(status_code=413, detail=f"File exceeds {cfg.max_file_size_mb} MB limit")

        ok, reason = magic_matches_extension(content, ext)
        if not ok:
            log.warning("Rejected upload %r: %s", safe_name, reason)
            raise HTTPException(
                status_code=400,
                detail="File content does not match its extension.",
            )

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
            owner_pid = db.get_user_principal_id(user_id)
            doc_id = db.upsert_document(fp, result, owner_principal_id=owner_pid)
            if tag_list:
                db.set_tags(user_id, doc_id, tag_list)
        else:
            doc_id = None

        _audit(
            user_id,
            "upload",
            target_type="document",
            target_id=doc_id,
            detail={"path": str(out), "filename": safe_name, "tags": tag_list},
            request=request,
        )
        return {"status": "uploaded", "path": str(out), "document_id": doc_id, "ai_suggestion": asdict(suggestion)}

    @app.get("/api/folders")
    def api_folders(x_auth_token: str | None = Header(default=None)):
        # Returns filesystem directory names under upload_root, not index documents.
        # The ACL model governs documents (document_id), not raw folders, so there is
        # no visible_document_ids_subquery to apply here. Auth gate only. See ACL
        # Enforcement Completion plan, Task 8.
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
        # Filesystem listing of configured source_paths (admin-curated config), not
        # index documents. No document_id is returned, so the ACL document filter
        # does not apply. Auth gate only. See ACL Enforcement Completion plan, Task 8.
        require_user(x_auth_token)
        raw_source_paths: list[dict] = []
        if config_path.exists():
            try:
                raw_source_paths = json.loads(config_path.read_text(encoding="utf-8")).get("source_paths", [])
                if not isinstance(raw_source_paths, list):
                    raw_source_paths = []
            except Exception:
                log.warning("Failed to read source_paths from config %s; returning none", config_path, exc_info=True)
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
        admin_id = require_admin(x_auth_token)
        for p in req.paths:
            if not p.strip():
                raise HTTPException(status_code=400, detail="Path must not be empty.")
            # Normalize to canonical POSIX path and collapse any leading double-slashes
            norm = "/" + posixpath.normpath(p).lstrip("/")
            if norm in _BLOCKED_EXACT or norm.startswith(_BLOCKED_PREFIXES):
                raise HTTPException(
                    status_code=400,
                    detail=f"Path '{p}' is not allowed. Select a specific subdirectory.",
                )
        job_id = job_store.enqueue(
            "index_paths",
            payload={"paths": req.paths, "config_path": req.config_path},
            owner_user_id=admin_id,
            max_retries=0,
        )
        return {"job_id": str(job_id)}

    @app.post("/api/index/embeddings/start")
    def api_index_embeddings_start(x_auth_token: str | None = Header(default=None)):
        admin_id = require_admin(x_auth_token)
        job_id = job_store.enqueue(
            "embed_index", payload={"batch": 500}, owner_user_id=admin_id, max_retries=0
        )
        return {"job_id": str(job_id)}

    @app.get("/api/index/jobs/{job_id}")
    def api_index_job(job_id: str, x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        try:
            jid = int(job_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="Job not found")
        job = job_store.get(jid)
        if not job or job["kind"] not in ("index_paths", "embed_index"):
            raise HTTPException(status_code=404, detail="Job not found")
        # ACL: owner or admin
        user_row = store().get_user_by_id(user_id)
        is_admin = bool(user_row) and user_row["role"] == "admin"
        if not is_admin and job["owner_user_id"] != user_id:
            raise HTTPException(status_code=404, detail="Job not found")
        progress = json.loads(job["progress_json"]) if job["progress_json"] else {}
        state_to_status = {
            "pending": "queued",
            "running": "running",
            "succeeded": "finished",
            "failed": "failed",
            "interrupted": "interrupted",
        }
        return {
            "status": state_to_status.get(job["state"], job["state"]),
            "found":   int(progress.get("found", 0)),
            "indexed": int(progress.get("indexed", 0)),
            "skipped": int(progress.get("skipped", 0)),
            "updated": int(progress.get("updated", 0)),
            "errors":  int(progress.get("errors", 0)),
            "done":    int(progress.get("done", 0)),
        }

    def _job_acl_or_404(job_id_str: str, user_id: int) -> dict:
        """Resolve a persistent job by string id, enforcing owner-or-admin access.
        Raises 404 for missing/foreign jobs (no information leak)."""
        if not job_id_str.isdigit():
            raise HTTPException(status_code=404, detail="Job not found")
        job = job_store.get(int(job_id_str))
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        user_row = store().get_user_by_id(user_id)
        is_admin = bool(user_row) and user_row["role"] == "admin"
        if not is_admin and job["owner_user_id"] != user_id:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    @app.get("/api/jobs")
    def api_list_jobs(
        state: str | None = None,
        kind: str | None = None,
        limit: int = 100,
        x_auth_token: str | None = Header(default=None),
    ):
        user_id = require_user(x_auth_token)
        user_row = store().get_user_by_id(user_id)
        is_admin = bool(user_row) and user_row["role"] == "admin"
        owner = None if is_admin else user_id
        rows = job_store.list_jobs(
            owner_user_id=owner, state=state, kind=kind, limit=min(limit, 500)
        )
        out = []
        for r in rows:
            progress = json.loads(r["progress_json"]) if r["progress_json"] else {}
            out.append({
                "id": r["id"],
                "kind": r["kind"],
                "state": r["state"],
                "retry_count": r["retry_count"],
                "max_retries": r["max_retries"],
                "cancel_requested": bool(r["cancel_requested"]),
                "owner_user_id": r["owner_user_id"],
                "created_at": r["created_at"],
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
                "error_message": r["error_message"],
                "progress": progress,
            })
        return out

    @app.post("/api/jobs/{job_id}/cancel")
    def api_cancel_job(job_id: str, x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        _job_acl_or_404(job_id, user_id)
        outcome = job_store.request_cancel(int(job_id))
        if outcome == "not_found":
            raise HTTPException(status_code=404, detail="Job not found")
        return {"job_id": job_id, "outcome": outcome}

    @app.post("/api/jobs/{job_id}/re-enqueue")
    def api_re_enqueue_job(job_id: str, x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        _job_acl_or_404(job_id, user_id)
        new_id = job_store.re_enqueue(int(job_id))
        if new_id is None:
            raise HTTPException(
                status_code=409,
                detail="Job cannot be re-enqueued (must be interrupted, failed, or cancelled)",
            )
        return {"job_id": str(new_id)}

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
            log.exception("git rev-parse HEAD failed; falling back to GIT_COMMIT env var")
        if not current_commit:
            current_commit = os.getenv("GIT_COMMIT")

        latest_commit: str | None = None
        check_error: str | None = None
        import urllib.request as _ur
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
        admin_id = require_admin(x_auth_token)
        if os.getenv("DOCUMENT_SEARCH_UI_UPDATE_ENABLED", "true").lower() != "true":
            raise HTTPException(status_code=403, detail="UI update disabled")
        script = Path("/app/scripts/update.sh")
        if not script.exists():
            raise HTTPException(status_code=404, detail="Update script not found")

        job_id = uuid.uuid4().hex
        _update_job.clear()
        _update_job.update({"job_id": job_id, "status": "running", "stdout": "", "stderr": "", "exit_code": None})

        # Persist a system_update row that is finalised inside _runner BEFORE the
        # subprocess can replace this process. It is intentionally NOT enqueued on
        # the worker (no auto-resume); claiming it to 'running' lets the startup
        # hook record a crash mid-update as interrupted.
        persistent_id = job_store.enqueue(
            "system_update",
            payload={"legacy_job_id": job_id},
            owner_user_id=admin_id,
            max_retries=0,
        )
        job_store.claim_next(kinds=["system_update"])

        def _runner():
            try:
                proc = subprocess.run(
                    ["/bin/sh", str(script)],
                    capture_output=True, text=True, check=False, timeout=600,
                )
            except subprocess.TimeoutExpired as e:
                log.warning("Update script timed out after 600s")
                _update_job.update({
                    "status": "error",
                    "exit_code": None,
                    "stdout": (e.stdout or "")[-4000:] if isinstance(e.stdout, str) else "",
                    "stderr": "Update timed out after 600 seconds.",
                })
                job_store.mark_failed_permanent(persistent_id, "update timed out after 600s")
                return
            _update_job.update({
                "status": "done" if proc.returncode == 0 else "error",
                "exit_code": proc.returncode,
                "stdout": proc.stdout[-4000:],
                "stderr": proc.stderr[-4000:],
            })
            if proc.returncode == 0:
                job_store.mark_succeeded(persistent_id, {
                    "exit_code": proc.returncode,
                    "stdout": proc.stdout[-4000:],
                    "stderr": proc.stderr[-4000:],
                })
            else:
                job_store.mark_failed_permanent(
                    persistent_id, f"update.sh exited {proc.returncode}"
                )

        threading.Thread(target=_runner, daemon=True).start()
        return {"job_id": job_id, "persistent_id": str(persistent_id), "status": "started"}

    @app.get("/api/update/status")
    def api_update_status(x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        if _update_job.get("status") not in (None, "idle"):
            return dict(_update_job)
        rows = job_store.list_jobs(kind="system_update", limit=1)
        if not rows:
            return dict(_update_job)
        row = rows[0]
        state_map = {"succeeded": "done", "failed": "error", "running": "running",
                     "interrupted": "error", "cancelled": "error", "pending": "running"}
        result = json.loads(row["result_json"]) if row["result_json"] else {}
        return {
            "status": state_map.get(row["state"], row["state"]),
            "exit_code": result.get("exit_code"),
            "stdout": result.get("stdout", ""),
            "stderr": result.get("stderr", row["error_message"] or ""),
            "persistent_id": str(row["id"]),
        }

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
        # HA has no Seekr user identity (API-key channel, mirrors _ha_search_impl's
        # bypass_acl=True). Scope counts by the key's path_filter instead.
        path_filter: str | None = key_cfg.get("path_filter")
        where = ""
        params: list = []
        if path_filter:
            where = " WHERE d.path LIKE ?"
            params = [path_filter + "%"]
        doc_count = db.conn.execute(
            f"SELECT COUNT(*) FROM documents d{where}", params
        ).fetchone()[0]
        block_count = db.conn.execute(
            f"SELECT COUNT(*) FROM content_blocks cb WHERE cb.document_id IN "
            f"(SELECT d.id FROM documents d{where})",
            params,
        ).fetchone()[0]
        total_size = db.conn.execute(
            f"SELECT COALESCE(SUM(d.file_size), 0) FROM documents d{where}", params
        ).fetchone()[0]
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
        # HA is a privileged integration channel: API-key auth + per-key path_filter
        # already scope results. There is no Seekr user identity to map onto, so we
        # explicitly bypass the ACL filter. If HA keys ever become per-user, switch
        # this to pass the mapped user_id instead. See ACL Foundation plan, Task 10.
        rows = search(db, query, limit, 0, None, path_filter, None, None, None, None, None, bypass_acl=True)
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
            "created_at": dt.datetime.now(dt.UTC).isoformat(),
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
        raw_cfg: dict = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        paths = [sp["path"] for sp in raw_cfg.get("source_paths", []) if sp.get("path")]
        if not paths:
            raise HTTPException(status_code=400, detail="No source paths configured in config.json")
        job_id = job_store.enqueue(
            "index_paths",
            payload={"paths": paths, "config_path": None},
            owner_user_id=None,
            max_retries=0,
        )
        return {"job_id": str(job_id), "paths": paths}

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

    @app.get("/api/audit")
    def api_audit(
        x_auth_token: str | None = Header(default=None),
        actor_user_id: int | None = None,
        action: str | None = None,
        target_type: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        require_admin(x_auth_token)
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        db = store()
        items = db.list_audit(
            actor_user_id=actor_user_id,
            action=action,
            target_type=target_type,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
        )
        total = db.count_audit(
            actor_user_id=actor_user_id,
            action=action,
            target_type=target_type,
            date_from=date_from,
            date_to=date_to,
        )
        # Drop the raw detail_json from the wire shape — clients use `detail`.
        for it in items:
            it.pop("detail_json", None)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    @app.post("/api/users")
    def api_create_user(req: UserCreateRequest, request: Request, x_auth_token: str | None = Header(default=None)):
        admin_id = require_admin(x_auth_token)
        if req.role not in ("admin", "user"):
            raise HTTPException(status_code=400, detail="Role must be 'admin' or 'user'")
        _validate_username(req.username)
        _validate_password(req.password)
        db = store()
        try:
            user_id = db.create_user(req.username, req.password, req.role)
            _audit(
                admin_id,
                "user.create",
                target_type="user",
                target_id=user_id,
                detail={"username": req.username, "role": req.role},
                request=request,
            )
            return {"id": user_id, "username": req.username, "role": req.role}
        except Exception as e:
            raise _client_error("Could not create user (it may already exist).", e, 400)

    @app.put("/api/users/{user_id}")
    def api_update_user(user_id: int, req: UserUpdateRequest, request: Request, x_auth_token: str | None = Header(default=None)):
        admin_id = require_admin(x_auth_token)
        if req.role not in ("admin", "user"):
            raise HTTPException(status_code=400, detail="Role must be 'admin' or 'user'")
        db = store()
        if not db.get_user_by_id(user_id):
            raise HTTPException(status_code=404, detail="User not found")
        db.update_user_role(user_id, req.role)
        _audit(
            admin_id,
            "user.update_role",
            target_type="user",
            target_id=user_id,
            detail={"role": req.role},
            request=request,
        )
        return {"status": "updated"}

    @app.delete("/api/users/{user_id}")
    def api_delete_user(user_id: int, request: Request, x_auth_token: str | None = Header(default=None)):
        admin_id = require_admin(x_auth_token)
        if user_id == admin_id:
            raise HTTPException(status_code=400, detail="Cannot delete your own account")
        db = store()
        existing = db.get_user_by_id(user_id)
        if not existing:
            raise HTTPException(status_code=404, detail="User not found")
        db.delete_user(user_id)
        _audit(
            admin_id,
            "user.delete",
            target_type="user",
            target_id=user_id,
            detail={"username": existing["username"]},
            request=request,
        )
        return {"status": "deleted"}

    @app.post("/api/users/{user_id}/change-password")
    def api_change_password(user_id: int, req: ChangePasswordRequest, request: Request, x_auth_token: str | None = Header(default=None)):
        admin_id = require_admin(x_auth_token)
        _validate_password(req.new_password)
        db = store()
        if not db.get_user_by_id(user_id):
            raise HTTPException(status_code=404, detail="User not found")
        db.change_password(user_id, req.new_password)
        _audit(
            admin_id,
            "user.change_password",
            target_type="user",
            target_id=user_id,
            detail=None,  # never record password material
            request=request,
        )
        return {"status": "password changed"}

    # ── Groups (admin) ─────────────────────────────────────────────────

    @app.get("/api/groups")
    def api_list_groups(x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        return store().list_groups()

    @app.post("/api/groups")
    def api_create_group(req: GroupCreateRequest, x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        name = req.name.strip().lower()
        if not name:
            raise HTTPException(status_code=400, detail="Group name must not be empty")
        gid = store().create_group(name, req.display_name)
        return {"id": gid, "name": name, "display_name": req.display_name or name}

    @app.delete("/api/groups/{principal_id}")
    def api_delete_group(principal_id: int, x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        try:
            store().delete_group(principal_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"status": "deleted"}

    @app.get("/api/groups/{principal_id}/members")
    def api_list_group_members(principal_id: int, x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        return store().list_group_members(principal_id)

    @app.post("/api/groups/{principal_id}/members")
    def api_add_group_member(
        principal_id: int,
        req: GroupMemberRequest,
        x_auth_token: str | None = Header(default=None),
    ):
        require_admin(x_auth_token)
        try:
            store().add_user_to_group(req.user_id, principal_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"status": "added"}

    @app.delete("/api/groups/{principal_id}/members/{user_id}")
    def api_remove_group_member(
        principal_id: int,
        user_id: int,
        x_auth_token: str | None = Header(default=None),
    ):
        require_admin(x_auth_token)
        try:
            store().remove_user_from_group(user_id, principal_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"status": "removed"}

    # ── Document ACLs ──────────────────────────────────────────────────

    @app.get("/api/acl/documents/{document_id}")
    def api_list_document_acl(document_id: int, x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        db = store()
        doc = db.get_document_by_id(document_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        # Admin OR document owner may inspect the ACL.
        is_admin = (db.get_user_by_id(user_id) or {})["role"] == "admin"
        if not is_admin:
            owner_pid = doc["owner_principal_id"] if "owner_principal_id" in doc.keys() else None
            user_pid = db.conn.execute(
                "SELECT principal_id FROM users WHERE id=?", (user_id,)
            ).fetchone()
            user_pid = user_pid["principal_id"] if user_pid else None
            if owner_pid is None or owner_pid != user_pid:
                raise HTTPException(status_code=403, detail="Not allowed")
        return db.list_document_acl(document_id)

    @app.post("/api/acl/grant")
    def api_acl_grant(req: GrantRequest, x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        try:
            store().grant(req.document_id, req.principal_id, req.permission)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"status": "granted"}

    @app.post("/api/acl/revoke")
    def api_acl_revoke(req: GrantRequest, x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        try:
            store().revoke(req.document_id, req.principal_id, req.permission)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"status": "revoked"}

    # ── Path test & network mount ──────────────────────────────────────

    @app.post("/api/paths/test")
    def api_path_test(req: PathTestRequest, x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        if "\x00" in (req.path or ""):
            raise HTTPException(status_code=400, detail="Invalid path")
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
                log.warning("Write test failed for path %s; marking not writable", p, exc_info=True)
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
        if req.share_type not in ("smb", "nfs"):
            raise HTTPException(status_code=400, detail=f"Unknown share type: {req.share_type}. Use 'smb' or 'nfs'")
        _validate_remote_path(req.remote_path, req.share_type)

        mount_point = Path(req.mount_point)
        try:
            mount_point.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise _client_error("Cannot create mount point.", e, 500, server_error=True)

        creds_file: str | None = None
        try:
            if req.share_type == "smb":
                options = ["vers=3.0"]
                if req.username or req.password or req.domain:
                    fd, creds_file = tempfile.mkstemp(prefix="seekr-creds-")
                    lines = []
                    if req.username:
                        lines.append(f"username={req.username}")
                    if req.password:
                        lines.append(f"password={req.password}")
                    if req.domain:
                        lines.append(f"domain={req.domain}")
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        fh.write("\n".join(lines) + "\n")
                    os.chmod(creds_file, 0o600)
                    options.append(f"credentials={creds_file}")
                cmd = ["mount", "-t", "cifs", req.remote_path, str(mount_point), "-o", ",".join(options)]
            else:  # nfs (validated above)
                cmd = ["mount", "-t", "nfs", req.remote_path, str(mount_point)]

            proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
            return {
                "mounted": proc.returncode == 0,
                "exit_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "mount_point": str(mount_point),
            }
        finally:
            if creds_file:
                try:
                    os.remove(creds_file)
                except OSError as e:
                    log.warning("Could not remove mount credentials file %s: %s", creds_file, e)

    @app.post("/api/paths/unmount")
    def api_path_unmount(req: PathTestRequest, x_auth_token: str | None = Header(default=None)):
        require_admin(x_auth_token)
        if os.name == "nt":
            raise HTTPException(status_code=400, detail="Unmount not supported on Windows")
        path = (req.path or "").strip()
        if not path or path.startswith("-") or "\x00" in path:
            raise HTTPException(status_code=400, detail="Invalid path")
        proc = subprocess.run(["umount", path], capture_output=True, text=True, check=False, timeout=30)
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
    def api_ssl_generate(req: SslGenerateRequest, request: Request, x_auth_token: str | None = Header(default=None)):
        admin_id = require_admin(x_auth_token)
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

            now_utc = dt.datetime.now(dt.UTC)
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

            _audit(
                admin_id,
                "ssl.generate",
                target_type="ssl",
                target_id=None,
                detail={"common_name": req.common_name, "days": req.days},
                request=request,
            )
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
            raise _client_error("Certificate generation failed.", e, 500, server_error=True)

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
        admin_id = require_admin(x_auth_token)
        model = req.model or organizer.model
        job_id = job_store.enqueue(
            "ai_pull",
            payload={"model": req.model},
            owner_user_id=admin_id,
            max_retries=0,
        )
        return {"job_id": str(job_id), "model": model}

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
            log.warning("nvidia-smi unavailable or failed; reporting no GPU info", exc_info=True)

        # Models from Ollama with sizes
        models: list[dict] = []
        import urllib.request as _ur
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
            log.warning("Could not list models from Ollama at %s; returning empty list", organizer.base_url, exc_info=True)

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
        user_id = require_user(x_auth_token)
        # First: persistent jobs (numeric ID)
        if job_id.isdigit():
            jid = int(job_id)
            job = job_store.get(jid)
            if job and job["kind"] in ("ai_suggest_structure", "ai_reorganize", "ai_pull", "ai_bulk_tag"):
                user_row = store().get_user_by_id(user_id)
                is_admin = bool(user_row) and user_row["role"] == "admin"
                if not is_admin and job["owner_user_id"] != user_id:
                    raise HTTPException(status_code=404, detail="Job not found")
                import json
                state_to_status = {
                    "pending": "queued",
                    "running": "running",
                    "succeeded": "finished",
                    "failed": "failed",
                    "interrupted": "interrupted",
                    "cancelled": "cancelled",
                }
                response: dict = {"status": state_to_status.get(job["state"], job["state"])}
                progress = json.loads(job["progress_json"]) if job["progress_json"] else {}
                result   = json.loads(job["result_json"])   if job["result_json"]   else None
                if job["kind"] == "ai_pull":
                    payload = json.loads(job["payload_json"]) if job["payload_json"] else {}
                    model = (result or {}).get("model") or payload.get("model") or organizer.model
                    if job["state"] in ("pending", "running"):
                        pull_status = "pulling"
                    elif job["state"] == "succeeded" and (result or {}).get("ok"):
                        pull_status = "done"
                    else:
                        pull_status = "error"
                    return {"status": pull_status, "model": model, "result": result, "progress": progress}
                if job["kind"] == "ai_suggest_structure":
                    response["result"] = result if job["state"] == "succeeded" else None
                else:  # ai_reorganize
                    final = result or progress or {}
                    response["total"]   = final.get("total", 0)
                    response["done"]    = final.get("done", 0)
                    response["results"] = final.get("results", [])
                if job["state"] == "failed":
                    response["error"] = job["error_message"]
                return response
        raise HTTPException(status_code=404, detail="Job not found")

    @app.post("/api/ai/reorganize/start")
    def api_ai_reorganize_start(
        limit: int = 10,
        x_auth_token: str | None = Header(default=None),
    ):
        admin_id = require_admin(x_auth_token)
        job_id = job_store.enqueue(
            "ai_reorganize",
            payload={"limit": limit, "user_id": admin_id},
            owner_user_id=admin_id,
            max_retries=0,
        )
        return {"job_id": str(job_id)}

    @app.post("/api/ai/bulk-tag/start")
    def api_ai_bulk_tag_start(
        req: dict,
        x_auth_token: str | None = Header(default=None),
    ):
        admin_id = require_admin(x_auth_token)
        try:
            limit = int(req.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
        apply = bool(req.get("apply", False))
        job_id = job_store.enqueue(
            "ai_bulk_tag",
            payload={"owner_user_id": admin_id, "limit": limit, "apply": apply},
            owner_user_id=admin_id,
            max_retries=0,
        )
        return {"job_id": str(job_id)}

    @app.post("/api/ai/reorganize/apply")
    def api_ai_reorganize_apply(
        req: ReorganizeApplyRequest,
        request: Request,
        x_auth_token: str | None = Header(default=None),
    ):
        apply_user_id = require_admin(x_auth_token)
        db = store()
        is_admin_apply = True  # require_admin guarantees admin
        upload_root_resolved = upload_root.resolve()
        results = []

        for item in req.moves:
            doc = db.get_document_by_id(item.document_id)
            if not doc:
                results.append({"document_id": item.document_id, "status": "not_found"})
                continue

            if not is_admin_apply and not can_write(db.conn, apply_user_id, item.document_id):
                results.append({"document_id": item.document_id, "status": "forbidden"})
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
                _audit(
                    apply_user_id,
                    "document.move",
                    target_type="document",
                    target_id=item.document_id,
                    detail={"old_path": str(current), "new_path": str(new_path)},
                    request=request,
                )
                results.append({"document_id": item.document_id, "status": "moved", "new_path": str(new_path)})
            except Exception as e:
                results.append({"document_id": item.document_id, "status": "error", "detail": str(e)})

        return results

    @app.post("/api/ssl/upload")
    async def api_ssl_upload(
        request: Request,
        x_auth_token: str | None = Header(default=None),
        cert_file: UploadFile = File(...),
        key_file: UploadFile = File(...),
    ):
        admin_id = require_admin(x_auth_token)
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

            _audit(
                admin_id,
                "ssl.upload",
                target_type="ssl",
                target_id=None,
                detail={"cert_filename": cert_file.filename},
                request=request,
            )
            return {"ok": True, "cert_path": str(cert_path), "key_path": str(key_path)}
        except ImportError:
            raise HTTPException(status_code=501, detail="cryptography package not installed")
        except Exception as e:
            raise _client_error("Invalid certificate.", e, 400)

    @app.post("/api/search")
    def api_search(req: SearchRequest, request: Request, response: Response, x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        db = store()
        limit = max(1, min(req.limit, 100))
        offset = max(0, req.offset)
        cfg = load_effective_config()
        mode = req.mode if cfg.semantic_search_enabled else "keyword"
        embed_fn = embedder.embed if (cfg.semantic_search_enabled and mode != "keyword") else None
        if cfg.semantic_search_enabled and mode != "keyword":
            embedder.model = cfg.embed_model
        try:
            rows = search(db, req.query, limit, offset, req.filetype, req.path, req.block_type, req.modified_from, req.modified_to, req.tags, user_id, mode=mode, embed_fn=embed_fn, bm25_weight=cfg.bm25_weight, vector_weight=cfg.vector_weight)
            total = count_documents(db, req.query, req.filetype, req.path, req.block_type, req.modified_from, req.modified_to, req.tags, user_id)
        except FtsQueryError:
            raise HTTPException(400, "Could not parse your search query. Check quotes and operators.")
        except sqlite3.OperationalError as e:
            raise _client_error("Search query error — check your query syntax.", e, 400)
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

        has_more = len(rows) >= limit
        response.headers["X-Total-Count"] = str(total)
        response.headers["X-Has-More"] = "true" if has_more else "false"
        response.headers["X-Next-Offset"] = str(offset + limit) if has_more else str(offset)
        response.headers["Access-Control-Expose-Headers"] = "X-Total-Count, X-Has-More, X-Next-Offset"
        db.record_search_history(
            user_id,
            req.query,
            {
                "filetype": req.filetype,
                "path": req.path,
                "block_type": req.block_type,
                "modified_from": req.modified_from,
                "modified_to": req.modified_to,
                "tags": req.tags,
            },
        )
        _audit(
            user_id,
            "search",
            target_type="query",
            target_id=None,
            detail={"query": req.query, "result_count": len(output)},
            request=request,
        )
        return output

    @app.get("/api/search/history")
    def api_search_history(x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        return store().list_search_history(user_id)

    @app.delete("/api/search/history")
    def api_clear_search_history(x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        removed = store().clear_search_history(user_id)
        return {"status": "ok", "removed": removed}

    @app.get("/api/search/saved")
    def api_list_saved_searches(x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        return store().list_saved_searches(user_id)

    @app.post("/api/search/saved")
    def api_create_saved_search(req: SavedSearchRequest, x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        name = req.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Saved search name must not be blank.")
        try:
            saved_id = store().create_saved_search(user_id, name, req.query, req.filters)
        except sqlite3.IntegrityError as e:
            raise HTTPException(
                status_code=409,
                detail=f"A saved search named '{name}' already exists.",
            ) from e
        return {"id": saved_id, "name": name, "query": req.query, "filters": req.filters}

    @app.delete("/api/search/saved/{saved_id}")
    def api_delete_saved_search(saved_id: int, x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        removed = store().delete_saved_search(user_id, saved_id)
        if removed == 0:
            raise HTTPException(status_code=404, detail="Saved search not found.")
        return {"status": "deleted", "id": saved_id}

    @app.get("/api/status")
    def api_status(x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        db = store()
        from document_search.services.acl_service import visible_document_ids_subquery
        acl_sql, acl_params = visible_document_ids_subquery(user_id)
        docs = db.conn.execute(
            f"SELECT COUNT(*) FROM documents d WHERE d.id IN ({acl_sql})", acl_params
        ).fetchone()[0]
        blocks = db.conn.execute(
            f"SELECT COUNT(*) FROM content_blocks cb WHERE cb.document_id IN ({acl_sql})",
            acl_params,
        ).fetchone()[0]
        total_size = db.conn.execute(
            f"SELECT COALESCE(SUM(d.file_size), 0) FROM documents d WHERE d.id IN ({acl_sql})",
            acl_params,
        ).fetchone()[0]
        return {"documents": docs, "content_blocks": blocks, "total_file_size_bytes": total_size, "db_path": db_path}

    @app.get("/api/files/open")
    def api_files_open(document_id: int, request: Request, x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        db = store()
        doc = db.get_document_by_id(document_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        if not db.user_can_read_document(user_id, document_id):
            raise HTTPException(status_code=403, detail="Not permitted to read this document")
        p = Path(doc["path"])
        if not p.exists() or not p.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        _audit(
            user_id,
            "file.open",
            target_type="document",
            target_id=document_id,
            detail={"path": doc["path"], "filename": doc["filename"]},
            request=request,
        )
        return FileResponse(p)

    return app


app = create_app(os.getenv("DOCUMENT_SEARCH_DB", "./document_index.db"))
