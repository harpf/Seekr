from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from document_search.config import load_config
from document_search.crawler import iter_documents
from document_search.extractors.docx_extractor import DocxTextExtractor
from document_search.extractors.md_extractor import MdTextExtractor
from document_search.extractors.legacy_office_extractor import LegacyOfficeTextExtractor
from document_search.extractors.pdf_extractor import PdfTextExtractor
from document_search.extractors.pptx_extractor import PptxTextExtractor
from document_search.extractors.txt_extractor import TxtTextExtractor
from document_search.index.search_service import search
from document_search.index.sqlite_store import SqliteStore
from document_search.services.file_service import fingerprint

LOGGER = logging.getLogger("document_search")


def extractor_for(ext: str):
    return {
        ".pdf": PdfTextExtractor(),
        ".docx": DocxTextExtractor(),
        ".pptx": PptxTextExtractor(),
        ".txt": TxtTextExtractor(),
        ".md": MdTextExtractor(),
        ".doc": LegacyOfficeTextExtractor(),
        ".ppt": LegacyOfficeTextExtractor(),
    }.get(ext)


def cmd_index(args):
    cfg = load_config(Path(args.config) if args.config else None)
    store = SqliteStore(Path(args.db or cfg.database_path))
    roots = [Path(p) for p in args.paths]
    counts = {"found": 0, "indexed": 0, "skipped": 0, "updated": 0, "errors": 0}
    for path in iter_documents(roots, cfg):
        counts["found"] += 1
        fp = fingerprint(path)
        existing = store.get_document(str(fp.path))
        if existing and existing["sha256"] == fp.sha256 and existing["modified_at"] == fp.modified_at.isoformat():
            counts["skipped"] += 1
            continue
        ext = extractor_for(path.suffix.lower())
        if ext is None:
            continue
        result = ext.extract(path)
        store.upsert_document(fp, result)
        if result.status == "error":
            counts["errors"] += 1
        elif existing:
            counts["updated"] += 1
        else:
            counts["indexed"] += 1
    print(json.dumps(counts, indent=2))


def cmd_search(args):
    store = SqliteStore(Path(args.db))
    rows = search(store, args.query, args.limit, args.filetype, args.path)
    for r in rows:
        pos = f"{r['block_type']} {r['block_number']}"
        print(f"{r['filename']} | {pos} | {r['snippet']}")


def cmd_status(args):
    store = SqliteStore(Path(args.db))
    docs = store.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    blocks = store.conn.execute("SELECT COUNT(*) FROM content_blocks").fetchone()[0]
    print(json.dumps({"documents": docs, "content_blocks": blocks, "db": args.db}, indent=2))


def cmd_reset(args):
    path = Path(args.db)
    if path.exists():
        path.unlink()
    print("Index reset.")


def cmd_remove_missing(args):
    store = SqliteStore(Path(args.db))
    removed = store.remove_missing()
    print(f"Removed {removed} missing files")


def build_parser():
    p = argparse.ArgumentParser(prog="document-search")
    p.add_argument("--db", default="./document_index.db")
    p.add_argument("--config")
    sub = p.add_subparsers(required=True)
    i = sub.add_parser("index")
    i.add_argument("paths", nargs="+")
    i.set_defaults(func=cmd_index)
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--filetype")
    s.add_argument("--path")
    s.set_defaults(func=cmd_search)
    st = sub.add_parser("status")
    st.set_defaults(func=cmd_status)
    r = sub.add_parser("reset")
    r.set_defaults(func=cmd_reset)
    rm = sub.add_parser("remove-missing")
    rm.set_defaults(func=cmd_remove_missing)
    return p


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
