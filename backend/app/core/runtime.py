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


def _stateful_flags(*flags) -> list[str]:
    """The names of the enabled providers that pool sockets in process memory.

    Both guards below apply to ANY such provider, not to Mitra specifically --
    the hazard is process-local WebSocket state, and Saathi has exactly the same
    kind. Passing (name, enabled) pairs keeps the two call sites honest as
    providers are added.
    """
    return [name for name, enabled in flags if enabled]


def resolve_reloader(mitra_enabled, saathi_enabled=0) -> bool:
    """Whether the uvicorn auto-reloader may run.

    Never while a socket-pooling provider is on: the reloader re-execs a child
    that re-runs all module-level setup, double-booting the websocket state.
    """
    stateful = _stateful_flags(("MITRA_ENABLED", mitra_enabled), ("SAATHI_ENABLED", saathi_enabled))
    use_reloader = not stateful
    if stateful and use_reloader:
        raise RuntimeError(
            f"{' and '.join(stateful)} requires the uvicorn reloader to be off."
        )
    return use_reloader


def assert_single_worker(mitra_enabled, workers: int, saathi_enabled=0) -> None:
    """A socket-pooling provider must run in exactly one worker process.

    Channels are pooled per conversation in process memory, but a conversation's
    next turn can be dispatched to any worker -- which opens a SECOND channel
    for the same conversation. The advisory lock serialises turns; it does not
    route them to the process holding the socket. Disconnecting also stamps the
    remote side's completion field, so the duplicate is not merely wasteful.

    Scale with THREADPOOL_SIZE, not --workers.

    `saathi_enabled` is keyword-last so the existing two-positional call sites
    and their tests keep working unchanged.
    """
    stateful = _stateful_flags(("MITRA_ENABLED", mitra_enabled), ("SAATHI_ENABLED", saathi_enabled))
    if stateful and workers > 1:
        raise RuntimeError(
            f"{' and '.join(stateful)} requires a single worker process, got "
            f"workers={workers}. Scale with THREADPOOL_SIZE instead."
        )
