"""Presentation config for the frontend shell.

New in the FastAPI port -- the Flask app had no equivalent, because the sidebar
cards were literal markup in templates/index.html. They then briefly lived in
app/config/ui/capabilities.yaml, which a tenant could not vary; migration 0006
moved them into `capabilities` + `capability_agents`.

WHAT THIS IS NOT: an agent registry. `GET /api/agents` answers the routable
agent catalogue; this answers what the ADVANCED panel draws and what clicking
it does. The two are separate on purpose -- a capability card can group several
agents, or none at all (SG Commons Portal), neither of which the agent
catalogue can express.

THE SINGLE SOURCE. The frontend keeps NO bundled copy of this document, so an
empty answer here means the ADVANCED panel renders no cards at all and the
Mitra interviews have no entry point in the UI. That is deliberate -- an empty
panel is honest about the backend being unavailable, where a bundled fallback
would draw buttons whose POST /api/reset is going to fail anyway.

PER TENANT. The response varies by the caller's `tenant_code` and active
organization, both of which arrive as JWT claims. Two callers hitting this
route in the same process get two different documents; see
app/services/capability_service.py for the most-specific-wins scope rule.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.container import Container
from app.dependencies.container import get_container
from app.dependencies.db import get_db
from app.dependencies.identity import get_current_user
from app.domain.core import UserContext
from app.services.capability_service import resolve_for_user
from app.utils.responses import json_response

router = APIRouter(tags=["ui"])


@router.get("/api/ui/capabilities", response_model=None)
def get_ui_capabilities(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
    container: Container = Depends(get_container),
) -> JSONResponse:
    """The capability document for this caller's scope.

    A plain `def`, like every other endpoint; `tests/guards/
    test_no_async_endpoints.py` enforces that, and the work here is blocking
    DB I/O that belongs in a worker thread.

    `response_model=None` for the house reason: the document is passed through
    as built, and a response model would reshape config whose presentation
    fields this service deliberately does not interpret.

    An empty catalogue answers 200 with an empty list, NOT 404. "This tenant
    has no capabilities" is a real, correct answer -- 404 would say the route
    does not exist, which is a different claim and would make a genuinely
    misconfigured deployment indistinguishable from a deliberately empty one.
    """
    return json_response(
        resolve_for_user(db, user, mitra_enabled=bool(container.settings.mitra_enabled))
    )
