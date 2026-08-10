"""GET /api/ui/capabilities -- the sidebar document.

Responsible for: delegating to CapabilityService and returning its document.
Used by: the SPA on load, to render the capability buttons.
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
        resolve_for_user(
            db, user,
            mitra_enabled=bool(container.settings.mitra_enabled),
            saathi_enabled=bool(container.settings.saathi_enabled),
        )
    )
