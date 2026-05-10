from __future__ import annotations

from document_search.index.sqlite_store import SqliteStore


def build_match_query(query: str, filetype: str | None = None, path_filter: str | None = None, block_type: str | None = None) -> str:
    clauses = [query]
    if filetype:
        clauses.append(f"extension:{filetype}")
    if path_filter:
        clauses.append(f"path:{path_filter}*")
    if block_type:
        clauses.append(f"block_type:{block_type}")
    return " AND ".join(clauses)


def search(
    store: SqliteStore,
    query: str,
    limit: int = 20,
    filetype: str | None = None,
    path_filter: str | None = None,
    block_type: str | None = None,
    modified_from: str | None = None,
    modified_to: str | None = None,
):
    match_query = build_match_query(query, filetype, path_filter, block_type)
    sql = """
        SELECT c.rank, d.id as document_id, d.filename, d.path, d.extension, d.modified_at, d.indexed_at,
               b.block_type, b.block_number,
               snippet(content_fts, 7, '[', ']', ' … ', 12) AS snippet
        FROM content_fts c
        JOIN documents d ON d.id = c.document_id
        JOIN content_blocks b ON b.id = c.block_id
        WHERE content_fts MATCH ?
    """
    params: list[object] = [match_query]
    if modified_from:
        sql += " AND d.modified_at >= ?"
        params.append(modified_from)
    if modified_to:
        sql += " AND d.modified_at <= ?"
        params.append(modified_to)
    sql += " ORDER BY c.rank LIMIT ?"
    params.append(limit)
    return store.conn.execute(sql, tuple(params)).fetchall()
