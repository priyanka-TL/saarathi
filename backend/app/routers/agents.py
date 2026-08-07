"""GET /api/agents -- the agents this caller may select.

Responsible for: shaping the agent list; scope resolution and the access filter
live in AgentRegistry.routable_for_user.
Used by: the SPA, to render the agent picker.
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

    # Scope resolution and the access filter both live in the registry -- see
    # AgentRegistry.routable_for_user, which uses the SAME AccessSpec.matches()
    # RouterService does. There must never be a second access check here to
    # drift out of step with routing: an agent listed but not routable renders a
    # sidebar entry that silently falls through to the default agent.
    for reg in container.agent_registry.routable_for_user(db, user):
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
