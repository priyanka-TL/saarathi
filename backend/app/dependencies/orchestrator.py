"""The per-request OrchestrationService, as a dependency.

`_orchestrator(db, container)` was defined byte-identically in
`app/routers/chat.py` and `app/routers/sessions.py`, each hand-assembling the
same six collaborators. Two copies of a construction list means a seventh
dependency has to be added in both places, and the failure mode of forgetting
one is a service that behaves differently depending on which route reached it.

As a dependency rather than a helper, the routers stop knowing how the service
is built at all -- which is the point of the composition root existing.

PER REQUEST, NOT PER PROCESS. It is built around the request's own session
(`get_db`), which is pinned to one connection for the life of the turn because
the advisory lock is session-scoped. A shared instance would hold a connection
from a different request.
"""
from __future__ import annotations

from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.container import Container
from app.dependencies.container import get_container
from app.dependencies.db import get_db
from app.services.orchestration import OrchestrationService


def get_orchestrator(
    db: Session = Depends(get_db),
    container: Container = Depends(get_container),
) -> OrchestrationService:
    """The turn pipeline, wired to this request's session.

    Plain `def`, like every other dependency here: Starlette runs it in a worker
    thread, which the sync engine and the advisory lock both require.
    """
    return OrchestrationService(
        session=db,
        registry=container.agent_registry,
        handler_factory=container.handler_factory,
        llm_factory=container.llm_factory,
        mitra_sessions=container.mitra_sessions,
        mitra_clients=container.mitra_clients,
        settings=container.settings,
    )
