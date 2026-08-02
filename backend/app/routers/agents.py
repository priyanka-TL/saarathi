"""The public agent catalogue -- what the sidebar renders.

Port of src/api/agent_routes.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.core.container import Container
from app.dependencies.container import get_container
from app.dependencies.db import get_db
from app.dependencies.identity import get_current_user
from app.utils.responses import json_response

router = APIRouter(tags=["agents"])


@router.get("/api/agents", response_model=None)
def get_agents(
    container: Container = Depends(get_container),
    user=Depends(get_current_user),
    db=Depends(get_db),
) -> JSONResponse:
    """The list of available agents.

    Answers a BARE JSON ARRAY, not an object -- pinned by
    tests/characterisation/fixtures/api_agents_config.json.

    The first element is synthetic and deliberately carries ONLY `name` and
    `description`, with no `key`. The frontend uses the absence of `key` to
    recognise it as the "let the server route me" pseudo-agent and skip it when
    rendering the sidebar list. A response_model would either inject
    `key: null` here or reject the entry, which is why this route (like every
    other) declares response_model=None.
    """
    agents_list = [
        {"name": "Saarthi", "description": "Automatically routes your request to the best agent"}
    ]

    registered = sorted(container.agent_registry.routable(), key=lambda r: r.spec.sort_order)

    # Resolve each agent for THIS caller's scope, then apply AccessSpec.
    #
    # THE ACCESS FILTER IS A FIX, not a refactor. This route previously listed
    # every routable agent regardless of `access`, while RouterService._visible
    # has always filtered by the same AccessSpec -- so the sidebar advertised
    # agents the router would refuse for that caller, and selecting one
    # silently fell through to the default agent. The two now agree, and they
    # agree by calling the SAME AccessSpec.matches(): there must never be a
    # second access check here to drift out of step with routing.
    tenant_id = getattr(user, "tenant_code", "") or "default"
    organization_id = getattr(user, "active_org_id", None) or "default"

    visible = []
    for reg in registered:
        scoped = container.agent_registry.resolve_for_scope(db, reg, tenant_id, organization_id)
        if scoped.spec.access.matches(user):
            visible.append(scoped)

    for reg in visible:
        spec = reg.spec
        agents_list.append({
            "name": reg.name,
            "description": reg.description,
            "key": reg.key,
            "agent_type": reg.agent_type,
            "capabilities": spec.capabilities,
            "status": spec.status,
            "sort_order": spec.sort_order,
            "supports_options": spec.features.emit_options,
            "pin_session": spec.routing.pin_session,
        })

    return json_response(agents_list)
