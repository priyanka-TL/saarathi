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

It also carries the PER-TURN BREAKDOWN (`TurnTimings` and the `stage()` helper
below). One turn's `latency_ms` was a single opaque number covering an
orchestrator round of SQL, a WebSocket round trip to another platform and two
HTTP polls; "which of those was it" was unanswerable from a log line, and the
only honest answer to "where does the time go" was a database query and a guess.
`stage()` splits it without threading a timer object through nine call sites.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterator, Optional


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


class TurnTimings:
    """One turn's elapsed time, split by stage, plus non-duration facts.

    STAGES ACCUMULATE RATHER THAN OVERWRITE, and that is the point of `add()`
    rather than a plain dict assignment. Several stages fire more than once in a
    turn -- `is_complete` alone makes two HTTP calls, and a turn whose channel
    died is re-sent on a fresh one -- so the useful number is the total time
    spent in that stage, not the last visit's.

    `details` is for facts that are not durations: how a WebSocket turn ended,
    how many fragments it arrived in. They belong on the same record because
    they are what makes a duration interpretable -- `remote_turn: 9685` means
    something quite different alongside `end_reason: idle_gap` than alongside
    `end_reason: finish_reason`.

    `counts` is kept apart from `stages` rather than folded in as another
    integer, because "1200" meaning milliseconds and "2" meaning calls in the
    same map is exactly the sort of thing that gets read wrong once and then
    trusted. How MANY outbound calls a turn made is its own question -- it is
    what distinguishes a slow dependency from one being called twice.

    NOT THREAD-SAFE, and does not need to be. One instance belongs to one
    request, which is one worker thread for its whole life
    (app/core/concurrency.py). The provider's WebSocket reader thread never
    records: it feeds a queue, and the stage is timed by the request thread
    blocking on that queue.
    """

    __slots__ = ("_stages", "_counts", "_details")

    def __init__(self) -> None:
        self._stages: Dict[str, int] = {}
        self._counts: Dict[str, int] = {}
        self._details: Dict[str, Any] = {}

    def add(self, stage: str, ms: int) -> None:
        self._stages[stage] = self._stages.get(stage, 0) + ms

    def count(self, name: str, n: int = 1) -> None:
        self._counts[name] = self._counts.get(name, 0) + n

    def detail(self, key: str, value: Any) -> None:
        self._details[key] = value

    @property
    def stages(self) -> Dict[str, int]:
        return dict(self._stages)

    @property
    def counts(self) -> Dict[str, int]:
        return dict(self._counts)

    @property
    def details(self) -> Dict[str, Any]:
        return dict(self._details)


#: The turn being timed, if any. A ContextVar and NOT a thread-local for the
#: same reason `request_id_var` is one: RequestIDMiddleware sets it in the event
#: loop and every endpoint runs in a worker thread via `anyio.to_thread.run_sync`,
#: which copies the context across but would not carry a thread-local.
#:
#: The VALUE is mutated in place rather than re-set, so a recording made in the
#: worker thread is visible to whoever holds the same object.
turn_timings_var: ContextVar[Optional[TurnTimings]] = ContextVar(
    "turn_timings", default=None,
)


def current() -> Optional[TurnTimings]:
    """The turn being timed, or None outside a request."""
    return turn_timings_var.get()


def record(stage: str, ms: int) -> None:
    """Add `ms` to `stage`. A NO-OP when nothing is being timed.

    Silent rather than raising, deliberately: these calls sit in the middle of
    the turn pipeline, and a unit test that exercises one handler directly -- or
    a background thread that has no request context -- must not fail on the
    absence of a measurement nobody asked for.
    """
    timings = turn_timings_var.get()
    if timings is not None:
        timings.add(stage, ms)


def count(name: str, n: int = 1) -> None:
    """Increment a per-turn counter. A NO-OP outside a request."""
    timings = turn_timings_var.get()
    if timings is not None:
        timings.count(name, n)


def detail(key: str, value: Any) -> None:
    """Attach a non-duration fact to the turn. A NO-OP outside a request."""
    timings = turn_timings_var.get()
    if timings is not None:
        timings.detail(key, value)


@contextmanager
def stage(name: str) -> Iterator[Stopwatch]:
    """Time a block and file the result under `name`.

        with timing.stage("remote_turn"):
            bot = channel.send_and_await_turn(...)

    RECORDED EVEN WHEN THE BLOCK RAISES, matching Stopwatch's own rule: how long
    something ran before it failed is usually the more interesting number, and a
    turn that timed out is exactly the case this instrumentation exists to
    explain.
    """
    sw = Stopwatch()
    try:
        yield sw
    finally:
        record(name, sw.ms)
