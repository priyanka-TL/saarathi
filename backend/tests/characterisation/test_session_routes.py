"""GET/POST /api/sessions/{id} routes (design doc §10.2).

Real Flask app + real Postgres (matching this directory's established
convention); only the Mitra REST client and channel pool are faked, and the
agent registry lookup is stubbed so a session can be seeded directly via
repositories without needing a full agent_configurations row synced in.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import text

from app.agents.protocol import SessionDelta, SessionState
from app.database.engine import SessionLocal
from app.domain.agent_spec import RemoteFlowAgentSpec, RemoteSpec, RoutingSpec
from app.domain.core import UserContext
from app.repositories.conversations import ConversationRepository
from app.repositories.sessions import AgentSessionRepository
from app.services.session_service import SessionService
from tests.characterisation.conftest import chat


class _FakeMitraRest:
    def __init__(self):
        self.finalize_calls = []
        self.get_report_calls = []
        self.story_id = "9931"
        self.report_url = None
        # Per-instance, not class attributes: a shared mutable default would
        # leak recorded calls between tests.
        self.chat_rows = []
        self.recent_chat_calls = []

    def finalize(
        self, session_id, profile_id, flow, language, token,
        path="/api/end-story/v2/", as_guest=False,
    ):
        self.finalize_calls.append((session_id, profile_id, flow, language, token))
        return self.story_id, "narrative content"

    def get_report(self, session_id, media_type="application/pdf"):
        self.get_report_calls.append((session_id, media_type))
        return self.report_url

    # -- lost-turn recovery -------------------------------------------------
    # chat_rows is what Mitra's CompanyChat "already contains"; recording the
    # calls is how the tests assert that recovery is READ-ONLY.
    def recent_chat(self, session_id, profile_id, tail=10):
        self.recent_chat_calls.append((session_id, profile_id, tail))
        return list(self.chat_rows)

    def is_session_completed(self, session_id):
        return False


class _FakeMitraSessions:
    def __init__(self):
        self.close_calls = []

    def close(self, conversation_id):
        self.close_calls.append(conversation_id)


@dataclass
class _RegisteredAgentStub:
    id: str
    key: str
    spec: RemoteFlowAgentSpec


def _stub_agent(agent_id: uuid.UUID) -> _RegisteredAgentStub:
    spec = RemoteFlowAgentSpec(
        key="record_stories", name="Record Stories", description="test",
        agent_type="remote_flow",
        routing=RoutingSpec(pin_session=True, exit_keywords=["/exit"]),
        remote=RemoteSpec(
            provider="mitra", flow_name="guest-mi-story",
            bot_route_env="TEST_BOT_ROUTE", company_env="TEST_COMPANY",
            report_media_type="application/pdf",
        ),
    )
    return _RegisteredAgentStub(id=str(agent_id), key="record_stories", spec=spec)


@pytest.fixture()
def fake_mitra(flask_app, monkeypatch):
    """Container is a frozen dataclass -- object.__setattr__ bypasses that to
    swap in fakes for the duration of one test, restored afterward."""
    container = flask_app.state.container
    orig_rest, orig_sessions = container.mitra_rest, container.mitra_sessions
    rest, sessions = _FakeMitraRest(), _FakeMitraSessions()
    object.__setattr__(container, "mitra_rest", rest)
    object.__setattr__(container, "mitra_sessions", sessions)
    yield rest, sessions
    object.__setattr__(container, "mitra_rest", orig_rest)
    object.__setattr__(container, "mitra_sessions", orig_sessions)


@pytest.fixture()
def stub_registry(flask_app, monkeypatch):
    """Session routes resolve the owning agent via
    container.agent_registry.get_by_id -- stub it so a session can be seeded
    directly via repositories without a synced agent_configurations row."""
    container = flask_app.state.container

    def _patch(agent_id: uuid.UUID):
        agent = _stub_agent(agent_id)
        monkeypatch.setattr(container.agent_registry, "get_by_id", lambda aid: agent)
        return agent

    return _patch


def _insert_agent_row(db_session) -> uuid.UUID:
    row = db_session.execute(text("""
        INSERT INTO agents (key, name, description, agent_type, status)
        VALUES (:key, :key, 'test remote agent', 'remote_flow', 'enabled') RETURNING id
    """), {"key": f"test_remote_{uuid.uuid4().hex[:8]}"}).fetchone()
    return row[0]


def _own_conversation_id(client, script) -> uuid.UUID:
    """The caller's own conversation, created through a real /api/chat turn
    so it's scoped to whatever identity the test client authenticates as."""
    script.queue("hi there")
    _, body = chat(client, "hello", "General Support Agent")
    return uuid.UUID(body["conversation_id"])


def _seed_awaiting_session(conversation_id: uuid.UUID, agent_id: uuid.UUID):
    """Drives a real agent_sessions row to 'awaiting_user', own commit so the
    next request (a fresh db_session) sees it."""
    db = SessionLocal()
    try:
        ConversationRepository(db).pin(conversation_id, agent_id)
        svc = SessionService(db)
        repo = AgentSessionRepository(db)
        pending = repo.create_pending(conversation_id, agent_id)
        authing = svc.apply(pending, SessionDelta(
            state=SessionState.authenticating,
            remote_session_id=f"remote-{uuid.uuid4().hex[:8]}",
            remote_profile_id="profile-1",
        ))
        in_progress = svc.apply(authing, SessionDelta(state=SessionState.in_progress))
        awaiting = svc.apply(in_progress, SessionDelta(state=SessionState.awaiting_user, step=3))
        db.commit()
        return awaiting
    finally:
        db.close()


def _seed_completed_session(conversation_id: uuid.UUID, agent_id: uuid.UUID, report_url=None):
    db = SessionLocal()
    try:
        ConversationRepository(db).pin(conversation_id, agent_id)
        svc = SessionService(db)
        repo = AgentSessionRepository(db)
        pending = repo.create_pending(conversation_id, agent_id)
        authing = svc.apply(pending, SessionDelta(
            state=SessionState.authenticating,
            remote_session_id=f"remote-{uuid.uuid4().hex[:8]}",
            remote_profile_id="profile-1",
        ))
        in_progress = svc.apply(authing, SessionDelta(state=SessionState.in_progress))
        claimed = svc.claim_finalizing(in_progress.id)
        fields = {"result_ref": "story-1"}
        if report_url:
            fields["report_url"] = report_url
        completed = svc.apply(claimed, SessionDelta(state=SessionState.completed, **fields))
        db.commit()
        return completed
    finally:
        db.close()


def _other_tenant_conversation_id() -> uuid.UUID:
    db = SessionLocal()
    try:
        other_user = UserContext(
            user_id="someone_else", email="someone_else@example.com",
            display_name="Someone Else", tenant_code="OTHER_TENANT",
        )
        conv = ConversationRepository(db).get_or_create(None, other_user)
        db.commit()
        return conv.id
    finally:
        db.close()


# ---------------------------------------------------------------------------
# GET /api/sessions/{id}
# ---------------------------------------------------------------------------


def test_get_session_returns_scoped_state(client, stub_registry, script):
    conv_id = _own_conversation_id(client, script)
    db = SessionLocal()
    agent_id = _insert_agent_row(db)
    db.commit()
    db.close()
    stub_registry(agent_id)
    seeded = _seed_awaiting_session(conv_id, agent_id)

    response = client.get(f"/api/sessions/{seeded.id}")
    body = response.json()

    assert response.status_code == 200
    assert body["id"] == str(seeded.id)
    assert body["state"] == "awaiting_user"
    assert body["step"] == 3
    assert body["agent_key"] == "record_stories"
    assert body["result_ref"] is None
    assert body["report_url"] is None


def test_get_session_404_for_unknown_id(client):
    response = client.get(f"/api/sessions/{uuid.uuid4()}")
    body = response.json()

    assert response.status_code == 404
    assert body["error_code"] == "SESSION_NOT_FOUND"


def test_get_session_404_for_wrong_tenant(client, stub_registry):
    other_conv_id = _other_tenant_conversation_id()
    db = SessionLocal()
    agent_id = _insert_agent_row(db)
    db.commit()
    db.close()
    stub_registry(agent_id)
    seeded = _seed_awaiting_session(other_conv_id, agent_id)

    response = client.get(f"/api/sessions/{seeded.id}")

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/sessions/{id}/finalize
# ---------------------------------------------------------------------------


def test_finalize_is_idempotent_and_returns_cached_result(client, stub_registry, fake_mitra, script):
    rest, _sessions = fake_mitra
    conv_id = _own_conversation_id(client, script)
    db = SessionLocal()
    agent_id = _insert_agent_row(db)
    db.commit()
    db.close()
    stub_registry(agent_id)
    seeded = _seed_awaiting_session(conv_id, agent_id)

    first = client.post(f"/api/sessions/{seeded.id}/finalize")
    first_body = first.json()

    assert first.status_code == 200
    assert first_body["state"] == "completed"
    assert first_body["result_ref"] == "9931"
    assert len(rest.finalize_calls) == 1

    second = client.post(f"/api/sessions/{seeded.id}/finalize")
    second_body = second.json()

    assert second.status_code == 200
    assert second_body["state"] == "completed"
    assert second_body["result_ref"] == "9931"
    assert len(rest.finalize_calls) == 1, "a repeated request must not re-invoke Mitra"


def test_finalize_404_for_unknown_id(client):
    response = client.post(f"/api/sessions/{uuid.uuid4()}/finalize")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/sessions/{id}/abandon
# ---------------------------------------------------------------------------


def test_abandon_unpins_and_closes_channel(client, stub_registry, fake_mitra, script):
    _rest, sessions = fake_mitra
    conv_id = _own_conversation_id(client, script)
    db = SessionLocal()
    agent_id = _insert_agent_row(db)
    db.commit()
    db.close()
    stub_registry(agent_id)
    seeded = _seed_awaiting_session(conv_id, agent_id)

    response = client.post(f"/api/sessions/{seeded.id}/abandon")
    body = response.json()

    assert response.status_code == 200
    assert body["state"] == "abandoned"
    assert sessions.close_calls == [conv_id]

    verify = SessionLocal()
    try:
        pinned = verify.execute(
            text("SELECT pinned_agent_id FROM conversations WHERE id = :id"), {"id": conv_id}
        ).scalar()
        assert pinned is None
    finally:
        verify.close()


def test_abandon_is_idempotent(client, stub_registry, fake_mitra, script):
    _rest, sessions = fake_mitra
    conv_id = _own_conversation_id(client, script)
    db = SessionLocal()
    agent_id = _insert_agent_row(db)
    db.commit()
    db.close()
    stub_registry(agent_id)
    seeded = _seed_awaiting_session(conv_id, agent_id)

    first = client.post(f"/api/sessions/{seeded.id}/abandon")
    second = client.post(f"/api/sessions/{seeded.id}/abandon")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["state"] == "abandoned"
    assert len(sessions.close_calls) == 1, "a repeated abandon must not close the channel twice"


# ---------------------------------------------------------------------------
# GET /api/sessions/{id}/report
# ---------------------------------------------------------------------------


def test_report_202_when_not_ready(client, stub_registry, fake_mitra, script):
    rest, _sessions = fake_mitra
    rest.report_url = None
    conv_id = _own_conversation_id(client, script)
    db = SessionLocal()
    agent_id = _insert_agent_row(db)
    db.commit()
    db.close()
    stub_registry(agent_id)
    seeded = _seed_completed_session(conv_id, agent_id)

    response = client.get(f"/api/sessions/{seeded.id}/report")
    body = response.json()

    assert response.status_code == 202
    assert "retry_after" in body


def test_report_200_with_cached_url(client, stub_registry, fake_mitra, script):
    rest, _sessions = fake_mitra
    conv_id = _own_conversation_id(client, script)
    db = SessionLocal()
    agent_id = _insert_agent_row(db)
    db.commit()
    db.close()
    stub_registry(agent_id)
    seeded = _seed_completed_session(conv_id, agent_id, report_url="https://example.com/cached.pdf")

    response = client.get(f"/api/sessions/{seeded.id}/report")
    body = response.json()

    assert response.status_code == 200
    assert body["report_url"] == "https://example.com/cached.pdf"
    assert body["story_id"] == "story-1"
    assert rest.get_report_calls == [], "a cached report_url must not trigger a live Mitra call"


def test_report_200_with_freshly_fetched_url(client, stub_registry, fake_mitra, script):
    rest, _sessions = fake_mitra
    rest.report_url = "https://example.com/fresh.pdf"
    conv_id = _own_conversation_id(client, script)
    db = SessionLocal()
    agent_id = _insert_agent_row(db)
    db.commit()
    db.close()
    stub_registry(agent_id)
    seeded = _seed_completed_session(conv_id, agent_id)

    response = client.get(f"/api/sessions/{seeded.id}/report")
    body = response.json()

    assert response.status_code == 200
    assert body["report_url"] == "https://example.com/fresh.pdf"
    assert len(rest.get_report_calls) == 1


# ---------------------------------------------------------------------------
# POST /api/sessions/{id}/resume -- the safe replacement for the Retry button.
#
# Retry used to re-POST /api/chat with the same text. Live, Mitra had already
# answered and advanced, so the re-send was recorded as the answer to the NEXT
# question -- the §1.6 answer-destruction hazard. This route is read-only
# against Mitra and only ever reports whether re-sending is safe.
# ---------------------------------------------------------------------------

def _resume_setup(client, stub_registry, fake_mitra, script, rows):
    from app.integrations.mitra.turn_recovery import ChatRow  # noqa: F401

    rest, _sessions = fake_mitra
    conv_id = _own_conversation_id(client, script)
    db = SessionLocal()
    agent_id = _insert_agent_row(db)
    db.commit()
    db.close()
    stub_registry(agent_id)
    session = _seed_awaiting_session(conv_id, agent_id)
    rest.chat_rows = rows
    return conv_id, session, rest


def test_resume_recovers_a_reply_mitra_had_already_sent(client, stub_registry, fake_mitra, script):
    """THE regression test: the reply exists upstream, so hand it back rather
    than re-sending the user's answer into the next question."""
    from app.integrations.mitra.turn_recovery import ChatRow

    conv_id, session, rest = _resume_setup(client, stub_registry, fake_mitra, script, rows=[
        # _own_conversation_id sends "hello" as the last user message.
        ChatRow(id=1, from_user=True, message="hello"),
        ChatRow(id=2, from_user=False, message="What was the main problem you noticed?"),
    ])

    response = client.post(f"/api/sessions/{session.id}/resume")
    body = response.json()

    assert response.status_code == 200
    assert body["outcome"] == "answered"
    assert body["response"] == "What was the main problem you noticed?"
    assert body.get("can_resend") is not True
    assert rest.recent_chat_calls, "recovery must consult Mitra's own record"


def test_resume_persists_the_recovered_reply_into_the_transcript(client, stub_registry, fake_mitra, script):
    """Otherwise the recovered turn vanishes on the next page reload."""
    from app.integrations.mitra.turn_recovery import ChatRow

    conv_id, session, _ = _resume_setup(client, stub_registry, fake_mitra, script, rows=[
        ChatRow(id=1, from_user=True, message="hello"),
        ChatRow(id=2, from_user=False, message="Recovered question?"),
    ])

    client.post(f"/api/sessions/{session.id}/resume")

    messages = client.get(f"/api/conversations/{conv_id}/messages").json()["messages"]
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == "Recovered question?"


def test_resume_reports_pending_while_mitra_is_still_generating(client, stub_registry, fake_mitra, script):
    """202, so the client polls. It must NOT be told re-sending is safe."""
    from app.integrations.mitra.turn_recovery import ChatRow

    _conv, session, _ = _resume_setup(client, stub_registry, fake_mitra, script, rows=[
        ChatRow(id=1, from_user=True, message="hello"),
    ])

    response = client.post(f"/api/sessions/{session.id}/resume")
    body = response.json()

    assert response.status_code == 202
    assert body["outcome"] == "pending"
    assert body.get("can_resend") is not True
    assert body["retry_after"] >= 1


def test_resume_allows_a_resend_only_when_mitra_never_got_the_message(client, stub_registry, fake_mitra, script):
    """The one safe case -- and the one the old button assumed always held."""
    from app.integrations.mitra.turn_recovery import ChatRow

    _conv, session, _ = _resume_setup(client, stub_registry, fake_mitra, script, rows=[
        ChatRow(id=1, from_user=True, message="a completely different earlier answer"),
        ChatRow(id=2, from_user=False, message="an earlier question"),
    ])

    response = client.post(f"/api/sessions/{session.id}/resume")
    body = response.json()

    assert response.status_code == 200
    assert body["outcome"] == "not_delivered"
    assert body["can_resend"] is True


def test_resume_404s_for_another_tenants_session(client, stub_registry, fake_mitra, script):
    """Same scoping as every other session route -- a recovered reply is
    conversation content and must not leak across tenants."""
    from app.integrations.mitra.turn_recovery import ChatRow

    rest, _ = fake_mitra
    other_conv = _other_tenant_conversation_id()
    db = SessionLocal()
    agent_id = _insert_agent_row(db)
    db.commit()
    db.close()
    stub_registry(agent_id)
    session = _seed_awaiting_session(other_conv, agent_id)
    rest.chat_rows = [ChatRow(id=1, from_user=False, message="secret question")]

    response = client.post(f"/api/sessions/{session.id}/resume")

    assert response.status_code == 404
    assert "secret question" not in response.text


def test_resume_404s_for_an_unknown_session(client):
    assert client.post(f"/api/sessions/{uuid.uuid4()}/resume").status_code == 404
