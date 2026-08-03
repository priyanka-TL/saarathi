"""Concurrency test: two simultaneous terminal turns must produce exactly
ONE finalisation (design doc §4.7 -- the claim + uq_sess_remote make a
duplicate structurally impossible). Real Postgres, real threads; the Mitra
REST client and channel pool are faked since the point is proving
OrchestrationService._finalize's own control flow calls the upstream
finalize() exactly once under a genuine race -- SessionService.claim_finalizing's
own DB-level compare-and-swap guarantee is already proven in isolation by
tests/integration/test_session_service.py.
"""
from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Tuple

from sqlalchemy import text

from app.database.engine import SessionLocal
from app.domain.core import UserContext
from app.domain.agent_spec import RemoteFlowAgentSpec, RemoteSpec, RoutingSpec
from app.agents.protocol import SessionDelta, SessionState
from app.repositories.conversations import ConversationRepository
from app.repositories.sessions import AgentSessionRepository
from app.services.orchestration import OrchestrationService
from app.services.session_service import SessionService


class _FakeMitraRest:
    """Thread-safe-enough for this test: list.append is atomic under the
    GIL, and a lock guards the read-modify-write in finalize()."""
    def __init__(self):
        self._lock = threading.Lock()
        self.finalize_calls: List[Tuple] = []

    def finalize(
        self, session_id, profile_id, flow, language, token,
        path="/api/end-story/v2/", as_guest=False,
    ):
        with self._lock:
            self.finalize_calls.append((session_id, profile_id, flow, language, token))
        return "9931", "narrative content"

    def get_report(self, session_id, media_type="application/pdf"):
        return None


class _FakeMitraSessions:
    def __init__(self):
        self._lock = threading.Lock()
        self.close_calls: List[uuid.UUID] = []

    def close(self, conversation_id):
        with self._lock:
            self.close_calls.append(conversation_id)


def _insert_agent_row(session, key: str) -> uuid.UUID:
    row = session.execute(text("""
        INSERT INTO agents (key, name, description, agent_type)
        VALUES (:key, :key, 'test agent', 'remote_flow') RETURNING id
    """), {"key": key}).fetchone()
    return row[0]


def _new_user() -> UserContext:
    return UserContext(
        user_id=f"user_{uuid.uuid4().hex[:8]}",
        email="test@example.com",
        display_name="Test User",
        tenant_code=f"TENANT_{uuid.uuid4().hex[:8]}",
        token="the-token",
    )


def _remote_agent(agent_id: uuid.UUID):
    from dataclasses import dataclass

    @dataclass
    class _Agent:
        id: uuid.UUID
        spec: RemoteFlowAgentSpec

    remote = RemoteSpec(
        provider="mitra", flow_name="guest-mi-story",
        bot_route="/test-bot-route", company="test-company",
    )
    spec = RemoteFlowAgentSpec(
        key="record_stories", name="Record Stories", description="test",
        agent_type="remote_flow",
        routing=RoutingSpec(pin_session=True, exit_keywords=["/exit"]),
        remote=remote,
    )
    return _Agent(id=agent_id, spec=spec)


def test_concurrent_terminal_turns_produce_exactly_one_finalisation():
    setup_session = SessionLocal()
    try:
        agent_id = _insert_agent_row(setup_session, f"agent_{uuid.uuid4().hex[:8]}")
        conv_repo = ConversationRepository(setup_session)
        conv = conv_repo.get_or_create(None, _new_user())
        conv_repo.pin(conv.id, agent_id)

        svc = SessionService(setup_session)
        repo = AgentSessionRepository(setup_session)
        pending = repo.create_pending(conv.id, agent_id)
        authing = svc.apply(pending, SessionDelta(
            state=SessionState.authenticating,
            remote_session_id=f"remote-{uuid.uuid4().hex}",
            remote_profile_id="profile-1",
        ))
        in_progress = svc.apply(authing, SessionDelta(state=SessionState.in_progress))
        awaiting = svc.apply(in_progress, SessionDelta(state=SessionState.awaiting_user, step=10))
        setup_session.commit()
        session_id = awaiting.id
    finally:
        setup_session.close()

    shared_rest = _FakeMitraRest()
    shared_pool = _FakeMitraSessions()
    agent = _remote_agent(agent_id)
    user = _new_user()

    def try_finalize():
        thread_session = SessionLocal()
        try:
            orch = OrchestrationService(
                session=thread_session, registry=None, handler_factory=None, llm_factory=None,
                mitra_rest=shared_rest, mitra_sessions=shared_pool,
            )
            # Each thread re-reads its own view of the session, as a real
            # caller would (the terminal AgentTurn each request produced).
            sess_view = SessionService(thread_session).get(session_id)
            result = orch._finalize(sess_view, agent, user)
            thread_session.commit()
            return result
        finally:
            thread_session.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(try_finalize) for _ in range(2)]
        results = [f.result() for f in futures]

    assert len(shared_rest.finalize_calls) == 1, (
        "exactly one upstream finalize() call must happen across both concurrent requests"
    )

    # Both results must reflect the SAME real outcome -- the loser's `.get()`
    # fallback in _finalize returns whatever the DB shows at the moment it
    # checks. If the winner finished first, that's already 'completed' with
    # the real result_ref (as observed here); if the winner hadn't committed
    # yet, it would instead read 'finalizing'. Either is correct -- the
    # invariant is that finalize() itself only ran once, never that the
    # loser must observe an intermediate state.
    for r in results:
        assert r.state in ("finalizing", "completed")
    completed_results = [r for r in results if r.state == "completed"]
    assert len(completed_results) >= 1
    for r in completed_results:
        assert r.result_ref == "9931"

    verify = SessionLocal()
    try:
        fresh = AgentSessionRepository(verify).get(session_id)
        assert fresh.state == "completed"
        assert fresh.result_ref == "9931"
    finally:
        verify.close()
