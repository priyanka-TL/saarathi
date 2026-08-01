"""Runtime guards that must fire before the process starts serving.

Both guards exist because this app holds process-local state that a second
copy of the process would silently duplicate: MitraSessionManager is an
in-process LRU pool of live WebSockets, each with its own daemon reader thread.
"""
from __future__ import annotations


def resolve_reloader(mitra_enabled) -> bool:
    """MITRA_ENABLED=1 must never run under the uvicorn auto-reloader: the
    reloader forks/re-execs a child process that re-imports and re-runs all
    module-level setup, which would double-boot Mitra's persistent
    websocket/session state.

    Raises rather than silently overriding -- `assert` is stripped entirely
    under `python -O`, which would silently disable this guard in exactly the
    deployment mode where catching the regression matters most.
    """
    use_reloader = not bool(mitra_enabled)
    if mitra_enabled and use_reloader:
        raise RuntimeError("MITRA_ENABLED=1 requires the uvicorn reloader to be off.")
    return use_reloader


def assert_single_worker(mitra_enabled, workers: int) -> None:
    """MITRA_ENABLED=1 must run in exactly one worker process.

    MitraSessionManager pools live WebSocket channels keyed on conversation_id,
    in process memory. With >1 worker a conversation's channel lives in one
    process but its next turn can be dispatched to any of them, which opens a
    SECOND Mitra channel for the same interview. The Postgres advisory lock in
    OrchestrationService serialises turns; it does not route them to the
    process holding the socket. Disconnecting a channel also stamps Mitra's own
    completion field, so the duplicate is not merely wasteful.

    Scale with `threadpool_size`, not with `--workers`.

    Raises rather than asserting, for the same `python -O` reason as above.
    """
    if mitra_enabled and workers > 1:
        raise RuntimeError(
            f"MITRA_ENABLED=1 requires a single worker process, got workers={workers}. "
            "Scale with THREADPOOL_SIZE instead."
        )
