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
           cfg.config                                   AS config,
           -- Gate 5, as a FLAG rather than a filter. Selecting gated rows and
           -- dropping them in Python is what lets the caller tell "this card
           -- has no agents configured" (legitimate -- SG Commons, or a tenant's
           -- own new card) from "this card's agents were all filtered out"
           -- (a dead end, hidden below).
           --
           -- COMPARED AS TEXT, not as the enum: a bare literal is cast to
           -- agent_type_enum and raises InvalidTextRepresentation for a label
           -- the enum does not carry yet, so this would break on a database
           -- whose migrations lag the code.
           a.agent_type::text                           AS agent_type
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


def _launches_an_agent(action: Dict[str, Any]) -> bool:
    """Whether a CARD's own action starts an agent.

    Requires a non-empty `agentKey`, matching the frontend: `start_agent`
    without one is normalised to an inert `none` there, because a card that
    routed to `undefined` would reset the conversation and pin nothing. A card
    that cannot actually route must keep its nested buttons.
    """
    return (
        action.get("type") == "start_agent"
        and isinstance(action.get("agentKey"), str)
        and bool(action["agentKey"].strip())
    )


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
                     mitra_enabled: bool = True,
                     saathi_enabled: bool = True) -> Dict[str, Any]:
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
         "mitra_enabled": mitra_enabled,
         "saathi_enabled": saathi_enabled},
    ).fetchall()

    #: agent_type -> whether that provider is enabled in this deployment.
    provider_enabled = {"remote_flow": mitra_enabled, "saathi_flow": saathi_enabled}

    by_capability: Dict[Any, List[Dict[str, Any]]] = {}
    #: Capabilities that HAVE membership, whatever survives filtering below.
    #: The distinction between "no agents configured" and "every agent filtered
    #: out" is what decides whether a card is legitimate or a dead end.
    configured: set = set()

    for row in agent_rows:
        configured.add(row.capability_id)
        # Gate 5: the provider is switched off for this deployment.
        if not provider_enabled.get(row.agent_type, True):
            continue
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
        agents = by_capability.get(row.id, [])

        # A CARD WHOSE AGENTS WERE ALL FILTERED OUT IS A DEAD END, so hide it.
        #
        # Only when the card HAS membership. Two situations look identical in
        # the output and are not:
        #
        #   no membership at all -- legitimate. SG Commons Portal ships that
        #     way, and a tenant may add a card before wiring agents to it.
        #     Shown, and the frontend omits the actions block.
        #   membership, none surviving -- the provider is disabled or the
        #     caller lacks access, for a reason the user cannot see. The card
        #     would render a heading with nothing to click, which is the same
        #     mistake as listing an agent the router would refuse.
        if not agents and row.id in configured:
            continue

        card_action = _action_from(row.metadata, {"type": "display_card"})

        # A CARD THAT LAUNCHES AN AGENT ITSELF NEEDS NO NESTED BUTTONS.
        #
        # `Listening at Scale` groups two agents and is display-only, so its
        # buttons are the controls. A card whose OWN action is start_agent is
        # already the control -- rendering a button beside it shows the same
        # name twice and gives two ways to do one thing.
        #
        # The membership rows are NOT dead: they are what the gate above reads
        # to tell "no agents configured" from "every agent filtered out", which
        # is what hides this card when its provider is switched off. Suppress
        # them here, AFTER that decision, never by deleting the rows.
        if _launches_an_agent(card_action):
            agents = []

        capabilities.append({
            "id": row.key,
            "title": row.name,
            "description": row.description or "",
            "icon": row.icon or "",
            "badge": row.badge,
            "status": _STATUS_OUT.get(row.status, "enabled"),
            "order": row.display_order,
            "visible": True,
            "action": card_action,
            "agents": agents,
        })

    return {"version": DOCUMENT_VERSION, "capabilities": capabilities}
