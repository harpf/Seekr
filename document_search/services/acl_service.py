"""Single source of truth for ACL-aware retrieval.

All search/listing code paths MUST filter through `visible_document_ids_subquery`.
Skipping this filter is allowed only via an explicit `bypass_acl=True` flag in
the caller (CLI/system contexts), and never from HTTP request handlers.
"""
from __future__ import annotations


def visible_document_ids_subquery(user_id: int) -> tuple[str, list]:
    """Return a SQL fragment + bound parameters yielding `document_id` rows
    that the given user is allowed to read.

    Visibility rules (OR'd together):
    1. The user is the owner: `documents.owner_principal_id` equals the user's
       own 'user'-type principal.
    2. An ACL row grants 'read' to a principal the user is a member of (via
       `user_groups`) or to the user's own principal directly.

    The returned SQL is intended to be embedded as `... WHERE d.id IN (<sql>)`.
    """
    sql = """
        SELECT d.id AS document_id
        FROM documents d
        LEFT JOIN users u ON u.id = ?
        WHERE
          d.owner_principal_id = u.principal_id
          OR d.id IN (
            SELECT a.document_id FROM document_acl a
            WHERE a.permission = 'read'
              AND (
                a.principal_id = u.principal_id
                OR a.principal_id IN (
                  SELECT g.principal_id FROM user_groups g WHERE g.user_id = ?
                )
              )
          )
    """
    return sql, [user_id, user_id]


def can_read_document_subquery(user_id: int, document_id: int) -> tuple[str, list]:
    """Return a SQL fragment + params that yields exactly one row (the
    `document_id`) iff `user_id` is allowed to read it. Same visibility rules as
    `visible_document_ids_subquery`, narrowed to a single document.

    Intended for single-document permission checks:
        sql, params = can_read_document_subquery(uid, doc_id)
        allowed = conn.execute(sql, params).fetchone() is not None
    """
    sql = """
        SELECT d.id AS document_id
        FROM documents d
        LEFT JOIN users u ON u.id = ?
        WHERE d.id = ?
          AND (
            d.owner_principal_id = u.principal_id
            OR d.id IN (
              SELECT a.document_id FROM document_acl a
              WHERE a.permission = 'read'
                AND (
                  a.principal_id = u.principal_id
                  OR a.principal_id IN (
                    SELECT g.principal_id FROM user_groups g WHERE g.user_id = ?
                  )
                )
            )
          )
    """
    return sql, [user_id, document_id, user_id]


def can_write(conn, user_id: int, document_id: int) -> bool:
    """Return True if `user_id` may mutate `document_id`.

    A user may write if ANY of:
    1. They own the document (`documents.owner_principal_id` == their 'user' principal).
    2. A 'write' ACL row grants them directly or via a group they belong to.
    3. The document is UNMANAGED: it has no owner AND no 'write' ACL rows at all
       (legacy behaviour — every authenticated user could mutate it before ACLs
       were enforced). The first owner assignment or write grant flips it to
       managed and locks out everyone else.

    `conn` is a live sqlite3 connection (e.g. `store.conn`). The function never
    raises on a missing document — it returns False.
    """
    doc = conn.execute(
        "SELECT owner_principal_id FROM documents WHERE id=?", (document_id,)
    ).fetchone()
    if doc is None:
        return False
    owner_principal_id = doc["owner_principal_id"]

    user_principal = conn.execute(
        "SELECT principal_id FROM users WHERE id=?", (user_id,)
    ).fetchone()
    user_principal_id = user_principal["principal_id"] if user_principal else None

    # Rule 1: owner.
    if owner_principal_id is not None and owner_principal_id == user_principal_id:
        return True

    # Rule 2: explicit write grant (direct principal or via group membership).
    granted = conn.execute(
        """
        SELECT 1 FROM document_acl a
        WHERE a.document_id = ?
          AND a.permission = 'write'
          AND (
            a.principal_id = ?
            OR a.principal_id IN (
              SELECT g.principal_id FROM user_groups g WHERE g.user_id = ?
            )
          )
        LIMIT 1
        """,
        (document_id, user_principal_id, user_id),
    ).fetchone()
    if granted is not None:
        return True

    # Rule 3: unmanaged document (no owner AND no write grants at all).
    if owner_principal_id is None:
        any_write = conn.execute(
            "SELECT 1 FROM document_acl WHERE document_id=? AND permission='write' LIMIT 1",
            (document_id,),
        ).fetchone()
        if any_write is None:
            return True

    return False
