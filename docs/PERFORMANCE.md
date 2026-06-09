# Performance — how to pursue it correctly

Performance work here follows one loop, always backed by numbers:

> **measure → identify the hotspot → fix one thing → verify before/after → repeat**

Never optimise by guessing. Every change that claims a speedup must show a
before/after measurement from the benchmark harness below.

## The benchmark harness

`scripts/perf_bench.py` builds a synthetic index at a chosen scale and times the
**real code paths** (the same functions the API calls: `search`,
`count_documents`, `visible_document_ids_subquery`, the `/api/status`
aggregates). It is a developer tool — not part of the test suite or CI.

```bash
# Build 20k docs x 10 blocks and print best-of-5 timings:
PYTHONPATH=. python scripts/perf_bench.py --docs 20000 --blocks-per 10

# Larger, with query plans for the hotspots:
PYTHONPATH=. python scripts/perf_bench.py --docs 50000 --explain

# Time an existing real database (read-only-ish; builds nothing):
PYTHONPATH=. python scripts/perf_bench.py --db ./document_index.db
```

Workflow for any optimisation:
1. Run the harness at a representative scale → record the baseline row.
2. Make exactly one change.
3. Re-run → compare. Keep the change only if the number moved and correctness
   tests (`pytest -q tests/test_app_search*.py tests/test_acl_enforcement.py`)
   still pass.
4. Record the new number in the table below.

## Baseline (20k docs / 200k blocks, best-of-5, dev laptop)

| operation      | ms    | notes |
|----------------|-------|-------|
| visibility     | 4.2   | ACL set-build (`visible_document_ids_subquery`) |
| status.docs    | 7.7   | `/api/status` document count |
| status.blocks  | 15.5  | `/api/status` block count |
| status.size    | 7.4   | `/api/status` size sum |
| count.browse   | 13.1  | total count for an empty query |
| **count.fts**  | **285** | total count for a broad keyword query |
| **search.broad** | **250** | keyword search matching ~every doc (worst case) |
| search.rare    | 8.8   | selective keyword search |

Numbers are machine-relative — compare deltas on the same machine, not absolute
values across machines.

## Landed optimisations

- **ACL visibility rewrite** (`perf(acl)`): the `OR d.owner=… OR d.id IN
  (correlated subquery)` form was O(N²) (~6.6 s for a doc count at 5k docs).
  Rewritten as an index-friendly UNION (`idx_acl_principal` ∪ `idx_docs_owner`)
  → ~2 ms. This fixed every ACL-gated path (search, browse, status, folders).
- **Cheaper total-count** (`perf(search)`): browse count uses an indexed
  `EXISTS` instead of `JOIN content_blocks` + `COUNT(DISTINCT)`; the FTS count
  dropped an unused `content_blocks` join.
- **Tuned SQLite connection**: WAL, `synchronous=NORMAL`, 32 MB page cache,
  256 MB mmap, `temp_store=MEMORY`, `busy_timeout=5000` (already in place).

## Backlog (measured, ranked by impact)

| candidate | measured cost | expected after | tradeoff |
|---|---|---|---|
| **Capped total-count** for broad FTS queries (count distinct docs up to N, show "N+") | count.fts 285 ms | a few ms | `X-Total-Count` becomes approximate (e.g. "200+"), like web search engines — a UX change |
| **search.broad** (BM25 over a term in ~every doc) | 250 ms | hard to reduce | inherent to ranking all matches; selective queries are already ~9 ms. Possible: cap candidate set, or a `rank`-bounded query |
| **External-content FTS5** (drop the duplicated text column) | — (storage) | ~½ DB size | one-time migration; rebuilds the FTS index |
| **Semantic/hybrid search at scale** | brute-force cosine, no ANN | — | needs `sqlite-vec`/FAISS; only relevant once embeddings are enabled on a large corpus |

Investigated and intentionally **not** changed (measured cheap):
- `_backfill_acl` on store construction: 4 ms at 20k docs — negligible.
- Search result assembly: marks/tags already fetched in one batched query (no N+1).
