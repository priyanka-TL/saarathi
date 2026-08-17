"""Runtime guards that must fire before the process starts serving.

Responsible for: refusing configurations that would duplicate process-local state.
Used by: app/main.py's __main__ block, at startup.

Both guards exist for the same reason: a provider with a stateful transport
holds an in-process pool of live WebSockets, each with its own reader thread. A
second copy of the process silently doubles it.

THEY TAKE PROVIDER NAMES, NOT FLAGS. These used to be spelled in terms of two
named `*_ENABLED` settings, so every new platform meant editing both guards and
remembering which of them applied. The hazard was never a particular platform --
it is process-local socket state, which a provider declares for itself with
`stateful_transport`. A deployment running only stateless providers is
legitimately free of the constraint, which the old spelling could not express.

Both raise rather than assert -- `assert` is stripped under `python -O`, which is
exactly the deployment mode where the guard matters most.
"""
from __future__ import annotations

from typing import Sequence


def _describe(stateful: Sequence[str]) -> str:
    return " and ".join(sorted(stateful))


def resolve_reloader(stateful_providers: Sequence[str]) -> bool:
    """Whether the uvicorn auto-reloader may run.

    Never while a socket-pooling provider is enabled: the reloader re-execs a
    child that re-runs all module-level setup, double-booting the socket state.
    """
    return not list(stateful_providers)


def assert_single_worker(stateful_providers: Sequence[str], workers: int) -> None:
    """A socket-pooling provider must run in exactly one worker process.

    Channels are pooled per conversation in process memory, but a conversation's
    next turn can be dispatched to any worker -- which opens a SECOND channel for
    the same conversation. The advisory lock serialises turns; it does not route
    them to the process holding the socket. Disconnecting also stamps the remote
    side's completion field, so the duplicate is not merely wasteful.

    Scale with THREADPOOL_SIZE, not --workers.
    """
    stateful = list(stateful_providers)
    if stateful and workers > 1:
        raise RuntimeError(
            f"provider(s) {_describe(stateful)} pool sockets in process memory and "
            f"require a single worker process, got workers={workers}. "
            "Scale with THREADPOOL_SIZE instead."
        )
