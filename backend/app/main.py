"""FastAPI composition root.

Responsible for: building the app -- container, middleware, routers, handlers.
Used by: `python -m app.main`, and by every test via TestClient.

EVERY ENDPOINT AND DEPENDENCY HERE IS A PLAIN `def`, so Starlette dispatches it
to a real worker thread. Not a style choice -- the code underneath requires it:
the session-scoped advisory lock, MitraChannel's threading.Lock and blocking
Queue, `requests`, the sync SQLAlchemy engine, and an LLM client whose
`_agenerate` raises NotImplementedError. The only `async def` callables in the
request path are the two body readers in app/dependencies/body.py.

The consequence is a hard coupling: one request == one thread == one DB
connection, for the whole turn. See app/core/concurrency.py.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.core.concurrency import size_threadpool
from app.core.logger import configure_logging, get_logger
from app.core.runtime import resolve_reloader  # noqa: F401  (re-exported for __main__)
from app.exceptions.handlers import register_exception_handlers
from app.middleware.request_id import RequestIDMiddleware

logger = get_logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Sizes the worker threadpool. Nothing else belongs here.

    build_container and sync_and_reload run in create_app() instead, so a bad
    config aborts at import time and TestClient(app) gets a fully-booted app.
    There is no shutdown hook: MitraSessionManager registers its own atexit,
    and a second call would double-close.
    """
    size_threadpool(app.state.container.settings)
    yield


def create_app() -> FastAPI:
    # Importing settings triggers validation; a missing OPENROUTER_API_KEY
    # exits the process here, before anything else is built.
    from app.core.settings import settings

    # Before build_container, so its warnings are JSON-formatted too.
    configure_logging(settings.LOG_LEVEL)

    # In front of every route, including /healthz and the docs.
    prefix = settings.api_prefix

    app = FastAPI(
        title="Saarthi",
        description="Agentic AI orchestrator. Routes a chat turn to a configured agent.",
        version="1.0.0",
        default_response_class=JSONResponse,
        lifespan=lifespan,
        # Under the prefix too, or a proxy routing only the prefix cannot
        # reach them.
        docs_url=f"{prefix}/docs",
        redoc_url=f"{prefix}/redoc",
        openapi_url=f"{prefix}/openapi.json",
    )

    from app.core.container import build_container
    container = build_container(settings)
    app.state.container = container
    app.state.authenticator = container.authenticator

    from app.core.bootstrap import sync_and_reload
    # NOT wrapped in try/except: a bad config must abort startup rather than
    # fail silently on the first request.
    sync_and_reload(container)

    # add_middleware inserts at index 0, so the LAST added is OUTERMOST. CORS
    # must be outermost for preflight and for error responses to carry headers.
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.frontend_origins.split(",") if o.strip()],
        # No cookies and no server-side session anywhere; enabling credentials
        # would only forbid wildcard origins for no gain.
        allow_credentials=False,
        # PUT is for /api/voice/upload-local only (local provider). Against a
        # real bucket the upload goes to the storage origin, whose own CORS
        # rules apply instead.
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
        """Cheap liveness probe. Not under /api/, so it skips the registry
        reload in get_db, but it does sit under api_prefix so a prefix-routing
        proxy can reach it."""
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

    # Host, port and workers come from Settings; `make run` goes through here
    # so there is only one source for them.
    assert_single_worker(settings.mitra_enabled, settings.workers)
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        # Raises under MITRA_ENABLED=1: the reloader's child would double-boot
        # the WebSocket pool.
        reload=resolve_reloader(settings.mitra_enabled),
        workers=settings.workers,
    )
