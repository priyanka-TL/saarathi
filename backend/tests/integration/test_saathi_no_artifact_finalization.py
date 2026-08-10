"""A flow that produces no artifact must still close its session cleanly.

AGAINST A REAL DATABASE, deliberately. The risk this file exists for is a CHECK
constraint, not a branch: `ck_agent_sessions_completed_has_result` refuses a
'completed' row whose `result_ref` is null, and a mocked session service would
happily accept one. The failure mode in production is an IntegrityError on the
last turn of a working conversation.

Saathi is the first such provider -- its own /api/flow-connection-info/ reports
`create_story: "none"` -- and calling finalize() on it raises, which the
finalizer turns into a FAILED session. A normal conversation would look like an
outage.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import text

from app.agents.protocol import SessionDelta, SessionState
from app.database.engine import SessionLocal
from app.domain.agent_spec import (
    MitraConnectionSpec,
    RemoteSpec,
    RoutingSpec,
    SaathiFlowAgentSpec,
)
from app.domain.core import UserContext
from app.repositories.audit import AuditLogRepository
from app.repositories.conversations import ConversationRepository
from app.repositories.messages import MessageRepository
from app.repositories.sessions import AgentSessionRepository
from app.services.session_service import SessionService
from app.services.turn_finalization import TurnFinalizer

_CONNECTION = MitraConnectionSpec(
    base_url="https://qa.saathi.example.org",
    ws_url="wss://qa.saathi.example.org/ws/common/",
)


@dataclass
class _Agent:
    id: uuid.UUID
    spec: SaathiFlowAgentSpec
    checksum: str = "test-checksum"

    @property
    def key(self) -> str:
        return self.spec.key


def _saathi_agent(agent_id: uuid.UUID) -> _Agent:
    remote = RemoteSpec(
        provider="saathi",
        flow_name="saathi",
        bot_route="/saathi-bot",
        company="shikshalokamstaging",
        connection=_CONNECTION,
        # The two fields that define this path.
        produces_artifact=False,
        finalize_path=None,
    )
    spec = SaathiFlowAgentSpec(
        key="saathi",
        name="Saathi",
        description="test",
        agent_type="saathi_flow",
        routing=RoutingSpec(pin_session=True),
        remote=remote,
    )
    return _Agent(id=agent_id, spec=spec)


def _insert_agent_row(session, key: str) -> uuid.UUID:
    row = session.execute(text("""
        INSERT INTO agents (key, name, description, agent_type)
        VALUES (:key, :key, 'test agent', 'saathi_flow') RETURNING id
    """), {"key": key}).fetchone()
    return row[0]


def _new_user() -> UserContext:
    return UserContext(
        user_id=f"user_{uuid.uuid4().hex[:8]}",
        email="test@example.com",
        display_name="Test User",
        tenant_code=f"TENANT_{uuid.uuid4().hex[:8]}",
        token="user-jwt-token",
    )


def _session_awaiting_user(session, conv_id, agent_id):
    svc = SessionService(session)
    repo = AgentSessionRepository(session)
    pending = repo.create_pending(conv_id, agent_id)
    authing = svc.apply(pending, SessionDelta(
        state=SessionState.authenticating,
        remote_session_id=f"remote-{uuid.uuid4().hex[:8]}",
        remote_profile_id="88",
    ))
    in_progress = svc.apply(authing, SessionDelta(state=SessionState.in_progress))
    return svc.apply(in_progress, SessionDelta(state=SessionState.awaiting_user, step=3))


def _finalizer(session, *, rest_for=None):
    """A TurnFinalizer whose REST client explodes if anything reaches it.

    That is the assertion, not a convenience: a no-artifact flow must not call
    finalize() or get_report() at all.
    """
    def _boom(agent):
        raise AssertionError(
            "a produces_artifact=False flow must never reach the REST client"
        )

    return TurnFinalizer(
        sessions=SessionService(session),
        messages=MessageRepository(session),
        conversations=ConversationRepository(session),
        audit=AuditLogRepository(session),
        mitra_sessions=None,
        mitra_clients=None,
        mitra_rest=None,
        rest_for=rest_for or _boom,
    )


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def test_a_no_artifact_flow_completes_without_calling_finalize(db):
    """The whole point: no finalize(), and a clean 'completed' row.

    `rest_for` raises on any use, so reaching the REST client fails the test
    rather than silently making a network call.
    """
    user = _new_user()
    conv_id = ConversationRepository(db).get_or_create(None, user).id
    agent = _saathi_agent(_insert_agent_row(db, f"saathi_{uuid.uuid4().hex[:8]}"))
    session_view = _session_awaiting_user(db, conv_id, agent.id)

    result = _finalizer(db).finalize_claiming(session_view, agent, user)

    assert result.claimed is True
    assert result.session.state == "completed"


def test_result_ref_is_the_remote_session_id(db):
    """Satisfies ck_agent_sessions_completed_has_result WITHOUT weakening it.

    The value is not a placeholder: Saathi's transcript is retrievable with
    exactly this id (/api/companychat/?session=...), so it really is the
    reference to what the session produced.
    """
    user = _new_user()
    conv_id = ConversationRepository(db).get_or_create(None, user).id
    agent = _saathi_agent(_insert_agent_row(db, f"saathi_{uuid.uuid4().hex[:8]}"))
    session_view = _session_awaiting_user(db, conv_id, agent.id)
    remote_sid = session_view.remote_session_id

    result = _finalizer(db).finalize_claiming(session_view, agent, user)

    assert result.session.result_ref == remote_sid
    assert result.session.report_url is None


def test_the_completed_row_actually_survives_the_check_constraint(db):
    """THE REASON THIS FILE IS AN INTEGRATION TEST.

    A mocked session service would accept a null result_ref. Postgres will not.
    Flushing and reading the row back is what proves the constraint holds.
    """
    user = _new_user()
    conv_id = ConversationRepository(db).get_or_create(None, user).id
    agent = _saathi_agent(_insert_agent_row(db, f"saathi_{uuid.uuid4().hex[:8]}"))
    session_view = _session_awaiting_user(db, conv_id, agent.id)

    _finalizer(db).finalize_claiming(session_view, agent, user)
    db.flush()

    row = db.execute(
        text("SELECT state, result_ref, ended_at, finalized_at FROM agent_sessions WHERE id = :id"),
        {"id": session_view.id},
    ).fetchone()

    assert row.state == "completed"
    assert row.result_ref is not None
    # apply() sets both on a terminal transition; ck_agent_sessions_terminal
    # requires ended_at.
    assert row.ended_at is not None
    assert row.finalized_at is not None


def test_a_lost_claim_still_returns_the_current_row(db):
    """Idempotency is unchanged by the no-artifact branch: the claim runs first,
    so a second caller reads back rather than completing twice."""
    user = _new_user()
    conv_id = ConversationRepository(db).get_or_create(None, user).id
    agent = _saathi_agent(_insert_agent_row(db, f"saathi_{uuid.uuid4().hex[:8]}"))
    session_view = _session_awaiting_user(db, conv_id, agent.id)

    finalizer = _finalizer(db)
    first = finalizer.finalize_claiming(session_view, agent, user)
    second = finalizer.finalize_claiming(session_view, agent, user)

    assert first.claimed is True
    assert second.claimed is False
    assert second.session.state == "completed"


def test_an_artifact_flow_is_unaffected(db):
    """Mitra's path must be untouched: produces_artifact defaults to True, so
    the finalizer still reaches the REST client."""
    remote = RemoteSpec(
        provider="mitra",
        flow_name="guest-mi-story",
        bot_route="/guided_guest",
        company="c",
        connection=_CONNECTION,
        finalize_path="/api/end-story/",
    )
    assert remote.produces_artifact is True

    reached = []

    class _Rest:
        def finalize(self, **kwargs):
            reached.append("finalize")
            return "story-1", ""

        def get_report(self, *a, **k):
            return None

    user = _new_user()
    conv_id = ConversationRepository(db).get_or_create(None, user).id
    agent_id = db.execute(text("""
        INSERT INTO agents (key, name, description, agent_type)
        VALUES (:key, :key, 'test agent', 'remote_flow') RETURNING id
    """), {"key": f"mitra_{uuid.uuid4().hex[:8]}"}).fetchone()[0]

    from app.domain.agent_spec import RemoteFlowAgentSpec
    agent = _Agent(
        id=agent_id,
        spec=RemoteFlowAgentSpec(
            key="record_stories", name="Record Stories", description="test",
            agent_type="remote_flow", routing=RoutingSpec(pin_session=True), remote=remote,
        ),
    )
    session_view = _session_awaiting_user(db, conv_id, agent_id)

    _finalizer(db, rest_for=lambda a: _Rest()).finalize_claiming(session_view, agent, user)

    assert reached == ["finalize"], "the artifact path must still call finalize()"
