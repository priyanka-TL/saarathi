"""The per-turn latency breakdown, end to end through a real request.

`conversation_messages.latency_ms` records what a turn cost but never where it
went. Answering "which stage was that" meant reading code and guessing, and the
guess was wrong in the one case that mattered most -- a WebSocket turn that had
finished quickly and then sat waiting out an eight-second backstop looked
identical to a provider that had genuinely thought for eight seconds.

These tests drive the WHOLE stack (RequestIDMiddleware -> orchestration ->
handler), because the accumulator lives in a ContextVar set in the event loop
and read in a worker thread. A unit test of `TurnTimings` would pass with that
hop broken, which is precisely the failure that would empty every log line.
"""
from __future__ import annotations

import json
import logging

import pytest

from app.core import timing


TURN_COMPLETED = "turn completed"
REQUEST_COMPLETED = "request completed"


@pytest.fixture()
def turn_events(caplog):
    """Every "turn completed" record emitted during the test."""
    caplog.set_level(logging.INFO)

    def _records():
        return [r for r in caplog.records if r.getMessage() == TURN_COMPLETED]

    return _records


@pytest.fixture()
def request_events(caplog):
    """Every "request completed" record -- the router's half of the breakdown."""
    caplog.set_level(logging.INFO)

    def _records():
        return [r for r in caplog.records if r.getMessage() == REQUEST_COMPLETED]

    return _records


def _chat(client, message="hello"):
    return client.post("/api/chat", json={"message": message, "agent_name": "Saarthi"})


def test_a_completed_turn_logs_its_stage_breakdown(client, script, turn_events):
    script.queue(json.dumps({"agent_key": "general_support", "confidence": 0.9}))
    script.queue("agent reply")

    assert _chat(client).status_code == 200

    events = turn_events()
    assert len(events) == 1, "exactly one turn-completed event per turn"
    stages = events[0].stages

    # The orchestrator's own steps, and the agent's work, as separate numbers.
    for expected in ("conv_lookup", "record_user_message", "router", "handler", "persist"):
        assert expected in stages, f"{expected!r} missing from {sorted(stages)}"

    # Durations, not counts. Zero is legitimate -- a local Postgres step can
    # genuinely round to under a millisecond -- but negative never is, and a
    # non-integer means something recorded a float duration by mistake.
    assert all(isinstance(v, int) and v >= 0 for v in stages.values()), stages


def test_the_llm_call_is_counted_and_timed_apart_from_routing(client, script, turn_events):
    """Routing overhead must not be billed to the agent, or vice versa.

    A pinned turn skips Gate 4 entirely, so `router_llm` being its own stage is
    what makes "this turn paid for a classifier" answerable at a glance.
    """
    script.queue(json.dumps({"agent_key": "general_support", "confidence": 0.9}))
    script.queue("agent reply")

    assert _chat(client, "route me").status_code == 200

    event = turn_events()[0]
    assert event.counts["router_llm_calls"] == 1, "Gate 4 ran, so it must be counted"
    assert event.counts["llm_calls"] == 1, "the serving agent's own call"
    assert "router_llm" in event.stages
    assert "llm" in event.stages


def test_every_field_survives_json_serialisation(client, script, turn_events):
    """The formatter promotes each `extra` key to a top-level JSON field, so a
    value it cannot encode takes the whole log line down rather than degrading.
    """
    script.queue(json.dumps({"agent_key": "general_support", "confidence": 0.9}))
    script.queue("agent reply")

    assert _chat(client).status_code == 200

    event = turn_events()[0]
    json.dumps({"stages": event.stages, "counts": event.counts, "ws": event.ws})


def test_the_accumulator_does_not_leak_between_requests(client, script, turn_events):
    """Two turns, two independent records.

    The accumulator is a MUTABLE object on a ContextVar. If the middleware ever
    stops installing a fresh one per request, stages would accumulate across
    turns -- and the second turn's numbers would be indistinguishable from a
    genuine slowdown, which is the one way instrumentation can actively mislead.
    """
    for _ in range(2):
        script.queue(json.dumps({"agent_key": "general_support", "confidence": 0.9}))
        script.queue("agent reply")
        assert _chat(client).status_code == 200

    first, second = turn_events()
    assert first.counts["llm_calls"] == 1
    assert second.counts["llm_calls"] == 1, (
        f"second turn saw {second.counts['llm_calls']} LLM calls -- the first "
        "turn's accumulator was reused"
    )


# ---------------------------------------------------------------------------
# The router's half: the work that happens OUTSIDE `handle_turn`
# ---------------------------------------------------------------------------


def test_the_router_logs_the_work_that_happens_outside_the_turn(
    client, script, turn_events, request_events,
):
    """TWO EVENTS PER REQUEST, joined by `request_id`.

    `"turn completed"` is emitted inside `handle_turn`, which returns before the
    response is built -- so the response stage cannot structurally appear on it.
    Without this second line, `flow_payload`'s three queries (one of them a
    `distinct_agent_sequence` scan) ran on every turn and were invisible.
    """
    script.queue(json.dumps({"agent_key": "general_support", "confidence": 0.9}))
    script.queue("agent reply")

    assert _chat(client).status_code == 200

    turn, request = turn_events()[0], request_events()[0]
    assert len(request_events()) == 1, "exactly one request-completed event"

    # The two halves that `"turn completed"` could not see.
    assert "request" in request.stages, sorted(request.stages)
    assert "response" in request.stages, sorted(request.stages)

    # And they carry the turn's stages too, so one line is readable alone.
    assert "handler" in request.stages

    assert request.request_id == turn.request_id, (
        "the two events are joined by request_id and nothing else"
    )


def test_the_request_total_covers_more_than_the_turn_it_wrapped(
    client, script, turn_events, request_events,
):
    """THE NUMBER THIS EXISTS TO PRODUCE. `latency_ms` is measured inside the
    handler; `total_ms` is what the user waited. The difference is everything
    the turn's own measurement structurally could not include, and it should be
    small -- but "should be" is what instrumentation is for."""
    script.queue(json.dumps({"agent_key": "general_support", "confidence": 0.9}))
    script.queue("agent reply")

    assert _chat(client).status_code == 200

    turn, request = turn_events()[0], request_events()[0]

    assert isinstance(request.total_ms, int) and request.total_ms >= 0
    assert request.total_ms >= turn.latency_ms, (
        f"total_ms {request.total_ms} < the turn's own {turn.latency_ms}ms -- "
        "the request cannot have taken less time than the turn inside it"
    )


def test_a_failed_request_logs_no_request_completed_event(client, request_events):
    """Only the success path. An error returns an envelope that already logs,
    and a rejected request has no response stage worth reading."""
    assert client.post("/api/chat", json={}).status_code == 400

    assert request_events() == []


def test_the_request_event_survives_json_serialisation(client, script, request_events):
    script.queue(json.dumps({"agent_key": "general_support", "confidence": 0.9}))
    script.queue("agent reply")

    assert _chat(client).status_code == 200

    event = request_events()[0]
    json.dumps({"stages": event.stages, "total_ms": event.total_ms})


def test_recording_outside_a_request_is_a_no_op_rather_than_an_error():
    """Handlers are exercised directly by unit tests and by background threads,
    neither of which has a request context. Instrumentation must never be the
    reason one of those fails."""
    assert timing.current() is None

    timing.record("nowhere", 5)
    timing.count("nothing")
    timing.detail("unused", True)
    with timing.stage("still_nowhere"):
        pass

    assert timing.current() is None
