from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_ASK_PROMPT = """\
You are a document search assistant. Based only on the document snippets provided, \
answer the user's question in 1–3 plain-text sentences. If the snippets do not \
contain enough information to answer, say so briefly.

Question: {query}

Snippets:
{context}
"""

_STRUCTURE_PROMPT = """\
You are a document management expert. Analyse the document list below from an organization's \
file repository and suggest an optimal folder hierarchy for organizing them.

Document sample ({count} files):
{doc_list}

Respond with ONLY a JSON object:
{{
  "suggested_structure": [
    {{
      "folder": "category/subcategory",
      "description": "Short description of what belongs here",
      "examples": ["filename1.pdf", "filename2.docx"]
    }}
  ],
  "rationale": "One or two sentence overview of the suggested approach"
}}

Rules:
- folder: lowercase, hyphens/slashes only, max 2 levels, no leading slash
- Suggest 3-8 folders that cover the document types found
- examples: up to 3 real filenames from the list
- rationale: max 200 characters, plain text
"""

_PROMPT = """\
You are a document filing assistant. Analyze the document excerpt below and suggest \
where to store it in a folder hierarchy.

Filename: {filename}
File type: {extension}
Tags provided by user: {tags}
Content preview:
---
{text}
---

Respond with ONLY a JSON object matching this schema exactly:
{{
  "suggested_subpath": "folder/subfolder",
  "suggested_tags": ["tag1", "tag2"],
  "reason": "short reason"
}}

Rules:
- suggested_subpath: lowercase, hyphens/slashes only, max 3 levels, no leading slash
- suggested_tags: 2-5 short lowercase words
- reason: max 80 characters, plain text
"""


@dataclass(slots=True)
class OrganizationSuggestion:
    suggested_subpath: str | None = None
    suggested_tags: list[str] | None = None
    reason: str | None = None
    model: str | None = None


class AiOrganizer:
    """Ollama-backed document organization suggester."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        timeout: int = 30,
    ) -> None:
        self.base_url = (
            base_url or os.getenv("DOCUMENT_SEARCH_OLLAMA_URL", "http://ollama:11434")
        ).rstrip("/")
        self.model = model or os.getenv("DOCUMENT_SEARCH_OLLAMA_MODEL", "llama3.2")
        self.timeout = timeout

    def is_available(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base_url}/api/tags", timeout=5) as r:
                return r.status == 200
        except Exception:
            return False

    def list_models(self) -> list[str]:
        try:
            with urllib.request.urlopen(f"{self.base_url}/api/tags", timeout=5) as r:
                data = json.loads(r.read())
                return [m["name"] for m in data.get("models", [])]
        except Exception:
            return []

    def test_connection(self) -> dict:
        """Send a minimal prompt to verify the configured model is working."""
        if not self.is_available():
            return {"ok": False, "error": f"Ollama not reachable at {self.base_url}", "model": self.model}

        models = self.list_models()
        base_name = self.model.split(":")[0]
        if self.model not in models and not any(m.startswith(base_name) for m in models):
            return {
                "ok": False,
                "error": f"Model '{self.model}' is not pulled. Pull it first.",
                "model": self.model,
                "available_models": models,
            }

        payload = json.dumps({
            "model": self.model,
            "prompt": "Reply with the single word: OK",
            "stream": False,
            "options": {"num_predict": 10, "temperature": 0},
        }).encode()
        try:
            req = urllib.request.Request(
                f"{self.base_url}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = json.loads(resp.read())
                return {
                    "ok": True,
                    "model": self.model,
                    "response": raw.get("response", "").strip()[:80],
                    "load_duration_ms": round(raw.get("load_duration", 0) / 1_000_000),
                    "eval_duration_ms": round(raw.get("eval_duration", 0) / 1_000_000),
                    "prompt_eval_count": raw.get("prompt_eval_count"),
                }
        except urllib.error.URLError as e:
            return {"ok": False, "error": str(e), "model": self.model}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "model": self.model}

    def get_running_models(self) -> list[dict]:
        """Return models currently loaded in Ollama memory (/api/ps)."""
        try:
            with urllib.request.urlopen(f"{self.base_url}/api/ps", timeout=5) as r:
                data = json.loads(r.read())
                return data.get("models", [])
        except Exception:
            return []

    def delete_model(self, name: str) -> dict:
        payload = json.dumps({"name": name}).encode()
        try:
            req = urllib.request.Request(
                f"{self.base_url}/api/delete",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="DELETE",
            )
            with urllib.request.urlopen(req, timeout=30):
                return {"ok": True, "model": name}
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            return {"ok": False, "model": name, "error": f"HTTP {e.code}: {body[:200]}"}
        except Exception as e:
            return {"ok": False, "model": name, "error": str(e)}

    def pull_model(self, model: str | None = None) -> dict:
        name = model or self.model
        payload = json.dumps({"name": name, "stream": False}).encode()
        try:
            req = urllib.request.Request(
                f"{self.base_url}/api/pull",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=600) as resp:
                data = json.loads(resp.read())
                return {"ok": True, "model": name, "status": data.get("status", "done")}
        except urllib.error.URLError as e:
            return {"ok": False, "model": name, "error": str(e)}
        except Exception as e:
            return {"ok": False, "model": name, "error": type(e).__name__}

    def suggest(
        self,
        *,
        file_path: Path,
        extracted_text: str = "",
        tags: list[str],
        metadata: dict[str, str],
    ) -> OrganizationSuggestion:
        text_preview = (extracted_text or "").strip()[:3000]
        prompt = _PROMPT.format(
            filename=file_path.name,
            extension=file_path.suffix.lower() or "unknown",
            tags=", ".join(tags) if tags else "none",
            text=text_preview or "(no extractable text)",
        )
        payload = json.dumps({
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.2},
        }).encode()

        try:
            req = urllib.request.Request(
                f"{self.base_url}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = json.loads(resp.read())
                parsed = json.loads(raw.get("response", "{}"))
                return OrganizationSuggestion(
                    suggested_subpath=_safe_subpath(parsed.get("suggested_subpath")),
                    suggested_tags=_safe_tags(parsed.get("suggested_tags")),
                    reason=str(parsed.get("reason", ""))[:120] or None,
                    model=self.model,
                )
        except urllib.error.URLError as e:
            logger.debug("Ollama not reachable: %s", e)
            return OrganizationSuggestion(reason="Ollama not available")
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Ollama response parse error: %s", e)
            return OrganizationSuggestion(reason="AI response could not be parsed")
        except Exception as e:
            logger.error("AI organizer error: %s", e)
            return OrganizationSuggestion(reason=f"Error: {type(e).__name__}")


    def suggest_structure(self, documents: list[dict]) -> dict:
        """Ask Ollama to suggest a folder taxonomy for a corpus of documents."""
        doc_lines = []
        for d in documents[:100]:
            tags = (d.get("tags") or "").strip()
            parent = Path(d["path"]).parent.name if d.get("path") else ""
            doc_lines.append(
                f"- {d['filename']} ({d.get('extension', '')}) | folder: {parent} | tags: {tags or 'none'}"
            )
        prompt = _STRUCTURE_PROMPT.format(count=len(documents), doc_list="\n".join(doc_lines))
        payload = json.dumps({
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.3},
        }).encode()
        try:
            req = urllib.request.Request(
                f"{self.base_url}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout * 3) as resp:
                raw = json.loads(resp.read())
                parsed = json.loads(raw.get("response", "{}"))
                structure = parsed.get("suggested_structure", [])
                return {
                    "ok": True,
                    "suggested_structure": structure,
                    "rationale": str(parsed.get("rationale", ""))[:250] or None,
                    "model": self.model,
                    "document_count": len(documents),
                }
        except urllib.error.URLError as e:
            return {"ok": False, "error": f"Ollama not available: {e}"}
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Structure suggestion parse error: %s", e)
            return {"ok": False, "error": f"AI response could not be parsed: {e}"}
        except Exception as e:
            logger.error("suggest_structure error: %s", e)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


    def ask(self, query: str, context: str) -> str | None:
        """Generate a short plain-text answer to a question given document context."""
        prompt = _ASK_PROMPT.format(query=query[:500], context=context[:3000])
        payload = json.dumps({
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": 200},
        }).encode()
        try:
            req = urllib.request.Request(
                f"{self.base_url}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = json.loads(resp.read())
                return raw.get("response", "").strip()[:500] or None
        except Exception as e:
            logger.debug("ask() failed: %s", e)
            return None


def _safe_subpath(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"[^a-z0-9/\-_]", "", value.lower().strip("/"))
    parts = [p for p in cleaned.split("/") if p][:3]
    return "/".join(parts) if parts else None


def _safe_tags(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    cleaned = [str(t).lower().strip()[:30] for t in value if t][:5]
    return cleaned or None
