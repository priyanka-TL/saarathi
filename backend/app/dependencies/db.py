"""Per-request database session.

Port of Flask's `before_request` (open) / `teardown_request` (commit, rollback,
close) pair, plus the TTL-gated registry reload that rode along with it.

TWO PROPERTIES THAT MUST NOT DRIFT
----------------------------------
1. **Commit on a returned error.** Flask's teardown committed whenever no
   exception escaped -- including when a handler *returned* a 400/404/409/500
   error tuple, which every mapped failure did. So the routers here must
   `return` a JSONResponse for every mapped error and never `raise`; only a
   genuinely unexpected exception should reach the `except` below and roll
   back. See app/exceptions/envelope.error_response.

2. **One connection for the whole request, pinned explicitly.** See below --
   this is the one place the port had to fix a latent bug rather than copy it.

Note that FastAPI runs the post-`yield` block *before* the response is sent,
where Flask's teardown ran after serialization. That is benign here (routers
serialize from DTOs, and the session factory sets `expire_on_commit=False`) and
strictly safer -- a failing commit becomes a 500 instead of a silently lost
write behind a 200.

CONNECTION PINNING -- THE ONE DELIBERATE FIX IN THIS PORT
--------------------------------------------------------
`OrchestrationService.handle_turn` takes a SESSION-scoped
`pg_try_advisory_lock`, commits mid-turn (to avoid holding a transaction across
a Mitra round trip of up to 60s), runs the handler, and finally releases the
lock with `pg_advisory_unlock`.

A session-scoped advisory lock belongs to the CONNECTION, and a plain
`Session` hands its connection back to the pool on `commit()`, checking one out
again for the next statement. Single-threaded that is invisible: the pool
returns the most recently used connection, so the unlock lands on the same one.
Under real concurrency it does not -- another worker can check that connection
out in the window between the commit and the unlock, so:

  * the unlock runs on some other connection and silently returns false, and
  * the original connection keeps the advisory lock FOREVER, poisoning that
    conversation with permanent 409s for every later turn that happens to be
    handed the same pooled connection.

This is latent in the Flask original too -- identical SQLAlchemy setup and
identical orchestration code -- but its dev server never ran turns
concurrently, so it could not surface. Under uvicorn with a 16-thread pool it
surfaces immediately: `tests/guards/test_turn_concurrency.py` reproduced it on
the first run.

Binding the Session to an explicitly checked-out `Connection` fixes it at the
root. The connection is checked out once, used for every statement in the
request including the lock and the unlock, and returned exactly once in the
`finally`. No business logic changes; `handle_turn` is untouched.
"""
from __future__ import annotations

from typing import Iterator

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.container import Container
from app.database.engine import engine
from app.dependencies.container import get_container


def get_db(request: Request, container: Container = Depends(get_container)) -> Iterator[Session]:
    # One connection, checked out for the whole request. Everything below --
    # the registry reload, the turn, the advisory lock AND its unlock -- runs
    # on this exact connection.
    connection = engine.connect()
    db = Session(
        bind=connection,
        autocommit=False,
        autoflush=False,
        # Mirrors SessionLocal: handle_turn commits mid-flight and keeps using
        # the DTOs it already read.
        expire_on_commit=False,
    )

    # TTL-gated agent registry reload, /api/ paths only -- same predicate as
    # Flask's `request.path.startswith("/api/")`, so the health check and any
    # non-API route still never touch the DB for this.
    #
    # The predicate MUST carry api_prefix. Under API_PREFIX=/saarathi-service
    # the real path is /saarathi-service/api/chat, which does not start with
    # "/api/" -- the registry would silently stop reloading and agent config
    # changes would never take effect until a restart.
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
        # Returns the connection to the pool. Postgres does NOT drop
        # session-scoped advisory locks on transaction end, but it does reset
        # them when the pooled connection is later recycled; the real guarantee
        # is that handle_turn's explicit unlock ran on this same connection.
        connection.close()
