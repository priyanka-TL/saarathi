"""FastAPI composition root. Port of src/app_factory.py + app.py + wsgi.py.

EVERY ENDPOINT AND EVERY DEPENDENCY IN THIS APP IS A PLAIN `def`
----------------------------------------------------------------
Starlette dispatches a non-coroutine endpoint through `run_in_threadpool` ->
`anyio.to_thread.run_sync`, giving it a real worker thread with a copied
contextvars context. That is not a stylistic choice; the code underneath
requires it:

  * OrchestrationService takes a SESSION-scoped `pg_try_advisory_lock`, holds
    it across a commit and across a handler call of up to 60s, then releases it
    with an explicit `pg_advisory_unlock` -- all of which must happen on one
    connection owned by one thread.
  * MitraChannel uses `threading.Lock` and a blocking
    `queue.Queue(maxsize=512)` fed by a daemon thread doing blocking
    `ws.recv()`.
  * MitraRestClient is `requests`, with a 30s read timeout.
  * SQLAlchemy is the sync engine on psycopg3.
  * `_NormalizedChatLiteLLM._agenerate` RAISES NotImplementedError, so an
    `await llm.ainvoke(...)` is a hard crash rather than a slowdown.

`tests/guards/test_no_async_endpoints.py` enforces this. The only `async def`
callables in the request path are the two body-reading dependencies in
app/dependencies/body.py, which do no blocking work.

The consequence is a hard coupling between the anyio threadpool and the DB
connection pool -- one request == one thread == one connection for the whole
turn. See app/core/concurrency.py.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.core.concurrency import size_threadpool
from app.core.logger import get_logger
from app.core.runtime import resolve_reloader  # noqa: F401  (re-exported for __main__)
from app.exceptions.handlers import register_exception_handlers
from app.middleware.request_id import RequestIDMiddleware

logger = get_logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup does exactly one thing: size the worker threadpool.

    NOTHING ELSE BELONGS HERE. In particular:

    * `build_container` + `sync_and_reload` run in `create_app()` instead, so
      that a misconfigured agent YAML still aborts at IMPORT time exactly as it
      did under Flask, and so `TestClient(app)` gets a fully-booted app without
      every test needing a `with` block.
    * There is no shutdown hook closing the Mitra channel pool.
      `MitraSessionManager` already registers `atexit.register(self.close_all)`
      in its constructor; adding a second call here would double-close.
    """
    size_threadpool(app.state.container.settings)
    yield


def create_app() -> FastAPI:
    # Importing settings triggers validation; a missing OPENROUTER_API_KEY
    # exits the process here, before anything else is built.
    from app.core.settings import settings

    # Mounted in front of every route, including /healthz and the docs. Empty
    # by default, so the paths are byte-identical to the unprefixed app.
    prefix = settings.api_prefix

    app = FastAPI(
        title="Saarthi",
        description="Agentic AI orchestrator. Routes a chat turn to a configured agent.",
        version="1.0.0",
        default_response_class=JSONResponse,
        lifespan=lifespan,
        # The docs must live under the prefix too -- otherwise a proxy routing
        # only /saarathi-service/* to this app cannot reach them at all.
        docs_url=f"{prefix}/docs",
        redoc_url=f"{prefix}/redoc",
        openapi_url=f"{prefix}/openapi.json",
    )

    from app.core.container import build_container
    container = build_container(settings)
    app.state.container = container
    app.state.authenticator = container.authenticator

    from app.core.bootstrap import sync_and_reload
    # Deliberately NOT wrapped in try/except. A misconfigured agent YAML or an
    # unknown tool reference must abort startup, not fail silently later.
    sync_and_reload(container)

    # Starlette's add_middleware inserts at index 0, so the LAST one added is
    # the OUTERMOST. CORS must be outermost for preflight to work and for
    # error responses to carry CORS headers.
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.frontend_origins.split(",") if o.strip()],
        # There is no cookie and no server-side session anywhere in this app;
        # identity is resolved once at startup by the Authenticator. Enabling
        # credentials would only forbid wildcard origins for no gain.
        allow_credentials=False,
        # PUT is here for exactly one route: /api/voice/upload-local/{key},
        # which the browser PUTs a recording to under CLOUD_STORAGE_PROVIDER=local.
        # Against a real bucket that upload goes to the STORAGE origin instead,
        # so it is the bucket's CORS rules that must allow PUT, not these.
        allow_methods=["GET", "POST", "PATCH", "PUT", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-Request-ID"],
        # Without this the browser cannot read the header back off a response.
        expose_headers=["X-Request-ID"],
    )

    # The ONE place the prefix is applied to the API. Every route decorator
    # keeps its absolute /api/... path; see app/api/router.py.
    app.include_router(api_router, prefix=prefix)
    register_exception_handlers(app)

    @app.get(f"{prefix}/healthz", response_model=None, tags=["ops"])
    def healthz() -> JSONResponse:
        """New in the FastAPI port. Exists so the verification loop and any
        container orchestrator have something cheap to poll. Note the path is
        NOT under /api/, so it skips the registry-reload check in get_db --
        but it DOES sit under api_prefix, so a proxy that routes only the
        prefix to this app can still reach it."""
        return JSONResponse({"status": "ok"})

    logger.info(
        "Saarthi API ready (app_env=%s, mitra_enabled=%s, api_prefix=%r)",
        settings.app_env,
        settings.mitra_enabled,
        prefix,
    )
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    from app.core.runtime import assert_single_worker
    from app.core.settings import settings

    # Host, port and workers are configuration, not literals -- `make run`
    # goes through this block precisely so there is only one source for them.
    assert_single_worker(settings.mitra_enabled, settings.workers)
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        # Raises if MITRA_ENABLED=1 -- the reloader forks a child that
        # re-imports everything and would double-boot the WebSocket pool.
        reload=resolve_reloader(settings.mitra_enabled),
        workers=settings.workers,
    )
