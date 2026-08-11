"""Item 6 -- POST /api/reset.

Module 2.6 replaces the global reset with conversation archival plus a successor
conversation, and module 5.7 additionally makes it abandon any open session and
close its channel. The response shape must survive both.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.agents.protocol import SessionDelta, SessionState
from app.database.engine import SessionLocal
from app.repositories.conversations import ConversationRepository
from app.repositories.sessions import AgentSessionRepository
from app.services.session_service import SessionService
from tests.characterisation.conftest import DEFAULT_AGENT, chat


class _FakeProviders:
    """Stands in for the provider registry.

    ONE fake where there were two. Closing a conversation's transport is
    provider-agnostic now -- the registry sweeps every pool -- so a test no
    longer has to know which platform's pool to swap.
    """

    def __init__(self):
        self.close_calls = []
        self.enabled = frozenset({"mitra", "saathi"})

    def close_conversation(self, conversation_id):
        self.close_calls.append(conversation_id)


@pytest.fixture()
def fake_providers(flask_app):
    """Container is a frozen dataclass -- object.__setattr__ bypasses that to
    swap in a fake for the duration of one test, restored afterward."""
    container = flask_app.state.container
    orig = container.providers
    fake = _FakeProviders()
    object.__setattr__(container, "providers", fake)
    yield fake
    object.__setattr__(container, "providers", orig)


def _insert_agent_row(db_session) -> uuid.UUID:
    row = db_session.execute(text("""
        INSERT INTO agents (key, name, description, agent_type, status)
        VALUES (:key, :key, 'test remote agent', 'remote_flow', 'enabled') RETURNING id
    """), {"key": f"test_remote_{uuid.uuid4().hex[:8]}"}).fetchone()
    return row[0]


def _seed_awaiting_session(conversation_id: uuid.UUID, agent_id: uuid.UUID):
    db = SessionLocal()
    try:
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


def test_reset_mid_interview_abandons_session_and_closes_channel(client, script, fake_providers):
    script.queue("hi there")
    _, body = chat(client, "hello", DEFAULT_AGENT)
    conv_id = uuid.UUID(body["conversation_id"])

    db = SessionLocal()
    agent_id = _insert_agent_row(db)
    db.commit()
    db.close()
    seeded = _seed_awaiting_session(conv_id, agent_id)

    response = client.post("/api/reset", json={"conversation_id": str(conv_id)})
    assert response.status_code == 200

    verify = SessionLocal()
    try:
        state = verify.execute(
            text("SELECT state FROM agent_sessions WHERE id = :id"), {"id": seeded.id}
        ).scalar()
        # No pinned_agent_id column: a terminal session IS the release.
        open_count = verify.execute(
            text("SELECT count(*) FROM agent_sessions WHERE conversation_id = :id "
                 "AND state NOT IN ('completed', 'failed', 'abandoned')"),
            {"id": conv_id},
        ).scalar()
    finally:
        verify.close()

    assert state == "abandoned"
    assert open_count == 0
    assert fake_providers.close_calls == [conv_id]


def test_reset_returns_success(client):
    """app.py:109-113, plus the conversation_id added when reset stopped
    archiving the previous conversation.

    The id is not decorative: the client must adopt it. Reset used to archive
    the old conversation so a null conversation_id could only resolve to a new
    one -- but archiving is what hid finished chats from the recent list, so
    the previous conversation now stays active and a null id would resume it."""
    response = client.post("/api/reset")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    uuid.UUID(body["conversation_id"])   # present and a real uuid


def test_reset_accepts_an_empty_body(client):
    """main.js posts no body at all. Keep that valid."""
    response = client.post("/api/reset")

    assert response.status_code == 200





def test_reset_clears_the_flow_title(client, script):
    """After a reset the NEXT message becomes the new title."""
    script.queue("first")
    _, first = chat(client, "the original question", DEFAULT_AGENT)
    assert first["flow"]["title"] == "the original question"

    client.post("/api/reset")

    script.queue("second")
    _, second = chat(client, "a brand new question", DEFAULT_AGENT)
    assert second["flow"]["title"] == "a brand new question"


def test_reset_clears_the_breadcrumb_stops(client, script, second_llm_agent):
    second_name, _key = second_llm_agent
    script.queue("a")
    chat(client, "one", DEFAULT_AGENT)
    script.queue("b")
    _, before = chat(client, "two", second_name)
    assert len(before["flow"]["stops"]) == 2

    client.post("/api/reset")

    script.queue("c")
    _, after = chat(client, "three", second_name)
    assert after["flow"]["stops"] == [second_name]
    assert after["flow"]["current_index"] == 0


def test_reset_drops_history_from_subsequent_llm_calls(client, script):
    """The cleared history must actually stop being sent to the model."""
    script.queue("first")
    chat(client, "remember this", DEFAULT_AGENT)

    client.post("/api/reset")

    script.queue("second")
    chat(client, "fresh start", DEFAULT_AGENT)

    second_call = script.calls[1]
    assert not any("remember this" in str(m.content) for m in second_call)


def test_repeated_reset_reuses_the_same_empty_conversation(client):
    """Pressing "New chat" repeatedly must not leave a dead row behind each time.

    This used to assert the opposite -- that every reset hands back a DISTINCT
    conversation -- and that is precisely what filled the database with empty
    shells: 47 of 68 active conversations on the dev database had never held a
    message. They are invisible (list_for_user filters message_count > 0) but
    they accumulate without bound.

    An empty conversation is already a clean slate, so there is nothing to
    reset: handing the same one back is the correct answer, not a compromise.
    """
    seen = []
    for _ in range(3):
        response = client.post("/api/reset")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "success"
        seen.append(body["conversation_id"])

    assert len(set(seen)) == 1, (
        "consecutive resets with nothing typed in between must reuse the same "
        f"empty conversation, got {len(set(seen))} distinct ids"
    )


def test_reset_after_a_real_turn_starts_a_new_conversation(client, script):
    """The other half: once a conversation has been used, reset must leave it in
    history and move on -- otherwise "New chat" would dump the user back into
    the conversation they just finished."""
    from tests.characterisation.conftest import DEFAULT_AGENT, chat

    script.queue("ok")
    _, body = chat(client, "something I actually said", DEFAULT_AGENT)
    used_conv_id = body["conversation_id"]

    fresh = client.post("/api/reset").json()["conversation_id"]
    assert fresh != used_conv_id

    # And the used conversation is still listed, not archived away.
    listed = client.get("/api/conversations?limit=20").json()["conversations"]
    assert used_conv_id in [c["id"] for c in listed]


def test_reset_rejects_a_malformed_conversation_id(client):
    """This route has no exception handler of its own, so uuid.UUID()'s
    ValueError escaped as a bare 500."""
    response = client.post("/api/reset", json={"conversation_id": "not-a-uuid"})
    assert response.status_code == 400
    assert response.json()["error_code"] == "INVALID_REQUEST"
