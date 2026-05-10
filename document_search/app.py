from __future__ import annotations
import json

import hashlib
import html
import os
import re
import threading
import uuid
import time
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.requests import Request

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


def extractor_for(ext: str):
    return {".pdf": PdfTextExtractor(), ".docx": DocxTextExtractor(), ".pptx": PptxTextExtractor(), ".txt": TxtTextExtractor(), ".md": MdTextExtractor(), ".doc": LegacyOfficeTextExtractor(), ".ppt": LegacyOfficeTextExtractor()}.get(ext)


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
    query: str = Field(min_length=1)
    limit: int = 20
    filetype: str | None = None
    path: str | None = None
    block_type: str | None = None
    modified_from: str | None = None
    modified_to: str | None = None



class UiConfigRequest(BaseModel):
    database_path: str
    supported_extensions: list[str]
    exclude_dirs: list[str]
    exclude_patterns: list[str]
    max_file_size_mb: int

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


def create_app(db_path: str = "./document_index.db") -> FastAPI:
    config_path = Path(os.getenv("DOCUMENT_SEARCH_CONFIG_PATH", "./config.json"))
    app = FastAPI(title="Document Search", version="1.3.0")
    templates = Jinja2Templates(directory="document_search/web/templates")
    app.mount("/static", StaticFiles(directory="document_search/web/static"), name="static")
    sessions: dict[str, tuple[int, float]] = {}
    jobs: dict[str, JobState] = {}
    upload_root = Path(os.getenv("DOCUMENT_SEARCH_UPLOAD_ROOT", "/documents/uploads"))
    organizer = AiOrganizer()

    def store() -> SqliteStore:
        db = SqliteStore(Path(db_path))
        db.ensure_default_admin()
        return db


    def load_effective_config() -> AppConfig:
        if config_path.exists():
            return load_config(config_path)
        return AppConfig()

    def require_user(token: str | None) -> int:
        if not token or token not in sessions:
            raise HTTPException(status_code=401, detail="Unauthorized")
        user_id, issued = sessions[token]
        if time.time() - issued > 60 * 60 * 8:
            sessions.pop(token, None)
            raise HTTPException(status_code=401, detail="Session expired")
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

    
    @app.get("/api/config")
    def api_get_config(x_auth_token: str | None = Header(default=None)):
        require_user(x_auth_token)
        if config_path.exists():
            return json.loads(config_path.read_text(encoding="utf-8"))
        return {"database_path": db_path, "supported_extensions": [".pdf", ".docx", ".pptx", ".txt"], "exclude_dirs": [".git", "node_modules", "__pycache__", ".venv", "temp"], "exclude_patterns": ["~$*", "*.tmp"], "max_file_size_mb": 100}

    @app.post("/api/config")
    def api_save_config(req: UiConfigRequest, x_auth_token: str | None = Header(default=None)):
        require_user(x_auth_token)
        config_path.write_text(json.dumps(req.model_dump(), indent=2), encoding="utf-8")
        return {"status": "saved", "path": str(config_path)}

    @app.post("/api/login")
    def api_login(req: LoginRequest):
        db = store()
        user = db.get_user(req.username)
        if not user or not verify_password(req.password, user["salt"], user["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        token = uuid.uuid4().hex
        sessions[token] = (user["id"], time.time())
        return {"token": token, "username": user["username"]}

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

        target = (upload_root / target_subpath).resolve()
        if upload_root.resolve() not in target.parents and target != upload_root.resolve():
            raise HTTPException(status_code=400, detail="Invalid target_subpath")
        target.mkdir(parents=True, exist_ok=True)

        # create deterministic safe filename suffix
        content = await file.read()
        digest = hashlib.sha256(content).hexdigest()[:8]
        out = target / f"{Path(safe_name).stem}_{digest}{ext}"
        out.write_bytes(content)

        metadata = json.loads(metadata_json or "{}")
        sidecar = out.with_suffix(out.suffix + ".meta.json")
        sidecar.write_text(json.dumps({"tags": tags, "metadata": metadata}, indent=2), encoding="utf-8")

        suggestion = organizer.suggest(file_path=out, tags=[t.strip() for t in tags.split(",") if t.strip()], metadata={k: str(v) for k, v in metadata.items()})

        db = store()
        fp = fingerprint(out)
        extractor = extractor_for(ext)
        result = extractor.extract(out) if extractor else None
        if result:
            doc_id = db.upsert_document(fp, result)
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]
            if tag_list:
                db.set_tags(user_id, doc_id, tag_list)
        else:
            doc_id = None

        return {"status": "uploaded", "path": str(out), "document_id": doc_id, "ai_suggestion": suggestion.__dict__}

    @app.post("/api/index/start")
    def api_index_start(req: IndexRequest, x_auth_token: str | None = Header(default=None)):
        require_user(x_auth_token)
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

    
    
    @app.post("/api/update/run")
    def api_run_update(x_auth_token: str | None = Header(default=None)):
        require_user(x_auth_token)
        if os.getenv("DOCUMENT_SEARCH_UI_UPDATE_ENABLED", "true").lower() != "true":
            raise HTTPException(status_code=403, detail="UI update disabled")
        script = Path("/app/scripts/update.sh")
        if not script.exists():
            raise HTTPException(status_code=404, detail="Update script not found")
        proc = subprocess.run(["/bin/sh", str(script)], capture_output=True, text=True, check=False)
        return {"exit_code": proc.returncode, "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-4000:]}

    @app.get("/api/system/dependencies")
    def api_dependencies(x_auth_token: str | None = Header(default=None)):
        require_user(x_auth_token)
        tools = ["antiword", "catppt", "tesseract", "pdftoppm"]
        return {tool: bool(shutil.which(tool)) for tool in tools}

    @app.post("/api/search")
    def api_search(req: SearchRequest, x_auth_token: str | None = Header(default=None)):
        user_id = require_user(x_auth_token)
        db = store()
        rows = search(db, req.query, req.limit, req.filetype, req.path, req.block_type, req.modified_from, req.modified_to)
        payload = [dict(r) for r in rows]
        marks = db.get_doc_marks_and_tags(user_id, [r["document_id"] for r in payload])
        for item in payload:
            item["open_url"] = f"/api/files/open?document_id={item['document_id']}"
            item["snippet_html"] = highlight_terms(item.get("snippet") or "", req.query)
            item.update(marks.get(item["document_id"], {"is_marked": False, "tags": []}))
        return payload

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
