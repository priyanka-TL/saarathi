"""Unit tests for RemoteFlowAgentHandler against faked REST client and pool.

No DB, no network: only plain dataclasses and hand-rolled fakes. Mirrors
tests/unit/test_llm_handler.py's style (real TurnContext/UserContext/
AgentSessionView, only the Mitra-facing dependencies faked).
"""
from __future__ import annotations

import dataclasses
import os
import uuid
from typing import List, Optional

import pytest

from app.agents.factory import HandlerDeps
from app.agents.protocol import AgentSessionView, SessionState, TurnContext, HistoryTurn
from app.agents.remote_flow_handler import RemoteFlowAgentHandler
from app.domain.core import UserContext
from app.domain.agent_spec import (
    MitraHandshakeSpec,
    MitraTurnSpec,
    RemoteFlowAgentSpec,
    RemoteSpec,
    RoutingSpec,
)
from app.integrations.mitra.exceptions import MitraChannelClosed, MitraTurnTimeout
from app.integrations.mitra.frame_parser import ParsedOption
from app.integrations.mitra.ws_channel import BotTurn


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRestClient:
    def __init__(self):
        self.upsert_profile_calls = []
        self.generate_session_calls = 0
        self.is_session_completed_calls = []
        self._profile_id = "profile-1"
        self._session_id = "remote-sess-1"
        self._completed = False

    def upsert_profile(self, email, latest_flow_used, company):
        self.upsert_profile_calls.append((email, latest_flow_used, company))
        return self._profile_id

    def generate_session(self):
        self.generate_session_calls += 1
        return self._session_id

    def is_session_completed(self, session_id):
        self.is_session_completed_calls.append(session_id)
        return self._completed

    def finalize(self, *args, **kwargs):
        raise AssertionError("RemoteFlowAgentHandler must never call finalize()")

    def get_report(self, *args, **kwargs):
        raise AssertionError("RemoteFlowAgentHandler must never call get_report()")


class _FakeChannel:
    def __init__(self, responses):
        self._responses = list(responses)
        self.sent: List[tuple] = []

    def send_and_await_turn(self, text, timeout_s, idle_gap_s):
        self.sent.append((text, timeout_s, idle_gap_s))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeSessionManager:
    def __init__(self, channels):
        """`channels` is a list of _FakeChannel -- consumed by successive
        acquire()/reacquire() calls in order."""
        self._channels = list(channels)
        self.acquire_calls = []
        self.reacquire_calls = []

    def acquire(self, spec, sess):
        self.acquire_calls.append((spec, sess))
        return self._channels.pop(0)

    def reacquire(self, spec, sess):
        self.reacquire_calls.append((spec, sess))
        return self._channels.pop(0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _remote_spec(**overrides) -> RemoteFlowAgentSpec:
    remote = RemoteSpec(
        provider="mitra",
        flow_name="guest-mi-story",
        bot_route_env="TEST_BOT_ROUTE",
        company_env="TEST_COMPANY",
        handshake=MitraHandshakeSpec(settle_ms=10),
        turn=MitraTurnSpec(first_turn_timeout_ms=60000, turn_timeout_ms=45000, idle_gap_ms=8000),
        **overrides,
    )
    return RemoteFlowAgentSpec(
        key="record_stories",
        name="Record Stories",
        description="test remote flow agent",
        agent_type="remote_flow",
        routing=RoutingSpec(pin_session=True, exit_keywords=["/exit"]),
        remote=remote,
    )


def _session(remote_session_id: Optional[str] = None, step: int = 0) -> AgentSessionView:
    return AgentSessionView(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        state=SessionState.pending if remote_session_id is None else SessionState.awaiting_user,
        remote_provider=None,
        remote_session_id=remote_session_id,
        remote_profile_id=None,
        remote_flow=None,
        remote_bot_route=None,
        language="en",
        step=step,
        turn_count=0,
        result_ref=None,
        report_url=None,
        error=None,
        error_code=None,
        state_data={},
    )


def _ctx(text: str, session: AgentSessionView, history=None) -> TurnContext:
    return TurnContext(
        request_id="req-1",
        conversation_id=session.conversation_id,
        user=UserContext(user_id="u1", email="user@example.com", display_name="U", tenant_code="t"),
        text=text,
        option_id=None,
        history=history or [],
        session=session,
        locale="en",
    )


def _deps(rest, sessions) -> HandlerDeps:
    return HandlerDeps(llm_factory=None, tool_registry=None, mitra_rest=rest, mitra_sessions=sessions, settings=None)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("TEST_BOT_ROUTE", "resolved-bot-route")
    monkeypatch.setenv("TEST_COMPANY", "resolved-company")


# ---------------------------------------------------------------------------
# Construction guards
# ---------------------------------------------------------------------------


def test_constructor_raises_if_mitra_rest_is_none():
    with pytest.raises(RuntimeError):
        RemoteFlowAgentHandler(_remote_spec(), _deps(rest=None, sessions=_FakeSessionManager([])))


def test_constructor_raises_if_mitra_sessions_is_none():
    with pytest.raises(RuntimeError):
        RemoteFlowAgentHandler(_remote_spec(), _deps(rest=_FakeRestClient(), sessions=None))


def test_handle_raises_if_ctx_session_is_none():
    rest, sessions = _FakeRestClient(), _FakeSessionManager([])
    handler = RemoteFlowAgentHandler(_remote_spec(), _deps(rest, sessions))
    ctx = _ctx("hi", session=_session())
    ctx = dataclasses.replace(ctx, session=None)
    with pytest.raises(RuntimeError):
        handler.handle(ctx)


# ---------------------------------------------------------------------------
# First turn
# ---------------------------------------------------------------------------


def test_first_turn_creates_profile_and_session():
    rest = _FakeRestClient()
    channel = _FakeChannel([BotTurn(text="Welcome!", options=[], step=1)])
    sessions = _FakeSessionManager([channel])
    handler = RemoteFlowAgentHandler(_remote_spec(), _deps(rest, sessions))

    ctx = _ctx("I want to record a story", session=_session(remote_session_id=None))
    turn = handler.handle(ctx)

    assert rest.upsert_profile_calls == [("user@example.com", "guest-mi-story", "resolved-company")]
    assert rest.generate_session_calls == 1
    assert turn.session_delta.remote_session_id == "remote-sess-1"
    assert turn.session_delta.remote_profile_id == "profile-1"
    assert turn.text == "Welcome!"


def test_first_turn_uses_first_turn_timeout():
    rest = _FakeRestClient()
    channel = _FakeChannel([BotTurn(text="hi", options=[], step=1)])
    sessions = _FakeSessionManager([channel])
    handler = RemoteFlowAgentHandler(_remote_spec(), _deps(rest, sessions))

    handler.handle(_ctx("go", session=_session(remote_session_id=None)))

    assert channel.sent[0][1] == 60.0  # first_turn_timeout_ms / 1000


def test_subsequent_turn_does_not_recreate_profile_or_session():
    rest = _FakeRestClient()
    channel = _FakeChannel([BotTurn(text="continuing", options=[], step=2)])
    sessions = _FakeSessionManager([channel])
    handler = RemoteFlowAgentHandler(_remote_spec(), _deps(rest, sessions))

    ctx = _ctx("Priya", session=_session(remote_session_id="already-set", step=1))
    turn = handler.handle(ctx)

    assert rest.upsert_profile_calls == []
    assert rest.generate_session_calls == 0
    assert channel.sent[0][1] == 45.0  # turn_timeout_ms / 1000
    assert turn.session_delta.remote_session_id == "already-set"


# ---------------------------------------------------------------------------
# Acceptance: exactly one re-establishment attempt on connection loss
# ---------------------------------------------------------------------------


def test_exactly_one_reestablishment_attempt_on_connection_loss():
    rest = _FakeRestClient()
    failing_channel = _FakeChannel([MitraChannelClosed("dropped")])
    retry_channel = _FakeChannel([BotTurn(text="recovered", options=[], step=3)])
    sessions = _FakeSessionManager([failing_channel, retry_channel])
    handler = RemoteFlowAgentHandler(_remote_spec(), _deps(rest, sessions))

    ctx = _ctx("Priya", session=_session(remote_session_id="already-set", step=2))
    turn = handler.handle(ctx)

    assert len(sessions.acquire_calls) == 1
    assert len(sessions.reacquire_calls) == 1
    assert turn.text == "recovered"


def test_second_connection_loss_is_not_retried_again():
    rest = _FakeRestClient()
    failing_channel = _FakeChannel([MitraChannelClosed("dropped")])
    also_failing_channel = _FakeChannel([MitraChannelClosed("dropped again")])
    sessions = _FakeSessionManager([failing_channel, also_failing_channel])
    handler = RemoteFlowAgentHandler(_remote_spec(), _deps(rest, sessions))

    ctx = _ctx("Priya", session=_session(remote_session_id="already-set", step=2))
    with pytest.raises(MitraChannelClosed):
        handler.handle(ctx)

    assert len(sessions.acquire_calls) == 1
    assert len(sessions.reacquire_calls) == 1  # not called a second time


def test_other_exceptions_are_not_retried():
    rest = _FakeRestClient()
    channel = _FakeChannel([MitraTurnTimeout(step=1)])
    sessions = _FakeSessionManager([channel])
    handler = RemoteFlowAgentHandler(_remote_spec(), _deps(rest, sessions))

    ctx = _ctx("Priya", session=_session(remote_session_id="already-set"))
    with pytest.raises(MitraTurnTimeout):
        handler.handle(ctx)
    assert len(sessions.reacquire_calls) == 0


# ---------------------------------------------------------------------------
# Acceptance: no history is ever sent
# ---------------------------------------------------------------------------


def test_no_history_is_ever_sent():
    rest = _FakeRestClient()
    channel = _FakeChannel([BotTurn(text="ok", options=[], step=1)])
    sessions = _FakeSessionManager([channel])
    handler = RemoteFlowAgentHandler(_remote_spec(), _deps(rest, sessions))

    poisoned_history = [
        HistoryTurn(role="user", content="SECRET_PRIOR_MESSAGE_MARKER", agent_key=None),
        HistoryTurn(role="assistant", content="ANOTHER_SECRET_MARKER", agent_key=None),
    ]
    ctx = _ctx("current message", session=_session(remote_session_id="already-set"), history=poisoned_history)
    handler.handle(ctx)

    assert channel.sent == [("current message", 45.0, 8.0)]
    for call_args in rest.upsert_profile_calls:
        assert "SECRET_PRIOR_MESSAGE_MARKER" not in str(call_args)
        assert "ANOTHER_SECRET_MARKER" not in str(call_args)


# ---------------------------------------------------------------------------
# Completion polling / terminal signaling
# ---------------------------------------------------------------------------


def test_completed_session_returns_terminal_true_and_awaiting_user_state():
    """The handler must NEVER put state=finalizing in its own delta -- that
    transition belongs exclusively to OrchestrationService._finalize's own
    claim_finalizing() call (design doc §4.7's idempotency guard, whose
    claimable set is only {in_progress, awaiting_user}). If the delta here
    set finalizing itself, orchestration.py's step 11 would apply that
    BEFORE calling _finalize(), so the claim would find the session already
    in 'finalizing' and fail -- wedging it there forever, since nothing
    ever revisits it. terminal=True alone is the correct, sufficient signal
    for OrchestrationService to call _finalize() next."""
    rest = _FakeRestClient()
    rest._completed = True
    channel = _FakeChannel([BotTurn(text="Thank you!", options=[], step=14)])
    sessions = _FakeSessionManager([channel])
    handler = RemoteFlowAgentHandler(_remote_spec(), _deps(rest, sessions))

    ctx = _ctx("last answer", session=_session(remote_session_id="already-set"))
    turn = handler.handle(ctx)

    assert turn.terminal is True
    assert turn.session_delta.state == SessionState.awaiting_user


def test_incomplete_session_returns_not_terminal_and_awaiting_user():
    rest = _FakeRestClient()
    rest._completed = False
    channel = _FakeChannel([BotTurn(text="tell me more", options=[], step=5)])
    sessions = _FakeSessionManager([channel])
    handler = RemoteFlowAgentHandler(_remote_spec(), _deps(rest, sessions))

    turn = handler.handle(_ctx("go on", session=_session(remote_session_id="already-set")))

    assert turn.terminal is False
    assert turn.session_delta.state == SessionState.awaiting_user


def test_completion_poll_every_turn_false_never_polls():
    rest = _FakeRestClient()
    channel = _FakeChannel([BotTurn(text="ok", options=[], step=1)])
    sessions = _FakeSessionManager([channel])
    spec = _remote_spec(completion_poll_every_turn=False)
    handler = RemoteFlowAgentHandler(spec, _deps(rest, sessions))

    turn = handler.handle(_ctx("go", session=_session(remote_session_id="already-set")))

    assert rest.is_session_completed_calls == []
    assert turn.terminal is False


def test_options_are_normalised_from_bot_turn():
    rest = _FakeRestClient()
    parsed = [ParsedOption(id="en", label="English", value="en")]
    channel = _FakeChannel([BotTurn(text="pick one", options=parsed, step=1)])
    sessions = _FakeSessionManager([channel])
    handler = RemoteFlowAgentHandler(_remote_spec(), _deps(rest, sessions))

    turn = handler.handle(_ctx("go", session=_session(remote_session_id="already-set")))

    assert len(turn.options) == 1
    assert turn.options[0].id == "en"
    assert turn.options[0].label == "English"


# ---------------------------------------------------------------------------
# Environment resolution
# ---------------------------------------------------------------------------


def test_missing_bot_route_env_raises(monkeypatch):
    monkeypatch.delenv("TEST_BOT_ROUTE", raising=False)
    rest = _FakeRestClient()
    sessions = _FakeSessionManager([])
    handler = RemoteFlowAgentHandler(_remote_spec(), _deps(rest, sessions))

    with pytest.raises(RuntimeError):
        handler.handle(_ctx("go", session=_session(remote_session_id="already-set")))


def test_missing_company_env_raises_on_first_turn(monkeypatch):
    monkeypatch.delenv("TEST_COMPANY", raising=False)
    rest = _FakeRestClient()
    sessions = _FakeSessionManager([])
    handler = RemoteFlowAgentHandler(_remote_spec(), _deps(rest, sessions))

    with pytest.raises(RuntimeError):
        handler.handle(_ctx("go", session=_session(remote_session_id=None)))
