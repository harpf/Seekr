from __future__ import annotations

import sqlite3
from collections.abc import Callable

from document_search.index.sqlite_store import SqliteStore


class FtsQueryError(ValueError):
    """Raised when an FTS MATCH query cannot be parsed by SQLite."""


def build_match_query(
    query: str,
    filetype: str | None = None,
    block_type: str | None = None,
) -> str | None:
    q = (query or "").strip()
    if not q or q == "*":
        return None
    clauses = [q]
    if filetype:
        extensions = [e.strip().lstrip(".") for e in filetype.split(",") if e.strip()]
        if len(extensions) == 1:
            clauses.append(f"extension:{extensions[0]}")
        elif len(extensions) > 1:
            or_parts = " OR ".join(f"extension:{e}" for e in extensions)
            clauses.append(f"({or_parts})")
    if block_type:
        clauses.append(f"block_type:{block_type}")
    return " AND ".join(clauses)


def _rrf_fuse(
    lists: dict[str, tuple[list[int], float]],
    k: int = 60,
) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion of several ranked document_id lists.

    `lists` maps a label -> (ranked_doc_ids, weight). Each document's fused
    score is the weighted sum over lists of 1/(k + rank), where rank is the
    1-based position in that list. Documents absent from a list contribute 0
    from it. Returns (document_id, score) sorted by score descending, ties
    broken by document_id for determinism.
    """
    scores: dict[int, float] = {}
    for ranked, weight in lists.values():
        for idx, doc_id in enumerate(ranked):
            rank = idx + 1
            scores[doc_id] = scores.get(doc_id, 0.0) + weight * (1.0 / (k + rank))
    return sorted(scores.items(), key=lambda t: (-t[1], t[0]))


def _recency_boost(modified_at: str | None, now_iso: str | None = None) -> float:
    """Map a document's modified_at into a [0, 1] recency score.

    1.0 == modified now, decaying with a ~365-day half-life. Unparseable or
    missing dates score 0.0 (no boost, never an error).
    """
    from datetime import UTC, datetime
    if not modified_at:
        return 0.0
    try:
        ts = datetime.fromisoformat(modified_at)
    except (ValueError, TypeError):
        return 0.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    now = datetime.fromisoformat(now_iso) if now_iso else datetime.now(tz=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
    # Exponential decay, half-life 365 days.
    return 0.5 ** (age_days / 365.0)


def _field_boost(query: str, filename: str) -> float:
    """Boost for query terms appearing in the filename/title.

    Returns the fraction of distinct query terms present in the filename, in
    [0, 1]. A filename match is a strong relevance signal that BM25 over body
    text under-weights.
    """
    import re
    terms = {t for t in re.findall(r"[a-z0-9]+", (query or "").lower()) if len(t) > 2}
    if not terms:
        return 0.0
    name = (filename or "").lower()
    hits = sum(1 for t in terms if t in name)
    return hits / len(terms)


def _browse_all(
    store: SqliteStore,
    filetype: str | None,
    path_filter: str | None,
    block_type: str | None,
    modified_from: str | None,
    modified_to: str | None,
    tags: list[str],
    user_id: int | None,
    limit: int,
    offset: int = 0,
    bypass_acl: bool = False,
):
    where = ["1=1"]
    params: list = []

    if filetype:
        exts = ["." + e.strip().lstrip(".") for e in filetype.split(",") if e.strip()]
        placeholders = ",".join("?" * len(exts))
        where.append(f"d.extension IN ({placeholders})")
        params.extend(exts)
    if path_filter:
        where.append("d.path LIKE ?")
        params.append(path_filter + "%")
    if block_type:
        where.append("cb.block_type = ?")
        params.append(block_type)
    if modified_from:
        where.append("d.modified_at >= ?")
        params.append(modified_from)
    if modified_to:
        where.append("d.modified_at <= ?")
        params.append(modified_to)
    if tags and user_id is not None:
        tag_ph = ",".join("?" * len(tags))
        where.append(
            f"""d.id IN (
                SELECT dt.document_id FROM document_tags dt
                JOIN user_tags ut ON ut.id = dt.tag_id
                WHERE dt.user_id = ? AND ut.name IN ({tag_ph})
                GROUP BY dt.document_id
                HAVING COUNT(DISTINCT ut.name) = ?
            )"""
        )
        params.extend([user_id] + tags + [len(tags)])
    if not bypass_acl:
        from document_search.services.acl_service import visible_document_ids_subquery
        acl_sql, acl_params = visible_document_ids_subquery(user_id)
        where.append(f"d.id IN ({acl_sql})")
        params.extend(acl_params)

    # Returns one row per content_block, not per document.
    # LIMIT therefore limits blocks — callers that group by document_id
    # (see api_search in app.py) handle this correctly.
    sql = f"""
        SELECT NULL AS rank,
               d.id AS document_id, d.filename, d.path, d.extension,
               d.modified_at, d.indexed_at,
               cb.block_type, cb.block_number,
               NULL AS snippet
        FROM documents d
        JOIN content_blocks cb ON cb.document_id = d.id
        WHERE {" AND ".join(where)}
        ORDER BY d.modified_at DESC
        LIMIT ? OFFSET ?
    """
    params.append(limit)
    params.append(offset)
    return store.conn.execute(sql, tuple(params)).fetchall()


def search(
    store: SqliteStore,
    query: str,
    limit: int = 20,
    offset: int = 0,
    filetype: str | None = None,
    path_filter: str | None = None,
    block_type: str | None = None,
    modified_from: str | None = None,
    modified_to: str | None = None,
    tags: list[str] | None = None,
    user_id: int | None = None,
    bypass_acl: bool = False,
    mode: str = "keyword",
    embed_fn: Callable[[str], list[float] | None] | None = None,
    bm25_weight: float = 1.0,
    vector_weight: float = 1.0,
):
    if user_id is None and not bypass_acl:
        raise ValueError("user_id is required unless bypass_acl=True is set explicitly")

    tags = [t.lower().strip() for t in (tags or [])]
    keyword_rows = _keyword_rows(
        store, query, limit, offset, filetype, path_filter,
        block_type, modified_from, modified_to, tags, user_id, bypass_acl,
    )
    if mode == "keyword":
        return keyword_rows
    if embed_fn is None:
        return keyword_rows
    query_vec = embed_fn(query)
    if not query_vec:
        return keyword_rows
    return _hybrid_rows(
        store, keyword_rows, query, query_vec, limit, filetype,
        path_filter, block_type, modified_from, modified_to, tags,
        user_id, bypass_acl, mode, bm25_weight, vector_weight,
    )


def _keyword_rows(
    store: SqliteStore,
    query: str,
    limit: int,
    offset: int,
    filetype: str | None,
    path_filter: str | None,
    block_type: str | None,
    modified_from: str | None,
    modified_to: str | None,
    tags: list[str],
    user_id: int | None,
    bypass_acl: bool,
):
    match_query = build_match_query(query, filetype, block_type)

    if match_query is None:
        return _browse_all(
            store, filetype, path_filter, block_type,
            modified_from, modified_to, tags, user_id, limit, offset,
            bypass_acl=bypass_acl,
        )

    # snippet() column 7 = text (0-based FTS5 index order in content_fts)
    sql = """
        SELECT c.rank, d.id as document_id, d.filename, d.path, d.extension,
               d.modified_at, d.indexed_at,
               b.block_type, b.block_number,
               snippet(content_fts, 7, '[', ']', ' … ', 12) AS snippet
        FROM content_fts c
        JOIN documents d ON d.id = c.document_id
        JOIN content_blocks b ON b.id = c.block_id
        WHERE content_fts MATCH ?
    """
    params: list = [match_query]
    if path_filter:
        sql += " AND d.path LIKE ?"
        params.append(path_filter + "%")
    if modified_from:
        sql += " AND d.modified_at >= ?"
        params.append(modified_from)
    if modified_to:
        sql += " AND d.modified_at <= ?"
        params.append(modified_to)
    if tags and user_id is not None:
        tag_ph = ",".join("?" * len(tags))
        sql += f"""
            AND d.id IN (
                SELECT dt.document_id FROM document_tags dt
                JOIN user_tags ut ON ut.id = dt.tag_id
                WHERE dt.user_id = ? AND ut.name IN ({tag_ph})
                GROUP BY dt.document_id
                HAVING COUNT(DISTINCT ut.name) = ?
            )"""
        params.extend([user_id] + tags + [len(tags)])
    if not bypass_acl:
        from document_search.services.acl_service import visible_document_ids_subquery
        acl_sql, acl_params = visible_document_ids_subquery(user_id)
        sql += f" AND d.id IN ({acl_sql})"
        params.extend(acl_params)
    sql += " ORDER BY c.rank LIMIT ? OFFSET ?"
    params.append(limit)
    params.append(offset)
    try:
        return store.conn.execute(sql, tuple(params)).fetchall()
    except sqlite3.OperationalError as exc:
        raise FtsQueryError(str(exc)) from exc


def _visible_doc_ids(store, user_id, bypass_acl, path_filter, modified_from, modified_to):
    """All document_ids the user may read, after the same scalar filters the
    keyword path applies (path/date), used as the vector candidate pool."""
    sql = "SELECT d.id FROM documents d WHERE 1=1"
    params: list = []
    if path_filter:
        sql += " AND d.path LIKE ?"
        params.append(path_filter + "%")
    if modified_from:
        sql += " AND d.modified_at >= ?"
        params.append(modified_from)
    if modified_to:
        sql += " AND d.modified_at <= ?"
        params.append(modified_to)
    if not bypass_acl:
        from document_search.services.acl_service import visible_document_ids_subquery
        acl_sql, acl_params = visible_document_ids_subquery(user_id)
        sql += f" AND d.id IN ({acl_sql})"
        params.extend(acl_params)
    return [r[0] for r in store.conn.execute(sql, tuple(params)).fetchall()]


def _vector_doc_order(store, query_vec, candidate_doc_ids):
    """Rank candidate documents by best (max) block cosine vs the query vector."""
    from document_search.services.embedding_service import cosine_similarity
    best: dict[int, float] = {}
    for _block_id, doc_id, vec in store.iter_block_embeddings(candidate_doc_ids):
        if len(vec) != len(query_vec):
            continue
        sim = cosine_similarity(query_vec, vec)
        if doc_id not in best or sim > best[doc_id]:
            best[doc_id] = sim
    return [doc_id for doc_id, _ in sorted(best.items(), key=lambda t: (-t[1], t[0]))]


def _hybrid_rows(store, keyword_rows, query, query_vec, limit, filetype, path_filter,
                 block_type, modified_from, modified_to, tags, user_id, bypass_acl,
                 mode, bm25_weight, vector_weight):
    bm25_order: list[int] = []
    seen: set[int] = set()
    for r in keyword_rows:
        did = r["document_id"]
        if did not in seen:
            seen.add(did)
            bm25_order.append(did)

    candidates = _visible_doc_ids(store, user_id, bypass_acl, path_filter, modified_from, modified_to)
    vector_order = _vector_doc_order(store, query_vec, candidates)

    lists: dict[str, tuple[list[int], float]] = {"vector": (vector_order, vector_weight)}
    if mode == "hybrid":
        lists["bm25"] = (bm25_order, bm25_weight)
    fused = _rrf_fuse(lists)

    meta = {}
    fused_ids = [d for d, _ in fused]
    if fused_ids:
        placeholders = ",".join("?" * len(fused_ids))
        for row in store.conn.execute(
            f"SELECT id, filename, modified_at FROM documents WHERE id IN ({placeholders})",
            tuple(fused_ids),
        ).fetchall():
            meta[row["id"]] = (row["filename"], row["modified_at"])

    boosted: list[tuple[int, float]] = []
    for doc_id, score in fused:
        filename, modified_at = meta.get(doc_id, ("", None))
        score += 0.1 * _recency_boost(modified_at)
        score += 0.2 * _field_boost(query, filename)
        boosted.append((doc_id, score))
    boosted.sort(key=lambda t: (-t[1], t[0]))
    ordered_ids = [d for d, _ in boosted][:limit]

    return _rows_for_doc_ids(store, ordered_ids, keyword_rows)


def _rows_for_doc_ids(store, ordered_ids, keyword_rows):
    """Materialise output rows (same shape as the keyword path) in fused doc order.
    Reuses BM25 snippet rows we already have; for docs that only surfaced via the
    vector list, emit a snippet-less representative block."""
    by_doc: dict[int, list] = {}
    for r in keyword_rows:
        by_doc.setdefault(r["document_id"], []).append(r)

    out = []
    for doc_id in ordered_ids:
        if doc_id in by_doc:
            out.extend(by_doc[doc_id])
            continue
        rep = store.conn.execute(
            """
            SELECT NULL AS rank, d.id AS document_id, d.filename, d.path, d.extension,
                   d.modified_at, d.indexed_at, cb.block_type, cb.block_number,
                   NULL AS snippet
            FROM documents d
            JOIN content_blocks cb ON cb.document_id = d.id
            WHERE d.id = ?
            ORDER BY cb.block_number LIMIT 1
            """,
            (doc_id,),
        ).fetchone()
        if rep is not None:
            out.append(rep)
    return out


def count_documents(
    store: SqliteStore,
    query: str,
    filetype: str | None = None,
    path_filter: str | None = None,
    block_type: str | None = None,
    modified_from: str | None = None,
    modified_to: str | None = None,
    tags: list[str] | None = None,
    user_id: int | None = None,
    bypass_acl: bool = False,
) -> int:
    """Return the number of distinct documents matching the same filters as search()."""
    if user_id is None and not bypass_acl:
        raise ValueError("user_id is required unless bypass_acl=True is set explicitly")

    tags = [t.lower().strip() for t in (tags or [])]
    match_query = build_match_query(query, filetype, block_type)

    if match_query is None:
        where = ["1=1"]
        params: list = []
        if filetype:
            exts = ["." + e.strip().lstrip(".") for e in filetype.split(",") if e.strip()]
            placeholders = ",".join("?" * len(exts))
            where.append(f"d.extension IN ({placeholders})")
            params.extend(exts)
        if path_filter:
            where.append("d.path LIKE ?")
            params.append(path_filter + "%")
        # A document counts only if it has at least one (matching) content block —
        # mirroring browse, which emits block-level rows. EXISTS preserves that
        # semantic with an indexed lookup instead of scanning every block and
        # de-duplicating (the old `JOIN content_blocks` + COUNT(DISTINCT)).
        if block_type:
            where.append(
                "EXISTS (SELECT 1 FROM content_blocks cb "
                "WHERE cb.document_id = d.id AND cb.block_type = ?)"
            )
            params.append(block_type)
        else:
            where.append("EXISTS (SELECT 1 FROM content_blocks cb WHERE cb.document_id = d.id)")
        if modified_from:
            where.append("d.modified_at >= ?")
            params.append(modified_from)
        if modified_to:
            where.append("d.modified_at <= ?")
            params.append(modified_to)
        if tags and user_id is not None:
            tag_ph = ",".join("?" * len(tags))
            where.append(
                f"""d.id IN (
                    SELECT dt.document_id FROM document_tags dt
                    JOIN user_tags ut ON ut.id = dt.tag_id
                    WHERE dt.user_id = ? AND ut.name IN ({tag_ph})
                    GROUP BY dt.document_id
                    HAVING COUNT(DISTINCT ut.name) = ?
                )"""
            )
            params.extend([user_id] + tags + [len(tags)])
        if not bypass_acl:
            from document_search.services.acl_service import visible_document_ids_subquery
            acl_sql, acl_params = visible_document_ids_subquery(user_id)
            where.append(f"d.id IN ({acl_sql})")
            params.extend(acl_params)
        sql = f"""
            SELECT COUNT(*)
            FROM documents d
            WHERE {" AND ".join(where)}
        """
        return int(store.conn.execute(sql, tuple(params)).fetchone()[0])

    # No `JOIN content_blocks` here: content_fts already holds one row per block
    # and nothing below references a content_blocks column, so the join was pure
    # overhead (an index probe per match).
    sql = """
        SELECT COUNT(DISTINCT c.document_id)
        FROM content_fts c
        JOIN documents d ON d.id = c.document_id
        WHERE content_fts MATCH ?
    """
    params = [match_query]
    if path_filter:
        sql += " AND d.path LIKE ?"
        params.append(path_filter + "%")
    if modified_from:
        sql += " AND d.modified_at >= ?"
        params.append(modified_from)
    if modified_to:
        sql += " AND d.modified_at <= ?"
        params.append(modified_to)
    if tags and user_id is not None:
        tag_ph = ",".join("?" * len(tags))
        sql += f"""
            AND d.id IN (
                SELECT dt.document_id FROM document_tags dt
                JOIN user_tags ut ON ut.id = dt.tag_id
                WHERE dt.user_id = ? AND ut.name IN ({tag_ph})
                GROUP BY dt.document_id
                HAVING COUNT(DISTINCT ut.name) = ?
            )"""
        params.extend([user_id] + tags + [len(tags)])
    if not bypass_acl:
        from document_search.services.acl_service import visible_document_ids_subquery
        acl_sql, acl_params = visible_document_ids_subquery(user_id)
        sql += f" AND d.id IN ({acl_sql})"
        params.extend(acl_params)
    try:
        return int(store.conn.execute(sql, tuple(params)).fetchone()[0])
    except sqlite3.OperationalError as exc:
        raise FtsQueryError(str(exc)) from exc
