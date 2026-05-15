from __future__ import annotations

from document_search.index.sqlite_store import SqliteStore


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
        LIMIT ?
    """
    params.append(limit)
    return store.conn.execute(sql, tuple(params)).fetchall()


def search(
    store: SqliteStore,
    query: str,
    limit: int = 20,
    filetype: str | None = None,
    path_filter: str | None = None,
    block_type: str | None = None,
    modified_from: str | None = None,
    modified_to: str | None = None,
    tags: list[str] | None = None,
    user_id: int | None = None,
):
    tags = [t.lower().strip() for t in (tags or [])]
    match_query = build_match_query(query, filetype, block_type)

    if match_query is None:
        return _browse_all(
            store, filetype, path_filter, block_type,
            modified_from, modified_to, tags, user_id, limit,
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
    sql += " ORDER BY c.rank LIMIT ?"
    params.append(limit)
    return store.conn.execute(sql, tuple(params)).fetchall()
