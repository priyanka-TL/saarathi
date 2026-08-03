import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text, select

from app.database.engine import SessionLocal
from app.models.orm import Conversation
from app.domain.core import UserContext
from app.domain.agent_spec import RemoteFlowAgentSpec, RemoteSpec, RoutingSpec
from app.agents.protocol import SessionDelta, SessionState
from app.repositories.conversations import ConversationRepository
from app.repositories.sessions import AgentSessionRepository
from app.services.session_service import (
    SessionService,
    InvalidTransitionError,
)


def _insert_agent_row(session, key: str) -> uuid.UUID:
    row = session.execute(text("""
        INSERT INTO agents (key, name, description, agent_type)
        VALUES (:key, :key, 'test agent', 'remote_flow') RETURNING id
    """), {"key": key}).fetchone()
    return row[0]


class _RegisteredAgentStub:
    """Duck-types app.services.agent_registry.RegisteredAgent's fields that
    SessionService.open_for actually reads (id, spec.routing.pin_session)."""
    def __init__(self, agent_id: uuid.UUID, pin_session: bool):
        self.id = str(agent_id)
        self.spec = RemoteFlowAgentSpec(
            key="test_remote_flow",
            name="Test Remote Flow",
            description="test agent for session service tests",
            agent_type="remote_flow",
            routing=RoutingSpec(pin_session=pin_session, exit_keywords=["/exit"]),
            remote=RemoteSpec(provider="mitra", flow_name="guest-mi-story", bot_route="/test-bot-route", company="test-company"),
        )


def _new_user() -> UserContext:
    return UserContext(
        user_id=f"user_{uuid.uuid4().hex[:8]}",
        email="test@example.com",
        display_name="Test User",
        tenant_code=f"TENANT_{uuid.uuid4().hex[:8]}",
    )


def _new_conversation(session) -> uuid.UUID:
    repo = ConversationRepository(session)
    conv = repo.get_or_create(None, _new_user())
    return conv.id


# ---------------------------------------------------------------------------
# open_for
# ---------------------------------------------------------------------------


def test_open_for_creates_pending_when_pin_session():
    session = SessionLocal()
    try:
        agent_id = _insert_agent_row(session, f"agent_{uuid.uuid4().hex[:8]}")
        conv_id = _new_conversation(session)
        session.commit()

        svc = SessionService(session)
        agent = _RegisteredAgentStub(agent_id, pin_session=True)
        result = svc.open_for(conv_id, agent)
        session.commit()

        assert result is not None
        assert result.state == "pending"
        assert result.agent_id == agent_id
        assert result.conversation_id == conv_id
    finally:
        session.close()


def test_open_for_returns_none_without_pin_session_and_no_open_session():
    session = SessionLocal()
    try:
        agent_id = _insert_agent_row(session, f"agent_{uuid.uuid4().hex[:8]}")
        conv_id = _new_conversation(session)
        session.commit()

        svc = SessionService(session)
        agent = _RegisteredAgentStub(agent_id, pin_session=False)
        assert svc.open_for(conv_id, agent) is None
    finally:
        session.close()


def test_open_for_attaches_existing_open_session():
    session = SessionLocal()
    try:
        agent_id = _insert_agent_row(session, f"agent_{uuid.uuid4().hex[:8]}")
        conv_id = _new_conversation(session)
        svc = SessionService(session)
        agent = _RegisteredAgentStub(agent_id, pin_session=True)

        first = svc.open_for(conv_id, agent)
        session.commit()

        second = svc.open_for(conv_id, agent)
        assert second is not None
        assert second.id == first.id
    finally:
        session.close()


def test_open_for_never_hands_one_agents_session_to_another():
    """THE cross-agent hijack regression.

    open_for used to return whichever open session the conversation had,
    "regardless of which agent it belongs to", trusting the caller to have
    pinned the right agent first. RouterService Gate 1 (explicit selection from
    the UI) never consults the pin, so with currentAgentKey still set to
    capture_discussion, opening a Record Stories conversation from the sidebar
    ran the next turn against the STORY session's remote_session_id -- and
    RemoteFlowAgentHandler then overwrote its remote_bot_route and remote_flow
    with the discussion agent's.

    A conversation spanning several agents is intended, so the old session is
    abandoned rather than the turn refused.
    """
    session = SessionLocal()
    try:
        story_agent_id = _insert_agent_row(session, f"agent_{uuid.uuid4().hex[:8]}")
        discussion_agent_id = _insert_agent_row(session, f"agent_{uuid.uuid4().hex[:8]}")
        conv_id = _new_conversation(session)
        svc = SessionService(session)

        story_session = svc.open_for(conv_id, _RegisteredAgentStub(story_agent_id, pin_session=True))
        # Give it a remote handle, exactly as a first turn would.
        svc.apply(story_session, SessionDelta(
            state=SessionState.awaiting_user,
            remote_session_id="story-remote-sid",
            remote_flow="guest-mi-story",
        ))
        session.commit()

        displaced: list[uuid.UUID] = []
        discussion_session = svc.open_for(
            conv_id,
            _RegisteredAgentStub(discussion_agent_id, pin_session=True),
            on_displace=displaced.append,
        )
        session.commit()

        assert discussion_session is not None
        assert discussion_session.id != story_session.id, "adopted the other agent's session"
        assert discussion_session.agent_id == discussion_agent_id
        assert discussion_session.remote_session_id is None, (
            "the new agent inherited the other agent's Mitra session id"
        )
        assert displaced == [conv_id], "the orphaned Mitra channel was not closed"

        # The story session is retired, and its remote handle is untouched.
        retired = AgentSessionRepository(session).get(story_session.id)
        assert retired.state == "abandoned"
        assert retired.remote_session_id == "story-remote-sid"
        assert retired.remote_flow == "guest-mi-story"
    finally:
        session.close()


def test_open_for_same_agent_still_resumes_without_displacing():
    """The multi-turn interview path must be unaffected: a second turn for the
    SAME agent attaches to the same session, remote handle intact."""
    session = SessionLocal()
    try:
        agent_id = _insert_agent_row(session, f"agent_{uuid.uuid4().hex[:8]}")
        conv_id = _new_conversation(session)
        svc = SessionService(session)
        agent = _RegisteredAgentStub(agent_id, pin_session=True)

        first = svc.open_for(conv_id, agent)
        svc.apply(first, SessionDelta(
            state=SessionState.awaiting_user, remote_session_id="keep-me",
        ))
        session.commit()

        displaced: list[uuid.UUID] = []
        second = svc.open_for(conv_id, agent, on_displace=displaced.append)

        assert second.id == first.id
        assert second.remote_session_id == "keep-me"
        assert displaced == []
    finally:
        session.close()


# ---------------------------------------------------------------------------
# apply(): invalid transitions rejected, same-state updates allowed
# ---------------------------------------------------------------------------


def test_apply_rejects_invalid_transition():
    session = SessionLocal()
    try:
        agent_id = _insert_agent_row(session, f"agent_{uuid.uuid4().hex[:8]}")
        conv_id = _new_conversation(session)
        repo = AgentSessionRepository(session)
        pending = repo.create_pending(conv_id, agent_id)
        session.commit()

        svc = SessionService(session)
        with pytest.raises(InvalidTransitionError):
            svc.apply(pending, SessionDelta(state=SessionState.completed, result_ref="9931"))
    finally:
        session.close()


def test_apply_rejects_transition_from_terminal_state():
    session = SessionLocal()
    try:
        agent_id = _insert_agent_row(session, f"agent_{uuid.uuid4().hex[:8]}")
        conv_id = _new_conversation(session)
        repo = AgentSessionRepository(session)
        pending = repo.create_pending(conv_id, agent_id)
        svc = SessionService(session)
        failed = svc.apply(pending, SessionDelta(state=SessionState.failed))
        session.commit()

        with pytest.raises(InvalidTransitionError):
            svc.apply(failed, SessionDelta(state=SessionState.failed))
    finally:
        session.close()


def test_apply_allows_same_state_field_only_update():
    session = SessionLocal()
    try:
        agent_id = _insert_agent_row(session, f"agent_{uuid.uuid4().hex[:8]}")
        conv_id = _new_conversation(session)
        repo = AgentSessionRepository(session)
        pending = repo.create_pending(conv_id, agent_id)
        session.commit()

        svc = SessionService(session)
        remote_id = f"remote-{uuid.uuid4().hex}"
        authing = svc.apply(pending, SessionDelta(state=SessionState.authenticating, remote_session_id=remote_id, remote_profile_id="42"))
        in_progress = svc.apply(authing, SessionDelta(state=SessionState.in_progress))
        awaiting = svc.apply(in_progress, SessionDelta(state=SessionState.awaiting_user, step=1))
        again = svc.apply(awaiting, SessionDelta(state=SessionState.awaiting_user, step=2))
        session.commit()

        assert again.state == "awaiting_user"
        assert again.step == 2
        assert again.turn_count == 4  # bumped once per apply() call (4 calls made above)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# claim_finalizing: concurrency
# ---------------------------------------------------------------------------


def test_concurrent_claim_finalizing_exactly_one_wins():
    session = SessionLocal()
    try:
        agent_id = _insert_agent_row(session, f"agent_{uuid.uuid4().hex[:8]}")
        conv_id = _new_conversation(session)
        repo = AgentSessionRepository(session)
        pending = repo.create_pending(conv_id, agent_id)
        svc = SessionService(session)
        remote_id = f"remote-{uuid.uuid4().hex}"
        authing = svc.apply(pending, SessionDelta(state=SessionState.authenticating, remote_session_id=remote_id))
        in_progress = svc.apply(authing, SessionDelta(state=SessionState.in_progress))
        awaiting = svc.apply(in_progress, SessionDelta(state=SessionState.awaiting_user, step=1))
        session.commit()
        session_id = awaiting.id
    finally:
        session.close()

    def try_claim():
        thread_session = SessionLocal()
        try:
            thread_svc = SessionService(thread_session)
            result = thread_svc.claim_finalizing(session_id)
            thread_session.commit()
            return result
        finally:
            thread_session.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(try_claim) for _ in range(2)]
        results = [f.result() for f in futures]

    winners = [r for r in results if r is not None]
    losers = [r for r in results if r is None]
    assert len(winners) == 1, "exactly one concurrent claim_finalizing call must succeed"
    assert len(losers) == 1
    assert winners[0].state == "finalizing"

    # The loser's caller would now fetch the cached result via get().
    verify = SessionLocal()
    try:
        cached = AgentSessionRepository(verify).get(session_id)
        assert cached.state == "finalizing"
    finally:
        verify.close()


def test_claim_finalizing_returns_none_when_not_claimable():
    session = SessionLocal()
    try:
        agent_id = _insert_agent_row(session, f"agent_{uuid.uuid4().hex[:8]}")
        conv_id = _new_conversation(session)
        repo = AgentSessionRepository(session)
        pending = repo.create_pending(conv_id, agent_id)  # state == 'pending', not claimable
        session.commit()

        svc = SessionService(session)
        assert svc.claim_finalizing(pending.id) is None
    finally:
        session.close()


# ---------------------------------------------------------------------------
# abandon(): unpins the conversation in the same transaction
# ---------------------------------------------------------------------------


def test_abandon_unpins_conversation_same_transaction():
    session = SessionLocal()
    try:
        agent_id = _insert_agent_row(session, f"agent_{uuid.uuid4().hex[:8]}")
        conv_id = _new_conversation(session)
        conv_repo = ConversationRepository(session)
        conv_repo.pin(conv_id, agent_id)

        repo = AgentSessionRepository(session)
        pending = repo.create_pending(conv_id, agent_id)
        svc = SessionService(session)
        remote_id = f"remote-{uuid.uuid4().hex}"
        authing = svc.apply(pending, SessionDelta(state=SessionState.authenticating, remote_session_id=remote_id))
        in_progress = svc.apply(authing, SessionDelta(state=SessionState.in_progress))
        svc.apply(in_progress, SessionDelta(state=SessionState.awaiting_user, step=1))
        session.commit()

        abandoned = svc.abandon(conv_id, reason="/exit")
        session.commit()

        assert abandoned is not None
        assert abandoned.state == "abandoned"
        assert abandoned.ended_at is not None
    finally:
        session.close()

    # Re-read with a FRESH session/connection to prove it's really committed,
    # not just visible in-process.
    verify = SessionLocal()
    try:
        conv = verify.execute(select(Conversation).where(Conversation.id == conv_id)).scalar_one()
        assert conv.pinned_agent_id is None

        sess_row = AgentSessionRepository(verify).get(abandoned.id)
        assert sess_row.state == "abandoned"

        audit_rows = verify.execute(text(
            "SELECT action, entity_type, entity_id, note FROM audit_logs WHERE entity_id = :id"
        ), {"id": abandoned.id}).fetchall()
        assert len(audit_rows) == 1
        assert audit_rows[0][0] == "session_abandon"
        assert audit_rows[0][1] == "agent_session"
        assert audit_rows[0][3] == "/exit"
    finally:
        verify.close()


def test_abandon_returns_none_when_no_open_session():
    session = SessionLocal()
    try:
        conv_id = _new_conversation(session)
        session.commit()

        svc = SessionService(session)
        assert svc.abandon(conv_id, reason="/exit") is None
    finally:
        session.close()


def test_abandon_works_from_in_progress_not_just_awaiting_user():
    """Abandon is an escape hatch reachable from ANY non-terminal state, per
    §6.5's pin lifecycle diagram (pinned --> abandoned drawn from the whole
    composite state) -- unlike apply()'s strict per-state map, which would
    reject in_progress -> abandoned."""
    session = SessionLocal()
    try:
        agent_id = _insert_agent_row(session, f"agent_{uuid.uuid4().hex[:8]}")
        conv_id = _new_conversation(session)
        repo = AgentSessionRepository(session)
        pending = repo.create_pending(conv_id, agent_id)
        svc = SessionService(session)
        remote_id = f"remote-{uuid.uuid4().hex}"
        authing = svc.apply(pending, SessionDelta(state=SessionState.authenticating, remote_session_id=remote_id))
        in_progress = svc.apply(authing, SessionDelta(state=SessionState.in_progress))
        session.commit()
        assert in_progress.state == "in_progress"

        abandoned = svc.abandon(conv_id, reason="crash recovery")
        session.commit()

        assert abandoned is not None
        assert abandoned.state == "abandoned"
    finally:
        session.close()
