"""Embedding generation + vector similarity for semantic/hybrid search.

The Ollama call pattern (urllib.request.Request + urlopen + timeout + broad
except returning a sentinel) intentionally mirrors document_search.services.
ai_organizer.AiOrganizer so the two share the same operational behaviour.

Vectors are stored elsewhere as packed little-endian float32 BLOBs; this module
owns the (un)packing and the pure-Python cosine ranker used as the universal
fallback when the optional `sqlite-vec` extension is unavailable.
"""
from __future__ import annotations

import json
import logging
import math
import os
import struct
import urllib.error
import urllib.request
from collections.abc import Iterable

logger = logging.getLogger(__name__)


def pack_vector(vec: list[float]) -> bytes:
    """Pack a float vector into a little-endian float32 BLOB."""
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack_vector(blob: bytes) -> list[float]:
    """Inverse of pack_vector."""
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity in [-1, 1]. Returns 0.0 for a zero-norm operand."""
    if len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def rank_by_cosine(
    query: list[float],
    candidates: Iterable[tuple[int, list[float]]],
) -> list[tuple[int, float]]:
    """Brute-force rank (document_id, vector) candidates by cosine vs query.

    Candidates whose vector dimension disagrees with the query are skipped
    (e.g. after an embed-model change) rather than raising. Returns a list of
    (document_id, score) sorted by score descending.
    """
    qdim = len(query)
    scored: list[tuple[int, float]] = []
    for doc_id, vec in candidates:
        if len(vec) != qdim:
            continue
        scored.append((doc_id, cosine_similarity(query, vec)))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored


def try_load_sqlite_vec(conn) -> bool:
    """Best-effort load of the optional `sqlite-vec` (vec0) extension.

    Returns True only if the extension loaded. Any failure (extension loading
    disabled, lib not installed, OS error) returns False so callers fall back
    to the pure-Python ranker. Never raises.
    """
    try:
        conn.enable_load_extension(True)
    except Exception:
        return False
    try:
        conn.load_extension("vec0")
        return True
    except Exception:
        return False
    finally:
        try:
            conn.enable_load_extension(False)
        except Exception:
            pass


class EmbeddingService:
    """Ollama-backed text embedder."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        timeout: int = 60,
    ) -> None:
        self.base_url = (
            base_url or os.getenv("DOCUMENT_SEARCH_OLLAMA_URL", "http://ollama:11434")
        ).rstrip("/")
        self.model = model or os.getenv(
            "DOCUMENT_SEARCH_OLLAMA_EMBED_MODEL", "nomic-embed-text"
        )
        self.timeout = timeout

    def embed(self, text: str) -> list[float] | None:
        """Return the embedding vector for `text`, or None on any failure.

        None (not an exception) is returned when Ollama is unreachable or the
        response is malformed, so callers can degrade to keyword-only search.
        """
        cleaned = (text or "").strip()
        if not cleaned:
            return None
        payload = json.dumps({"model": self.model, "prompt": cleaned[:8000]}).encode()
        try:
            req = urllib.request.Request(
                f"{self.base_url}/api/embeddings",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = json.loads(resp.read())
                vec = raw.get("embedding")
                if not isinstance(vec, list) or not vec:
                    logger.warning("Ollama embeddings response had no 'embedding' array")
                    return None
                return [float(x) for x in vec]
        except urllib.error.URLError as e:
            logger.debug("Ollama embeddings not reachable: %s", e)
            return None
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            logger.warning("Ollama embeddings parse error: %s", e)
            return None
        except Exception as e:  # noqa: BLE001 - defensive, mirrors AiOrganizer
            logger.error("Embedding error: %s", e)
            return None
