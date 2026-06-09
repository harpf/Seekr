#!/usr/bin/env python3
"""Reproducible performance benchmark for Seekr's hot query paths.

Measure → identify → fix → verify. This harness builds a synthetic index at a
chosen scale and times the REAL code paths (the same functions the API calls),
so any optimisation can be proven with before/after numbers and regressions are
visible. It is a developer tool — not part of the test suite or CI.

Usage (from the repo root):

    PYTHONPATH=. python scripts/perf_bench.py --docs 20000 --blocks-per 10
    PYTHONPATH=. python scripts/perf_bench.py --docs 50000 --explain
    PYTHONPATH=. python scripts/perf_bench.py --db ./document_index.db   # real DB, read-only timing

What it reports (best-of-N milliseconds):
  - visibility    : visible_document_ids_subquery set-build (ACL core)
  - status.*      : the three /api/status aggregate queries
  - count.browse  : count_documents for an empty query (browse total)
  - count.fts     : count_documents for a keyword query (search total)
  - search.broad  : search() for a term matching most docs (worst case)
  - search.rare   : search() for a selective term
With --explain it also prints EXPLAIN QUERY PLAN for each timed statement.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

# Allow running as `python scripts/perf_bench.py` without PYTHONPATH=.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from document_search.index.search_service import (  # noqa: E402
    build_match_query,
    count_documents,
    search,
)
from document_search.index.sqlite_store import SqliteStore  # noqa: E402
from document_search.services.acl_service import visible_document_ids_subquery  # noqa: E402

_NOW = "2026-06-10T00:00:00"


def _best_ms(fn, n: int) -> float:
    best = float("inf")
    for _ in range(n):
        t = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t)
    return round(best * 1000, 2)


def build_dataset(db_path: Path, docs: int, blocks_per: int) -> tuple[SqliteStore, int]:
    store = SqliteStore(db_path)
    uid = store.create_user("admin", "adminpassword")
    c = store.conn
    pub = c.execute(
        "SELECT id FROM principals WHERE type='group' AND external_id='public'"
    ).fetchone()["id"]

    print(f"building {docs} docs x {blocks_per} blocks ...", flush=True)
    c.executemany(
        "INSERT INTO documents(id,path,filename,extension,mime_type,file_size,"
        "modified_at,created_at,sha256,indexed_at,status) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [
            (i, f"/docs/file{i}.pdf", f"file{i}.pdf", ".pdf", "application/pdf",
             1000 + i, _NOW, _NOW, f"hash{i}", _NOW, "indexed")
            for i in range(1, docs + 1)
        ],
    )
    c.executemany(
        "INSERT INTO document_acl(document_id,principal_id,permission,granted_at) VALUES(?,?,?,?)",
        [(i, pub, "read", _NOW) for i in range(1, docs + 1)],
    )
    # Block text: every doc contains "invoice" (broad term); ~1% contain "zürich"
    # (rare term) so we can contrast worst-case and selective searches.
    block_rows = []
    bid = 0
    for i in range(1, docs + 1):
        rare = " zürich" if i % 100 == 0 else ""
        for b in range(blocks_per):
            bid += 1
            block_rows.append((bid, i, "page", b, f"invoice document body {i}{rare}", "Pdf", 20, None))
    c.executemany(
        "INSERT INTO content_blocks(id,document_id,block_type,block_number,text,"
        "extractor,text_length,metadata_json) VALUES(?,?,?,?,?,?,?,?)",
        block_rows,
    )
    c.executemany(
        "INSERT INTO content_fts(document_id,block_id,path,filename,extension,"
        "block_type,block_number,text) VALUES(?,?,?,?,?,?,?,?)",
        [
            (r[1], r[0], f"/docs/file{r[1]}.pdf", f"file{r[1]}.pdf", ".pdf", r[2], r[3], r[4])
            for r in block_rows
        ],
    )
    c.commit()
    c.execute("ANALYZE")
    c.commit()
    return store, uid


def _explain(conn, label: str, sql: str, params) -> None:
    print(f"  EXPLAIN {label}:", flush=True)
    for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params):
        detail = row["detail"] if "detail" in row.keys() else tuple(row)
        print(f"    {detail}", flush=True)


def run(store: SqliteStore, uid: int, repeats: int, explain: bool) -> None:
    c = store.conn
    acl_sql, acl_params = visible_document_ids_subquery(uid)

    status_docs = f"SELECT COUNT(*) FROM documents d WHERE d.id IN ({acl_sql})"
    status_blocks = f"SELECT COUNT(*) FROM content_blocks cb WHERE cb.document_id IN ({acl_sql})"
    status_size = f"SELECT COALESCE(SUM(d.file_size),0) FROM documents d WHERE d.id IN ({acl_sql})"

    timings = {
        "visibility": _best_ms(
            lambda: c.execute(f"SELECT COUNT(*) FROM ({acl_sql})", acl_params).fetchone(), repeats
        ),
        "status.docs": _best_ms(lambda: c.execute(status_docs, acl_params).fetchone(), repeats),
        "status.blocks": _best_ms(lambda: c.execute(status_blocks, acl_params).fetchone(), repeats),
        "status.size": _best_ms(lambda: c.execute(status_size, acl_params).fetchone(), repeats),
        "count.browse": _best_ms(lambda: count_documents(store, "", user_id=uid), repeats),
        # cap=1000 mirrors the production search path (app.SEARCH_TOTAL_CAP).
        "count.fts": _best_ms(lambda: count_documents(store, "invoice", user_id=uid, cap=1000), repeats),
        "count.fts.exact": _best_ms(lambda: count_documents(store, "invoice", user_id=uid), repeats),
        "search.broad": _best_ms(
            lambda: search(store, "invoice", limit=25, user_id=uid, mode="keyword"), repeats
        ),
        "search.rare": _best_ms(
            lambda: search(store, "zürich", limit=25, user_id=uid, mode="keyword"), repeats
        ),
    }

    width = max(len(k) for k in timings)
    print(f"\n{'operation'.ljust(width)}   best-of-{repeats} (ms)", flush=True)
    print("-" * (width + 20), flush=True)
    for k, v in timings.items():
        print(f"{k.ljust(width)}   {v:>10}", flush=True)

    if explain:
        print("\n--- query plans ---", flush=True)
        _explain(c, "status.blocks", status_blocks, acl_params)
        _explain(c, "count.fts", *(_fts_count_sql(acl_sql, acl_params)))


def _fts_count_sql(acl_sql: str, acl_params: list) -> tuple[str, list]:
    sql = (
        "SELECT COUNT(DISTINCT c.document_id) FROM content_fts c "
        "JOIN documents d ON d.id = c.document_id "
        f"WHERE content_fts MATCH ? AND d.id IN ({acl_sql})"
    )
    return sql, ["invoice", *acl_params]


def main() -> None:
    ap = argparse.ArgumentParser(description="Seekr query performance benchmark")
    ap.add_argument("--docs", type=int, default=20000, help="number of synthetic documents")
    ap.add_argument("--blocks-per", type=int, default=10, help="content blocks per document")
    ap.add_argument("--repeats", type=int, default=5, help="timing repeats (best-of-N)")
    ap.add_argument("--explain", action="store_true", help="print EXPLAIN QUERY PLAN for hotspots")
    ap.add_argument("--db", type=str, default=None, help="time against an existing DB instead of building one")
    args = ap.parse_args()

    if args.db:
        store = SqliteStore(Path(args.db))
        row = store.conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        if row is None:
            print("error: --db has no users to attribute the ACL query to", flush=True)
            return
        uid = row["id"]
        ndocs = store.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        print(f"timing existing DB {args.db}: {ndocs} documents, user_id={uid}", flush=True)
        run(store, uid, args.repeats, args.explain)
        return

    db_path = Path(tempfile.mkdtemp()) / "perf_bench.db"
    store, uid = build_dataset(db_path, args.docs, args.blocks_per)
    run(store, uid, args.repeats, args.explain)


if __name__ == "__main__":
    main()
