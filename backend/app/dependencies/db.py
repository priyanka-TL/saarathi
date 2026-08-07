"""Per-request database session.

Responsible for: opening one session, committing or rolling it back, closing it,
and running the TTL-gated agent-registry reload.
Used by: every route, via Depends(get_db).

TWO PROPERTIES THAT MUST NOT DRIFT:

1. COMMIT ON A RETURNED ERROR. Mapped failures must `return` a JSONResponse,
   never `raise` -- a raise reaches the `except` below and rolls back, losing
   the write the error response was supposed to accompany.

2. ONE CONNECTION, PINNED. handle_turn takes a SESSION-scoped advisory lock,
   commits mid-turn, then unlocks. A session-scoped lock belongs to the
   CONNECTION, and a plain Session returns its connection to the pool on
   commit() -- so under concurrency the unlock can land on a different
   connection, silently returning false and leaving the original locked
   FOREVER, poisoning that conversation with permanent 409s. Binding to an
   explicitly checked-out Connection fixes it at the root.
   See tests/guards/test_turn_concurrency.py.
"""
from __future__ import annotations

from typing import Iterator

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.container import Container
from app.dependencies.container import get_container


def get_db(request: Request, container: Container = Depends(get_container)) -> Iterator[Session]:
    # One connection for the whole request -- the registry reload, the turn,
    # the advisory lock and its unlock all run on this exact one. Taken from the
    # container rather than the module-level global, so Container.engine is not
    # bypassed on every request.
    connection = container.engine.connect()
    db = Session(
        bind=connection,
        autocommit=False,
        autoflush=False,
        # Mirrors SessionLocal: handle_turn commits mid-flight and keeps using
        # the DTOs it already read.
        expire_on_commit=False,
    )

    # TTL-gated registry reload, /api/ paths only, so the health check never
    # touches the DB. The predicate MUST carry api_prefix: under
    # API_PREFIX=/saarathi-service the path does not start with "/api/", and the
    # registry would silently stop reloading until a restart.
    if request.url.path.startswith(f"{container.settings.api_prefix}/api/"):
        container.agent_registry.maybe_reload(db)

    try:
        yield db
    except Exception:
        db.rollback()
        raise
    else:
        db.commit()
    finally:
        db.close()
        # Postgres does not drop session-scoped advisory locks on transaction
        # end -- the guarantee is that handle_turn's explicit unlock ran on this
        # same connection.
        connection.close()
