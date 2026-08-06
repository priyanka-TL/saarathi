"""Integration tests for OrchestrationService._finalize (design doc §4.7, §8.5).

Real Postgres for SessionService/repositories (the whole point is exercising
the real claim_finalizing/apply/audit/unpin logic together) -- only the
Mitra-facing REST client and channel pool are faked, matching
tests/unit/test_remote_flow_handler.py's established fake style.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import pytest
from sqlalchemy import text, select

from app.database.engine import SessionLocal
from app.models.orm import Conversation
from app.domain.core import UserContext
from app.domain.agent_spec import (
    MitraConnectionSpec,
    RemoteFlowAgentSpec,
    RemoteSpec,
    RoutingSpec,
)

#: Required on every RemoteSpec now -- the MITRA_* environment floor is gone.
_CONNECTION = MitraConnectionSpec(
    base_url="https://mitra.example.com",
    ws_url="wss://mitra.example.com/ws/common/",
)
from app.agents.protocol import SessionDelta, SessionState
from app.repositories.conversations import ConversationRepository
from app.repositories.sessions import AgentSessionRepository
from app.services.orchestration import OrchestrationService


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeMitraRest:
    def __init__(self, call_order: List[str]):
        self._call_order = call_order
        self.finalize_calls: List[Tuple] = []
        self.finalize_paths: List[str] = []
        self.finalize_as_guest: List[bool] = []
        self.get_report_calls: List[Tuple] = []
        self._story_id = "9931"
        self._content = "narrative content"
        self._report_url: Optional[str] = None
        self._finalize_error: Optional[Exception] = None

    def finalize(
        self, session_id, profile_id, flow, language, token,
        path="/api/end-story/v2/", as_guest=False,
    ):
        self._call_order.append("finalize")
        self.finalize_calls.append((session_id, profile_id, flow, language, token))
        self.finalize_paths.append(path)
        self.finalize_as_guest.append(as_guest)
        if self._finalize_error is not None:
            raise self._finalize_error
        return self._story_id, self._content

    def get_report(self, session_id, media_type="application/pdf"):
        self._call_order.append("get_report")
        self.get_report_calls.append((session_id, media_type))
        return self._report_url


class _FakeMitraSessions:
    def __init__(self, call_order: List[str]):
        self._call_order = call_order
        self.close_calls: List[uuid.UUID] = []

    def close(self, conversation_id):
        self._call_order.append("close")
        self.close_calls.append(conversation_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_agent_row(session, key: str) -> uuid.UUID:
    row = session.execute(text("""
        INSERT INTO agents (key, name, description, agent_type)
        VALUES (:key, :key, 'test agent', 'remote_flow') RETURNING id
    """), {"key": key}).fetchone()
    return row[0]


def _new_user(token: Optional[str] = "user-jwt-token") -> UserContext:
    return UserContext(
        user_id=f"user_{uuid.uuid4().hex[:8]}",
        email="test@example.com",
        display_name="Test User",
        tenant_code=f"TENANT_{uuid.uuid4().hex[:8]}",
        token=token,
    )


def _new_conversation(session, user=None) -> uuid.UUID:
    repo = ConversationRepository(session)
    conv = repo.get_or_create(None, user or _new_user())
    return conv.id


@dataclass
class _Agent:
    id: uuid.UUID
    spec: RemoteFlowAgentSpec
    checksum: str = "test-checksum"

    @property
    def key(self) -> str:
        return self.spec.key


def _remote_agent(agent_id: uuid.UUID) -> _Agent:
    remote = RemoteSpec(
        provider="mitra",
        flow_name="guest-mi-story",
        bot_route="/test-bot-route",
        company="test-company",
        connection=_CONNECTION,
        report_media_type="application/pdf",
        finalize_path="/api/end-story/",
    )
    spec = RemoteFlowAgentSpec(
        key="record_stories",
        name="Record Stories",
        description="test",
        agent_type="remote_flow",
        routing=RoutingSpec(pin_session=True, exit_keywords=["/exit"]),
        remote=remote,
    )
    return _Agent(id=agent_id, spec=spec)


def _session_ready_for_finalizing(session, conv_id, agent_id) -> "AgentSessionDTO":
    """Drives a real agent_sessions row up to 'awaiting_user' with
    remote_session_id/remote_profile_id/language set -- the real precondition
    for claim_finalizing to succeed."""
    from app.services.session_service import SessionService
    svc = SessionService(session)
    repo = AgentSessionRepository(session)
    pending = repo.create_pending(conv_id, agent_id)
    authing = svc.apply(pending, SessionDelta(
        state=SessionState.authenticating,
        remote_session_id=f"remote-{uuid.uuid4().hex[:8]}",
        remote_profile_id=f"profile-{uuid.uuid4().hex[:8]}",
    ))
    in_progress = svc.apply(authing, SessionDelta(state=SessionState.in_progress))
    awaiting = svc.apply(in_progress, SessionDelta(state=SessionState.awaiting_user, step=5))
    return awaiting


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_won_claim_runs_full_sequence_in_order():
    session = SessionLocal()
    try:
        agent_id = _insert_agent_row(session, f"agent_{uuid.uuid4().hex[:8]}")
        conv_id = _new_conversation(session)
        sess_view = _session_ready_for_finalizing(session, conv_id, agent_id)
        session.commit()

        call_order: List[str] = []
        rest = _FakeMitraRest(call_order)
        pool = _FakeMitraSessions(call_order)
        orch = OrchestrationService(
            session=session, registry=None, handler_factory=None, llm_factory=None,
            mitra_rest=rest, mitra_sessions=pool,
        )

        user = _new_user(token="the-real-token")
        agent = _remote_agent(agent_id)
        result = orch._finalize(sess_view, agent, user)
        session.commit()

        # Ordering: close BEFORE finalize.
        assert call_order.index("close") < call_order.index("finalize")

        # finalize() called with the right args.
        assert len(rest.finalize_calls) == 1
        called_sid, called_pid, called_flow, called_lang, called_token = rest.finalize_calls[0]
        assert called_sid == sess_view.remote_session_id
        assert called_pid == sess_view.remote_profile_id
        assert called_flow == "guest-mi-story"
        assert called_lang == sess_view.language
        assert called_token == "the-real-token"

        # ...and at the endpoint the AGENT declares, not a hardcoded one.
        # 'guest-mi-story' has no Flow row in Mitra, so v2 answers it with an
        # HTTP 500; the spec's finalize_path is what keeps this flow off v2.
        assert rest.finalize_paths == [agent.spec.remote.finalize_path]

        # Same for the token-presence flag. This agent sends its token; the
        # discussion agent does not, because Mitra picks the PDF template's
        # user_type from token presence and a guest flow finalised WITH a token
        # renders a blank PDF. Hardcoding either value here would break one of
        # the two agents silently -- one with an HTTP 500, one with an empty
        # file and no error at all.
        assert rest.finalize_as_guest == [agent.spec.remote.finalize_as_guest]
        assert rest.finalize_as_guest == [False], "record_stories sends its token"

        # Resulting session is completed with result_ref/finalized_at/ended_at.
        assert result.state == "completed"
        assert result.result_ref == "9931"
        assert result.finalized_at is not None
        assert result.ended_at is not None

        # Conversation released for routing: 'completed' is terminal, so the
        # conversation has no open session and RouterService Gate 2 will not
        # find one. That state transition replaced the separate unpin write.
        assert AgentSessionRepository(session).get_open_for_conversation(conv_id) is None

        # Audit row written.
        audit_rows = session.execute(text(
            "SELECT action, entity_type, entity_id FROM audit_logs WHERE entity_id = :id"
        ), {"id": result.id}).fetchall()
        assert len(audit_rows) == 1
        assert audit_rows[0][0] == "session_finalize"
        assert audit_rows[0][1] == "agent_session"
    finally:
        session.close()


def test_a_guest_flow_agent_finalizes_without_the_users_token():
    """The other half of the pin above, from the discussion agent's side.

    capture_discussion sets finalize_as_guest: true because Mitra derives
    `auth = access_token is not None` and picks the PDF template's user_type
    from it -- so finalising with a token misses the GUEST-typed template,
    get_html_from_template returns "" and Gotenberg produces a valid, blank
    PDF. _finalize must forward the spec value; a hardcoded False here would
    reintroduce the empty report with every other signal still green.

    The user's token is still handed to finalize() (the client ignores it when
    as_guest is set), so nothing about UserContext changes for guest flows.
    """
    session = SessionLocal()
    try:
        agent_id = _insert_agent_row(session, f"agent_{uuid.uuid4().hex[:8]}")
        conv_id = _new_conversation(session)
        sess_view = _session_ready_for_finalizing(session, conv_id, agent_id)
        session.commit()

        call_order: List[str] = []
        rest = _FakeMitraRest(call_order)
        orch = OrchestrationService(
            session=session, registry=None, handler_factory=None, llm_factory=None,
            mitra_rest=rest, mitra_sessions=_FakeMitraSessions(call_order),
        )

        agent = _remote_agent(agent_id)
        agent.spec.remote.flow_name = "guest-discussion"
        agent.spec.remote.finalize_as_guest = True

        orch._finalize(sess_view, agent, _new_user(token="the-real-token"))
        session.commit()

        assert rest.finalize_as_guest == [True]
        assert rest.finalize_calls[0][4] == "the-real-token", (
            "the token is still passed; only the client decides to drop it"
        )
    finally:
        session.close()


def test_report_url_set_when_present():
    session = SessionLocal()
    try:
        agent_id = _insert_agent_row(session, f"agent_{uuid.uuid4().hex[:8]}")
        conv_id = _new_conversation(session)
        sess_view = _session_ready_for_finalizing(session, conv_id, agent_id)
        session.commit()

        call_order: List[str] = []
        rest = _FakeMitraRest(call_order)
        rest._report_url = "https://example.com/story.pdf"
        pool = _FakeMitraSessions(call_order)
        orch = OrchestrationService(
            session=session, registry=None, handler_factory=None, llm_factory=None,
            mitra_rest=rest, mitra_sessions=pool,
        )

        result = orch._finalize(sess_view, _remote_agent(agent_id), _new_user())
        session.commit()

        assert result.report_url == "https://example.com/story.pdf"
    finally:
        session.close()


def test_report_url_left_null_when_absent():
    session = SessionLocal()
    try:
        agent_id = _insert_agent_row(session, f"agent_{uuid.uuid4().hex[:8]}")
        conv_id = _new_conversation(session)
        sess_view = _session_ready_for_finalizing(session, conv_id, agent_id)
        session.commit()

        call_order: List[str] = []
        rest = _FakeMitraRest(call_order)  # _report_url stays None
        pool = _FakeMitraSessions(call_order)
        orch = OrchestrationService(
            session=session, registry=None, handler_factory=None, llm_factory=None,
            mitra_rest=rest, mitra_sessions=pool,
        )

        result = orch._finalize(sess_view, _remote_agent(agent_id), _new_user())
        session.commit()

        assert result.report_url is None
        assert result.state == "completed"  # still completed, just no report yet
    finally:
        session.close()


def test_finalize_exception_transitions_to_failed_and_propagates():
    session = SessionLocal()
    try:
        agent_id = _insert_agent_row(session, f"agent_{uuid.uuid4().hex[:8]}")
        conv_id = _new_conversation(session)
        sess_view = _session_ready_for_finalizing(session, conv_id, agent_id)
        session.commit()

        call_order: List[str] = []
        rest = _FakeMitraRest(call_order)
        rest._finalize_error = RuntimeError("upstream exploded")
        pool = _FakeMitraSessions(call_order)
        orch = OrchestrationService(
            session=session, registry=None, handler_factory=None, llm_factory=None,
            mitra_rest=rest, mitra_sessions=pool,
        )

        with pytest.raises(RuntimeError, match="upstream exploded"):
            orch._finalize(sess_view, _remote_agent(agent_id), _new_user())
        session.commit()

        fresh = AgentSessionRepository(session).get(sess_view.id)
        assert fresh.state == "failed"
        assert fresh.error == "upstream exploded"
    finally:
        session.close()


def test_losing_claim_returns_cached_state_without_calling_finalize():
    session = SessionLocal()
    try:
        agent_id = _insert_agent_row(session, f"agent_{uuid.uuid4().hex[:8]}")
        conv_id = _new_conversation(session)
        sess_view = _session_ready_for_finalizing(session, conv_id, agent_id)
        session.commit()

        # Simulate another request already winning the claim.
        from app.services.session_service import SessionService
        SessionService(session).claim_finalizing(sess_view.id)
        session.commit()

        call_order: List[str] = []
        rest = _FakeMitraRest(call_order)
        pool = _FakeMitraSessions(call_order)
        orch = OrchestrationService(
            session=session, registry=None, handler_factory=None, llm_factory=None,
            mitra_rest=rest, mitra_sessions=pool,
        )

        result = orch._finalize(sess_view, _remote_agent(agent_id), _new_user())

        assert result.state == "finalizing"
        assert len(rest.finalize_calls) == 0
        assert len(pool.close_calls) == 0

        audit_rows = session.execute(text(
            "SELECT count(*) FROM audit_logs WHERE entity_id = :id"
        ), {"id": sess_view.id}).scalar()
        assert audit_rows == 0
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Regression: a real bug found in production -- a handler returning
# terminal=True with state=finalizing IN ITS OWN DELTA (rather than leaving
# the delta at awaiting_user and letting terminal=True alone drive
# _finalize()) wedges the session in 'finalizing' forever. handle_turn()'s
# own step 11 applies the handler's delta BEFORE calling _finalize(), so
# claim_finalizing() -- claimable only from {in_progress, awaiting_user} --
# finds the session already 'finalizing' and can never claim it. Every test
# above calls orch._finalize() directly, bypassing handle_turn() entirely,
# so none of them exercise the interaction between step 11's apply() and
# _finalize() at all.
#
# This test alone does NOT catch a regression of the original bug -- it uses
# a fake handler that always emits the correct contract, so it only proves
# handle_turn() correctly drives a well-behaved handler's delta through to
# 'completed'. The other half -- proving RemoteFlowAgentHandler itself emits
# that correct contract -- is
# tests/unit/test_remote_flow_handler.py::test_completed_session_returns_terminal_true_and_awaiting_user_state.
# The two together are what would have caught this before it shipped.
# ---------------------------------------------------------------------------


class _FakeTerminalHandler:
    """A handler that DOES follow the correct contract: terminal=True with
    session_delta.state left at awaiting_user. This is what
    RemoteFlowAgentHandler.handle() must produce (design doc §4.7)."""
    def handle(self, ctx):
        from app.agents.protocol import AgentTurn
        return AgentTurn(
            text="Thank you, your story is complete.",
            terminal=True,
            session_delta=SessionDelta(state=SessionState.awaiting_user, step=99),
        )


class _FakeHandlerFactory:
    def __init__(self, handler):
        self._handler = handler

    def build(self, spec, checksum):
        return self._handler


class _FakeRouter:
    def __init__(self, agent):
        self._agent = agent

    def select(self, conv, ctx, explicit_key=None):
        from app.services.router_service import RouteDecision
        return RouteDecision(agent=self._agent, reason="pinned", confidence=1.0, router_latency_ms=0)


class _FakeRegistry:
    """handle_turn() calls registry.default() unconditionally while building
    router history context, even though this test's router never needs it."""
    def __init__(self, agent=None):
        self._agent = agent

    def default(self):
        return None

    def get_by_id(self, agent_id):
        """Only finalize_now() reaches this, via agent_for_session()."""
        return self._agent

    def resolve_for_scope(self, session, agent, tenant_id, organization_id):
        """Mirrors the real method's no-op path: a tenant with no scoped config
        gets the agent back unchanged. This test's agent has no scoped config,
        so returning the argument IS the faithful behaviour."""
        return agent


def test_handle_turn_with_terminal_delta_actually_reaches_completed():
    """The concrete end-to-end regression test for the bug described above."""
    session = SessionLocal()
    try:
        agent_id = _insert_agent_row(session, f"agent_{uuid.uuid4().hex[:8]}")
        user = _new_user(token="the-real-token")
        conv_id = _new_conversation(session, user=user)
        sess_view = _session_ready_for_finalizing(session, conv_id, agent_id)
        session.commit()

        call_order: List[str] = []
        rest = _FakeMitraRest(call_order)
        pool = _FakeMitraSessions(call_order)
        agent = _remote_agent(agent_id)

        orch = OrchestrationService(
            session=session,
            registry=_FakeRegistry(),
            handler_factory=_FakeHandlerFactory(_FakeTerminalHandler()),
            llm_factory=None,
            router_service=_FakeRouter(agent),
            mitra_rest=rest,
            mitra_sessions=pool,
        )

        from app.services.orchestration import TurnInput
        ctx_in = TurnInput(
            request_id="req-1", conversation_id=conv_id, user=user, text="last answer",
        )
        result = orch.handle_turn(ctx_in)
        session.commit()

        assert result.session is not None
        assert result.session.state == "completed", (
            "a handler reporting terminal=True must actually reach 'completed', "
            "not get stuck in 'finalizing'"
        )
        assert result.session.result_ref == "9931"
        assert len(rest.finalize_calls) == 1

        fresh = AgentSessionRepository(session).get(sess_view.id)
        assert fresh.state == "completed"
    finally:
        session.close()


# ---------------------------------------------------------------------------
# The post-interview follow-up, stored as a real message.
#
# The completion notice above it (report ready + download link) is rendered by
# the client from the agent_sessions row and deliberately has no message row.
# This is the other thing: Saarthi taking a conversational turn, so it has to
# survive a reload and reach the next agent as history.
# ---------------------------------------------------------------------------


def _transcript(session, conv_id):
    """(content, agent_id, agent_session_id) per message, in transcript order."""
    return session.execute(text(
        "SELECT content, agent_id, agent_session_id FROM conversation_messages "
        "WHERE conversation_id = :cid ORDER BY seq"
    ), {"cid": conv_id}).fetchall()


def test_a_completed_interview_asks_the_user_what_else_they_need():
    """...and asks it BELOW the agent's closing line, not above it.

    seq orders the transcript, and _finalize runs at step 10 -- before step 11
    inserts the reply that triggered it. Writing the follow-up from inside
    _finalize would put "anything else?" ahead of "Thank you, your story is
    complete."
    """
    session = SessionLocal()
    try:
        agent_id = _insert_agent_row(session, f"agent_{uuid.uuid4().hex[:8]}")
        user = _new_user(token="the-real-token")
        conv_id = _new_conversation(session, user=user)
        _session_ready_for_finalizing(session, conv_id, agent_id)
        session.commit()

        agent = _remote_agent(agent_id)
        orch = OrchestrationService(
            session=session,
            registry=_FakeRegistry(agent),
            handler_factory=_FakeHandlerFactory(_FakeTerminalHandler()),
            llm_factory=None,
            router_service=_FakeRouter(agent),
            mitra_rest=_FakeMitraRest([]),
            mitra_sessions=_FakeMitraSessions([]),
        )

        from app.services.orchestration import SESSION_FOLLOW_UP, TurnInput
        orch.handle_turn(TurnInput(
            request_id="req-1", conversation_id=conv_id, user=user, text="last answer",
        ))
        session.commit()

        rows = _transcript(session, conv_id)
        assert [r[0] for r in rows] == [
            "last answer",
            "Thank you, your story is complete.",
            SESSION_FOLLOW_UP,
        ]

        follow_up = rows[-1]
        assert follow_up[1] == agent_id, (
            "ck_conversation_messages_assistant_attribution allows no anonymous "
            "assistant row, and naming the session's own agent keeps the flow "
            "breadcrumb unchanged -- distinct_agent_sequence collapses the repeat"
        )
        assert follow_up[2] is None, (
            "tagging it with the session would make the client anchor the completion "
            "notice AFTER it, putting the Download PDF link below the follow-up"
        )
    finally:
        session.close()


def test_a_repeated_forced_finalize_does_not_ask_twice():
    """POST /api/sessions/{id}/finalize is idempotent by design (§10.2): the
    second call loses the claim and reads the completed session back. Only the
    call that actually finalised may speak, or every repeat appends another
    "anything else?" to the transcript."""
    session = SessionLocal()
    try:
        agent_id = _insert_agent_row(session, f"agent_{uuid.uuid4().hex[:8]}")
        user = _new_user()
        conv_id = _new_conversation(session, user=user)
        sess_view = _session_ready_for_finalizing(session, conv_id, agent_id)
        session.commit()

        agent = _remote_agent(agent_id)
        rest = _FakeMitraRest([])
        orch = OrchestrationService(
            session=session, registry=_FakeRegistry(agent), handler_factory=None,
            llm_factory=None, mitra_rest=rest, mitra_sessions=_FakeMitraSessions([]),
        )

        from app.services.orchestration import SESSION_FOLLOW_UP
        first = orch.finalize_now(sess_view.id, user)
        second = orch.finalize_now(sess_view.id, user)
        session.commit()

        assert first.state == "completed"
        assert second.state == "completed", "the repeat still reports the real state"
        assert len(rest.finalize_calls) == 1, "the claim guard itself must still hold"

        contents = [r[0] for r in _transcript(session, conv_id)]
        assert contents.count(SESSION_FOLLOW_UP) == 1
    finally:
        session.close()
