"""Runtime guards that must fire before the process starts serving.

Responsible for: refusing configurations that would duplicate process-local state.
Used by: app/main.py's __main__ block, at startup.

Both guards exist for the same reason: MitraSessionManager is an in-process pool
of live WebSockets, each with its own reader thread. A second copy of the
process silently doubles it.

Both raise rather than assert -- `assert` is stripped under `python -O`, which
is exactly the deployment mode where the guard matters most.
"""
from __future__ import annotations


def resolve_reloader(mitra_enabled) -> bool:
    """Whether the uvicorn auto-reloader may run.

    Never under MITRA_ENABLED=1: the reloader re-execs a child that re-runs all
    module-level setup, double-booting Mitra's websocket state.
    """
    use_reloader = not bool(mitra_enabled)
    if mitra_enabled and use_reloader:
        raise RuntimeError("MITRA_ENABLED=1 requires the uvicorn reloader to be off.")
    return use_reloader


def assert_single_worker(mitra_enabled, workers: int) -> None:
    """MITRA_ENABLED=1 must run in exactly one worker process.

    Channels are pooled per conversation in process memory, but a conversation's
    next turn can be dispatched to any worker -- which opens a SECOND Mitra
    channel for the same interview. The advisory lock serialises turns; it does
    not route them to the process holding the socket. Disconnecting also stamps
    Mitra's completion field, so the duplicate is not merely wasteful.

    Scale with THREADPOOL_SIZE, not --workers.
    """
    if mitra_enabled and workers > 1:
        raise RuntimeError(
            f"MITRA_ENABLED=1 requires a single worker process, got workers={workers}. "
            "Scale with THREADPOOL_SIZE instead."
        )
