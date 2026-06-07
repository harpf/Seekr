"""Upload path + content validation shared by /api/upload and the reorganize path.

Three concerns:
  1. `reject_traversal` — fast textual `..` / NUL screen on the client-supplied
     subpath, before any filesystem resolution.
  2. `is_within` — strict containment check using the *real* (symlink-resolved)
     paths of both the root and the candidate target.
  3. `magic_matches_extension` — libmagic content sniffing so a renamed binary
     (e.g. an .exe called report.pdf) can't slip past the extension allowlist.

libmagic is provided by the `libmagic1` system package (already installed in the
Dockerfile) plus the `python-magic` Python binding. When the binding/library is
unavailable (some dev hosts), magic sniffing falls back to a leading-signature
byte check so the obvious `.exe`-as-`.pdf` case is still rejected.
"""
from __future__ import annotations

from pathlib import Path

_BINARY_EXTS = {".pdf", ".docx", ".pptx", ".doc", ".ppt"}
_TEXT_EXTS = {".txt", ".md"}

_HEADER_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    ".pdf": (b"%PDF-",),
    ".docx": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    ".pptx": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    ".doc": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    ".ppt": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
}

_OOXML = "application/vnd.openxmlformats-officedocument"
_ALLOWED_MIME: dict[str, set[str]] = {
    ".pdf": {"application/pdf"},
    ".docx": {
        f"{_OOXML}.wordprocessingml.document",
        "application/zip",  # OOXML is a zip; libmagic sometimes reports the container
    },
    ".pptx": {
        f"{_OOXML}.presentationml.presentation",
        "application/zip",
    },
    ".doc": {
        "application/msword",
        "application/x-ole-storage",
        "application/vnd.ms-office",
    },
    ".ppt": {
        "application/vnd.ms-powerpoint",
        "application/x-ole-storage",
        "application/vnd.ms-office",
    },
}


def reject_traversal(subpath: str) -> bool:
    """True if `subpath` must be rejected for traversal / NUL / absolute-path reasons."""
    if not subpath:
        return False
    if "\x00" in subpath:
        return True
    normalised = subpath.replace("\\", "/")
    # Reject absolute paths: a relative subpath must never start at the FS root
    # (posix `/...`, UNC `//...`) or carry a Windows drive (`C:/...`).
    if normalised.startswith("/"):
        return True
    if len(normalised) >= 2 and normalised[1] == ":" and normalised[0].isalpha():
        return True
    # Reject any `..` path component.
    return any(part == ".." for part in normalised.split("/"))


def is_within(root: Path, candidate: Path) -> bool:
    """True iff `candidate`, after symlink resolution, is `root` or below it."""
    root_resolved = root.resolve()
    cand_resolved = candidate.resolve()
    if cand_resolved == root_resolved:
        return True
    return root_resolved in cand_resolved.parents


def _header_matches(content: bytes, ext: str) -> bool:
    sigs = _HEADER_SIGNATURES.get(ext)
    if not sigs:
        return False
    return any(content.startswith(sig) for sig in sigs)


def _looks_like_text(content: bytes) -> bool:
    """Heuristic: a small sample decodes as UTF-8 and has no NUL bytes."""
    sample = content[:4096]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def magic_matches_extension(content: bytes, ext: str) -> tuple[bool, str | None]:
    """Return (ok, reason). `ok=False` means the content does not match `ext`.

    Text extensions (.txt/.md) pass when the bytes look like text.
    Binary extensions must match a known MIME (via libmagic) or, if libmagic is
    unavailable, a known leading header signature.
    """
    ext = ext.lower()
    if not content:
        return False, "empty file"

    if ext in _TEXT_EXTS:
        if _looks_like_text(content):
            return True, None
        return False, f"content does not look like text for {ext}"

    if ext in _BINARY_EXTS:
        detected_mime: str | None = None
        try:
            import magic  # python-magic binding over libmagic

            detected_mime = magic.from_buffer(content[:8192], mime=True)
        except Exception:
            detected_mime = None  # libmagic unavailable -> fall back to header bytes

        if detected_mime is not None:
            if detected_mime in _ALLOWED_MIME.get(ext, set()):
                return True, None
            return False, f"file content ({detected_mime}) does not match extension {ext}"
        if _header_matches(content, ext):
            return True, None
        return False, f"file content does not match extension {ext}"

    return False, f"unsupported extension {ext}"
