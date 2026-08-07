"""Threadpool sizing.

Responsible for: keeping the anyio worker pool no larger than the DB pool.
Used by: the lifespan hook in app/main.py, once at startup.

Every endpoint is a plain `def`, so Starlette dispatches it to a worker thread.
That gives a hard coupling anyio and SQLAlchemy know nothing about:

    one request == one worker thread == one DB connection, for the whole turn

anyio's default is 40 threads; the engine grants db_pool_size + max_overflow
(24). Left alone, requests 25-40 block in QueuePool.connect() and then raise,
surfacing as a 500 rather than as backpressure.
"""
from __future__ import annotations

import anyio.to_thread

from app.core.logger import get_logger

logger = get_logger("concurrency")


def resolve_threadpool_size(settings) -> int:
    """The size the threadpool should have. Pure -- no event loop required.

    `db_max_overflow` is deliberately excluded: the registry reload and
    pool_pre_ping churn need a connection no request is holding, so overflow
    stays as headroom rather than being handed to request threads.
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

    MUST run inside an event loop: anyio keeps the limiter in a RunVar scoped to
    the current run.
    """
    size = resolve_threadpool_size(settings)
    anyio.to_thread.current_default_thread_limiter().total_tokens = size
    logger.info("anyio threadpool sized to %s (db_pool_size=%s)", size, settings.db_pool_size)
    return size
