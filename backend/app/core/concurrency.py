"""Threadpool sizing.

Every endpoint in this app is a plain `def`, so Starlette dispatches it through
`run_in_threadpool` -> `anyio.to_thread.run_sync`. That is deliberate: the turn
pipeline holds a session-scoped Postgres advisory lock across a commit and
across a handler call of up to 60s, uses `threading.Lock`/`queue.Queue` inside
the Mitra WebSocket channel, calls `requests` synchronously, and runs an LLM
client whose `_agenerate` raises NotImplementedError. All of that needs a real
thread, not a coroutine.

The consequence is a hard coupling between two pool sizes that anyio and
SQLAlchemy know nothing about:

    one request == one worker thread == one DB connection, held for the whole turn

anyio's default limiter is 40 tokens; the engine grants
`db_pool_size (16) + db_max_overflow (8) = 24` connections. Left alone,
requests 25-40 block in `QueuePool.connect()` for `pool_timeout` and then
raise -- surfacing to the client as a 500 INTERNAL rather than backpressure.
Flask never hit this because its dev server serialised requests.
"""
from __future__ import annotations

import anyio.to_thread

from app.core.logger import get_logger

logger = get_logger("concurrency")


def resolve_threadpool_size(settings) -> int:
    """The size the threadpool SHOULD have. Pure -- no event loop required.

    `db_max_overflow` is deliberately not counted in: the registry-reload path
    and `pool_pre_ping` churn both need a connection that no request is
    holding, so overflow stays as genuine headroom rather than being handed out
    to request threads.
    """
    requested = settings.threadpool_size or settings.db_pool_size
    size = min(requested, settings.db_pool_size)
    if size != requested:
        logger.warning(
            "threadpool_size %s exceeds db_pool_size %s; clamping to %s",
            requested, settings.db_pool_size, size,
        )
    return size


def size_threadpool(settings) -> int:
    """Apply resolve_threadpool_size() to the running loop's limiter.

    MUST be called from inside an event loop: anyio keeps the default thread
    limiter in a RunVar scoped to the current run. The call site is the
    lifespan in app/main.py, i.e. uvicorn's loop.
    """
    size = resolve_threadpool_size(settings)
    anyio.to_thread.current_default_thread_limiter().total_tokens = size
    logger.info("anyio threadpool sized to %s (db_pool_size=%s)", size, settings.db_pool_size)
    return size
