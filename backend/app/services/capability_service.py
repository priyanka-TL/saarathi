"""The sidebar capability document.

Responsible for: resolving which capabilities and agents a caller can see, with
most-specific-wins scope inheritance.
Used by: GET /api/ui/capabilities.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text

from app.core.logger import get_logger
from app.domain.core import UserContext
from app.domain.scope import DEFAULT_SCOPE, scope_for_user
from app.repositories.scope_sql import (
    scope_candidate_filter,
    scope_precedence_order_by,
)

logger = get_logger("capability_service")

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

# DISTINCT ON picks the most specific row per key; Postgres requires that
# expression to lead the ORDER BY, so display ordering moves to the outer query.
#
# THE STATUS FILTER MUST STAY IN THE OUTER QUERY. Filtering before DISTINCT ON
# drops a tenant's disabled row from the candidate set, letting the default win
# -- so disabling a capability would SHOW THE DEFAULT rather than hide it.
_CAPABILITIES_SQL = text(f"""
    SELECT * FROM (
        SELECT DISTINCT ON (c.key)
               c.id, c.key, c.name, c.description, c.icon, c.badge,
               c.status, c.display_order, c.metadata
        FROM capabilities c
        WHERE {scope_candidate_filter("c")}
        ORDER BY c.key,
                 {scope_precedence_order_by("c")}
    ) resolved
    WHERE resolved.status <> 'disabled'
    ORDER BY resolved.display_order, resolved.key
""")

# Membership inherits the capability's scope, so it needs no filter of its own.
# The agent's CONFIG is scoped, and is resolved with the same most-specific-wins
# rule via the LATERAL below.
_AGENTS_SQL = text(f"""
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
          AND {scope_candidate_filter("ac")}
        ORDER BY {scope_precedence_order_by("ac")}
        LIMIT 1
    ) cfg ON TRUE
    WHERE ca.capability_id = ANY(:capability_ids)
      AND ca.is_visible
      AND a.status = 'enabled'
      -- Gate 5: with Mitra off, a remote_flow agent has nothing to serve it,
      -- so advertising it renders a button that raises at click time.
      AND (:mitra_enabled OR a.agent_type <> 'remote_flow')
    ORDER BY ca.display_order, a.key
""")


def _scope(user: Optional[UserContext]) -> Tuple[str, str]:
    """The caller's (tenant_id, organization_id). See domain/scope.py."""
    return scope_for_user(user)


def _action_from(metadata: Optional[Dict[str, Any]], fallback: Dict[str, Any]) -> Dict[str, Any]:
    """`action` lives inside `metadata` rather than its own column, so new
    presentation keys need no migration. A row with no action falls back rather
    than producing a capability nothing can do."""
    action = (metadata or {}).get("action")
    return action if isinstance(action, dict) and action.get("type") else fallback


def _access_permits(config: Dict[str, Any], user: Optional[UserContext]) -> bool:
    """Gate 4, via the domain layer's own AccessSpec.

    An unparseable config is treated as NOT permitted: rendering a button the
    router will refuse is worse than rendering nothing.
    """
    from app.domain.agent_spec import AccessSpec

    try:
        access = AccessSpec(**(config.get("access") or {}))
    except Exception:  # noqa: BLE001 -- a malformed spec must not 500 the sidebar
        logger.warning("capability: unparseable access spec; hiding agent")
        return False
    return access.matches(user)


def resolve_for_user(session, user: Optional[UserContext],
                     mitra_enabled: bool = True) -> Dict[str, Any]:
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
        _AGENTS_SQL,
        {**params,
         "capability_ids": [r.id for r in capability_rows],
         "mitra_enabled": mitra_enabled},
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
            # From the join, not the metadata blob, so renaming an agent key
            # cannot leave a stale copy behind.
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
