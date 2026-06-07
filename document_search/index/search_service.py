from __future__ import annotations

import sqlite3

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
):
    if user_id is None and not bypass_acl:
        raise ValueError("user_id is required unless bypass_acl=True is set explicitly")

    tags = [t.lower().strip() for t in (tags or [])]
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
        sql = f"""
            SELECT COUNT(DISTINCT d.id)
            FROM documents d
            JOIN content_blocks cb ON cb.document_id = d.id
            WHERE {" AND ".join(where)}
        """
        return int(store.conn.execute(sql, tuple(params)).fetchone()[0])

    sql = """
        SELECT COUNT(DISTINCT c.document_id)
        FROM content_fts c
        JOIN documents d ON d.id = c.document_id
        JOIN content_blocks b ON b.id = c.block_id
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
