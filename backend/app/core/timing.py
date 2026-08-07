"""Elapsed-time measurement, in one place.

`int((time.monotonic() - t0) * 1000)` was written out five times across
`llm_handler.py`, `remote_flow_handler.py` and `router_service.py`. The
duplication was harmless in itself; what it cost was consistency -- one of the
five could have reached for `time.time()` and nothing would have flagged it.

MONOTONIC, NEVER `time.time()`. A wall clock can step backwards (NTP, a DST
change on a badly configured host) and produce a negative latency, which then
lands in `conversation_messages.latency_ms` and in a log field where it is
indistinguishable from a real measurement.

Stdlib only, deliberately: this is imported from `app.agents`, which
`.importlinter` forbids from reaching a web framework.
"""
from __future__ import annotations

import time
from typing import Optional


def start() -> float:
    """A monotonic reference point to measure from. Not a wall-clock time."""
    return time.monotonic()


def elapsed_ms(t0: float) -> int:
    """Whole milliseconds since `t0`, which must have come from :func:`start`."""
    return int((time.monotonic() - t0) * 1000)


class Stopwatch:
    """Times a block and exposes the result as `.ms`.

    For the several call sites that need the duration after the block rather
    than at a single point inside it::

        with Stopwatch() as sw:
            result = client.invoke(...)
        logger.info("done", extra={"latency_ms": sw.ms})

    `.ms` reads correctly inside the block too (elapsed so far), so a partial
    duration can still be logged from an exception handler.
    """

    __slots__ = ("_t0", "_stopped_ms")

    def __init__(self) -> None:
        self._t0 = time.monotonic()
        self._stopped_ms: Optional[int] = None

    def __enter__(self) -> "Stopwatch":
        self._t0 = time.monotonic()
        return self

    def __exit__(self, *exc_info) -> None:
        # Recorded even when the block raised: how long something took before
        # it failed is usually the more interesting number.
        self._stopped_ms = elapsed_ms(self._t0)

    @property
    def ms(self) -> int:
        return self._stopped_ms if self._stopped_ms is not None else elapsed_ms(self._t0)
