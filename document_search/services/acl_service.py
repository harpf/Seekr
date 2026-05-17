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
