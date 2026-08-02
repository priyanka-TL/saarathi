"""Resolves the sidebar's capability document for one caller's scope.

Answers the EXACT shape the frontend already consumes, so making this
database-driven changed no frontend code at all:

    {"version": 1, "capabilities": [ {..., "agents": [...]}, ... ]}

SCOPE RESOLUTION: most specific wins
------------------------------------
Every `capabilities` row carries `tenant_id` + `organization_id`, defaulting to
the sentinel 'default'. Three levels are consulted per capability KEY:

    (tenant, org)  >  (tenant, 'default')  >  ('default', 'default')

so a tenant inherits the default catalogue until it inserts a row of its own,
and onboarding a tenant costs zero writes. Implemented as one DISTINCT ON,
because picking the winner per key in SQL is what keeps this two queries rather
than one per capability -- one request holds one thread and one DB connection
for its whole lifetime (see app/main.py), so an N+1 here would be the wrong
shape at any size.

FOUR GATES DECIDE WHETHER AN AGENT APPEARS, and all four must pass:

  1. agents.status = 'enabled'          -- the global kill switch
  2. capability_agents.is_visible       -- hidden in THIS capability
  3. an ACTIVE agent_configs row exists in the caller's scope
  4. spec.access.matches(user)          -- AccessSpec, evaluated in Python

Gate 4 cannot be SQL: AccessSpec lives inside the JSONB config and its rules
(tenant_codes / organization_codes / required_roles, ANDed, empty == no
restriction) are already implemented in the domain layer. It is applied by
calling that same method -- there must never be a second access check to drift
out of step with it.

This module is deliberately framework-free (the .importlinter contracts forbid
app.services importing fastapi/starlette): it takes a Session and a
UserContext and returns plain dicts.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text

from app.core.logger import get_logger
from app.domain.core import UserContext

logger = get_logger("capability_service")

#: Matches migration 0006. The sentinel meaning "applies to everyone".
DEFAULT_SCOPE = "default"

#: Bumped only if the document's SHAPE changes in a way the frontend's
#: normaliser would have to know about. Content changes are not a version bump.
DOCUMENT_VERSION = 1

#: DB vocabulary -> API vocabulary. `active` becomes `enabled` because that is
#: what the frontend's status set uses; `disabled` never leaves here (those
#: rows are filtered out server-side) but is mapped for completeness.
_STATUS_OUT = {
    "active": "enabled",
    "coming_soon": "coming_soon",
    "disabled": "disabled",
}

# DISTINCT ON (key) with the ORDER BY below picks the most specific row per
# capability key. The two CASE expressions rank scope specificity: an exact
# organization match sorts first, then a tenant-wide row, then the default.
# Postgres requires the DISTINCT ON expression to lead the ORDER BY, which is
# why `key` comes first and the display ordering is applied by the outer query.
#
# THE STATUS FILTER BELONGS IN THE OUTER QUERY, NOT THE INNER ONE. Filtering
# `status <> 'disabled'` before DISTINCT ON would drop a tenant's disabled row
# from the candidate set, and the default row would then win -- so a tenant
# disabling a capability would SEE THE DEFAULT ONE INSTEAD OF HIDING IT, which
# is the exact opposite of what it asked for. Resolve the winner first, then
# decide whether the winner is showable.
_CAPABILITIES_SQL = text("""
    SELECT * FROM (
        SELECT DISTINCT ON (c.key)
               c.id, c.key, c.name, c.description, c.icon, c.badge,
               c.status, c.display_order, c.metadata
        FROM capabilities c
        WHERE c.tenant_id IN (:tenant_id, :default_scope)
          AND c.organization_id IN (:organization_id, :default_scope)
        ORDER BY c.key,
                 CASE WHEN c.organization_id = :organization_id THEN 0 ELSE 1 END,
                 CASE WHEN c.tenant_id = :tenant_id THEN 0 ELSE 1 END
    ) resolved
    WHERE resolved.status <> 'disabled'
    ORDER BY resolved.display_order, resolved.key
""")

# Membership inherits the capability's scope, so this needs no scope filter of
# its own -- it is joined against the ids the query above already resolved.
# The agent's CONFIG, however, is scoped, and is resolved with the same
# most-specific-wins rule via a LATERAL subquery.
_AGENTS_SQL = text("""
    SELECT ca.capability_id,
           a.key                                        AS agent_key,
           COALESCE(ca.label_override, a.name)          AS label,
           a.description                                AS description,
           a.icon                                       AS icon,
           ca.display_order                             AS display_order,
           ca.metadata                                  AS metadata,
           cfg.config                                   AS config
    FROM capability_agents ca
    JOIN agents a ON a.id = ca.agent_id
    JOIN LATERAL (
        SELECT ac.config
        FROM agent_configs ac
        WHERE ac.agent_id = a.id
          AND ac.is_active
          AND ac.tenant_id IN (:tenant_id, :default_scope)
          AND ac.organization_id IN (:organization_id, :default_scope)
        ORDER BY CASE WHEN ac.organization_id = :organization_id THEN 0 ELSE 1 END,
                 CASE WHEN ac.tenant_id = :tenant_id THEN 0 ELSE 1 END
        LIMIT 1
    ) cfg ON TRUE
    WHERE ca.capability_id = ANY(:capability_ids)
      AND ca.is_visible
      AND a.status = 'enabled'
    ORDER BY ca.display_order, a.key
""")


def _scope(user: Optional[UserContext]) -> Tuple[str, str]:
    """The caller's (tenant_id, organization_id).

    An anonymous caller resolves to the default scope, which is the same thing
    an unknown tenant resolves to -- there is no tenants table to validate
    against (tenants belong to the user service), so an unrecognised code
    simply matches no tenant-specific row and inherits the default. Inert, not
    an error.
    """
    if user is None:
        return DEFAULT_SCOPE, DEFAULT_SCOPE
    return (user.tenant_code or DEFAULT_SCOPE), (user.active_org_id or DEFAULT_SCOPE)


def _action_from(metadata: Optional[Dict[str, Any]], fallback: Dict[str, Any]) -> Dict[str, Any]:
    """`action` lives inside `metadata` rather than in its own column.

    Keeps the table to the agreed columns while leaving room for future
    presentation keys without another migration. A row with no action falls
    back rather than producing a capability nothing can do.
    """
    action = (metadata or {}).get("action")
    return action if isinstance(action, dict) and action.get("type") else fallback


def _access_permits(config: Dict[str, Any], user: Optional[UserContext]) -> bool:
    """Gate 4, via the domain layer's own AccessSpec.

    A config that will not parse is treated as NOT permitted: an agent whose
    spec is unreadable cannot have its access rules evaluated, and rendering a
    button the router will refuse is worse than rendering nothing.
    """
    from app.domain.agent_spec import AccessSpec

    try:
        access = AccessSpec(**(config.get("access") or {}))
    except Exception:  # noqa: BLE001 -- a malformed spec must not 500 the sidebar
        logger.warning("capability: unparseable access spec; hiding agent")
        return False
    return access.matches(user)


def resolve_for_user(session, user: Optional[UserContext]) -> Dict[str, Any]:
    """The capability document for this caller's tenant and organization."""
    tenant_id, organization_id = _scope(user)
    params = {
        "tenant_id": tenant_id,
        "organization_id": organization_id,
        "default_scope": DEFAULT_SCOPE,
    }

    capability_rows = session.execute(_CAPABILITIES_SQL, params).fetchall()
    if not capability_rows:
        return {"version": DOCUMENT_VERSION, "capabilities": []}

    agent_rows = session.execute(
        _AGENTS_SQL, {**params, "capability_ids": [r.id for r in capability_rows]}
    ).fetchall()

    by_capability: Dict[Any, List[Dict[str, Any]]] = {}
    for row in agent_rows:
        if not _access_permits(row.config or {}, user):
            continue
        action = _action_from(row.metadata, {"type": "start_agent"})
        by_capability.setdefault(row.capability_id, []).append({
            "id": row.agent_key,
            "label": row.label,
            "description": row.description or "",
            "icon": row.icon or "",
            "status": "enabled",
            "order": row.display_order,
            "visible": True,
            # `agentKey` is injected from the join rather than stored in the
            # metadata JSON, so renaming an agent key cannot leave a stale copy
            # of it behind in a blob nothing validates.
            "action": {**action, "agentKey": row.agent_key},
        })

    capabilities = []
    for row in capability_rows:
        capabilities.append({
            "id": row.key,
            "title": row.name,
            "description": row.description or "",
            "icon": row.icon or "",
            "badge": row.badge,
            "status": _STATUS_OUT.get(row.status, "enabled"),
            "order": row.display_order,
            "visible": True,
            "action": _action_from(row.metadata, {"type": "display_card"}),
            # An empty list is legitimate -- SG Commons Portal has no agents --
            # and the frontend omits the actions block entirely rather than
            # rendering an empty one.
            "agents": by_capability.get(row.id, []),
        })

    return {"version": DOCUMENT_VERSION, "capabilities": capabilities}
