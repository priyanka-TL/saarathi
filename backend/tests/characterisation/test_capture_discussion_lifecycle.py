"""Capture Discussion lifecycle regressions found during the QA audit.

Everything here is a defect that shipped and was reproduced against the running
application or the live database, not a hypothetical. Each test names the
symptom a user would have seen.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.database.engine import SessionLocal, engine
from tests.characterisation.conftest import DEFAULT_AGENT, chat
from tests.characterisation.test_conversations_endpoint import (
    _general_support_agent,
    _new_user,
    _seed_conversation,
    _seed_message,
    _seed_session,
    as_user,  # noqa: F401  -- fixture, imported for pytest to collect
)


def _conversation_title(conv_id) -> str | None:
    db = SessionLocal()
    try:
        return db.execute(
            text("SELECT title FROM conversations WHERE id = :id"), {"id": conv_id},
        ).scalar()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# H6 -- every discussion in the sidebar had the same title
# ---------------------------------------------------------------------------


def test_autostart_message_does_not_become_the_conversation_title(client, script):
    """The capability buttons post a canned opener ("I want to capture a
    discussion") on the user's behalf, and the title is the first user message,
    so EVERY discussion in the sidebar was titled identically. Live count on the
    dev database: 5 conversations named "I want to capture a discussion" and 3
    named "I want to record a story", indistinguishable from each other.
    """
    script.queue("Nice to meet you! Which village was this?")
    _, body = chat(client, "I want to capture a discussion", DEFAULT_AGENT, autostart=True)
    conv_id = body["conversation_id"]

    title = _conversation_title(conv_id)
    assert title != "I want to capture a discussion", (
        "the UI's canned opener must not name the conversation"
    )
    assert title, "a conversation opened by autostart still needs a title"


def test_the_first_real_user_message_titles_the_conversation(client, script):
    """The user's own words win, which is the whole point of skipping the
    autostart text rather than just suppressing titling altogether."""
    script.queue("Nice to meet you!")
    _, body = chat(client, "I want to capture a discussion", DEFAULT_AGENT, autostart=True)
    conv_id = body["conversation_id"]

    script.queue("Thank you.")
    chat(client, "Rampur village, Channapatna block", DEFAULT_AGENT, conversation_id=conv_id)

    assert _conversation_title(conv_id) == "Rampur village, Channapatna block"


def test_a_normal_message_still_titles_the_conversation(client, script):
    """Regression guard: only autostart is exempt."""
    script.queue("ok")
    _, body = chat(client, "what is a learning circle", DEFAULT_AGENT)
    assert _conversation_title(body["conversation_id"]) == "what is a learning circle"


# ---------------------------------------------------------------------------
# H1 -- a completed session whose PDF is still generating
# ---------------------------------------------------------------------------


def test_completed_session_without_a_report_url_is_still_returned(client, as_user):
    """THE regression for "I finished the interview and refreshed, and there was
    nothing there".

    OrchestrationService._finalize deliberately completes with report_url = NULL
    when Mitra's PDF generation lags -- its own comment says the field "is
    designed to be null here anyway". The client only rebuilt completion UI for
    sessions that already had a report_url, so refreshing in that gap showed no
    completion notice, no download button, no polling and no error, while
    GET /api/sessions/{id}/report would have served the report moments later.

    The API half of the contract: the session must be in the payload with its
    state, so the client can poll for the report.
    """
    user = _new_user()
    as_user(user)
    conv_id = _seed_conversation(user, "a discussion", datetime.now(timezone.utc))
    agent_id, _ = _general_support_agent()

    sess_id = _seed_session(
        conv_id, agent_id, "completed", step=10, result_ref="4124", report_url=None,
    )

    body = client.get(f"/api/conversations/{conv_id}/messages").json()
    by_id = {s["id"]: s for s in body["sessions"]}

    assert str(sess_id) in by_id
    assert by_id[str(sess_id)]["state"] == "completed"
    assert by_id[str(sess_id)]["report_url"] is None
    assert by_id[str(sess_id)]["result_ref"] == "4124", (
        "result_ref proves the interview really finished, so the client knows "
        "to poll rather than to conclude it failed"
    )


def test_sessions_payload_carries_agent_key_for_completion_copy(client, as_user):
    """The client picks "Your discussion report is ready" vs "Your story is
    ready" from agent_key. It used to branch on the display name string
    'Capture Discussions', and this agent has already been renamed once."""
    user = _new_user()
    as_user(user)
    conv_id = _seed_conversation(user, "a discussion", datetime.now(timezone.utc))
    agent_id, _ = _general_support_agent()
    _seed_session(conv_id, agent_id, "completed", result_ref="4124")

    body = client.get(f"/api/conversations/{conv_id}/messages").json()
    assert body["sessions"][0]["agent_key"] == "general_support"


# ---------------------------------------------------------------------------
# H3 -- double submission
# ---------------------------------------------------------------------------


def test_a_second_turn_on_a_busy_conversation_is_refused_not_duplicated(client, script):
    """THE double-submit regression.

    handle_turn took a row lock at step 2 but COMMITTED at step 9, before
    calling the handler -- so two concurrent posts to one conversation both got
    through and both called Mitra. On a first turn that is two
    upsert_profile + generate_session pairs and an orphaned remote session; on a
    later turn it is §1.6 answer destruction, because two user messages in a row
    merge into one in Mitra's database and the first answer is lost.

    The lock is claimed BEFORE the user message is written, so a refused turn
    must leave no trace at all.
    """
    script.queue("first reply")
    _, body = chat(client, "opening message", DEFAULT_AGENT)
    conv_id = body["conversation_id"]

    # Hold the conversation's turn lock on a DEDICATED connection, exactly as an
    # in-flight sibling request would. It must not go back to the pool: advisory
    # locks are per-connection AND re-entrant within one, so a pooled connection
    # handed to the request under test would simply re-acquire its own lock.
    conn = engine.connect()
    try:
        conn.execute(
            text("SELECT pg_advisory_lock(hashtext(:key))"),
            {"key": f"saarthi:turn:{conv_id}"},
        )

        status, refused = chat(client, "the same answer again", DEFAULT_AGENT,
                               conversation_id=conv_id)

        assert status == 409
        assert refused["error_code"] == "CONCURRENT_TURN"
    finally:
        conn.execute(
            text("SELECT pg_advisory_unlock(hashtext(:key))"),
            {"key": f"saarthi:turn:{conv_id}"},
        )
        conn.close()

    # The refused turn wrote nothing.
    db = SessionLocal()
    try:
        contents = [
            r.content for r in db.execute(
                text("SELECT content FROM conversation_messages "
                     "WHERE conversation_id = :cid ORDER BY seq"),
                {"cid": conv_id},
            ).fetchall()
        ]
    finally:
        db.close()
    assert "the same answer again" not in contents, (
        "a refused turn must not leave a user message behind"
    )


def test_the_turn_lock_is_released_so_the_next_turn_succeeds(client, script):
    """A session-scoped advisory lock is NOT released by commit or by the
    connection returning to the pool, so a missing unlock would wedge the
    conversation permanently after its very first turn."""
    script.queue("one")
    _, body = chat(client, "first", DEFAULT_AGENT)
    conv_id = body["conversation_id"]

    script.queue("two")
    status, second = chat(client, "second", DEFAULT_AGENT, conversation_id=conv_id)
    assert status == 200, "the turn lock leaked from the previous request"
    assert second["status"] == "success"


# ---------------------------------------------------------------------------
# M4 -- limits are enforced, not decorative
# ---------------------------------------------------------------------------


def test_max_turns_is_enforced(client, script, as_user, monkeypatch):
    """`limits:` was validated, checksummed into agent_configurations, and then
    consulted by nobody -- RateLimits.check was a no-op stub named
    RateLimitsDummy. An interview had no turn ceiling at all.
    """
    from app.services.orchestration import RateLimits, TurnLimitExceeded

    user = _new_user()
    as_user(user)
    conv_id = _seed_conversation(user, "an interview", datetime.now(timezone.utc))
    agent_id, _ = _general_support_agent()
    sess_id = _seed_session(conv_id, agent_id, "awaiting_user", step=60)
    # _seed_session does not expose turn_count, and turn_count is what the
    # ceiling is measured against.
    db = SessionLocal()
    try:
        db.execute(
            text("UPDATE agent_sessions SET turn_count = 60 WHERE id = :id"),
            {"id": sess_id},
        )
        db.commit()
    finally:
        db.close()

    class _Limits:
        max_turns = 60
        rate_limit_per_conversation_per_min = None

    db = SessionLocal()
    try:
        from app.repositories.sessions import AgentSessionRepository
        limits = RateLimits(db, AgentSessionRepository(db))
        with pytest.raises(TurnLimitExceeded) as exc:
            limits.check(conv_id, user, _Limits())
        assert exc.value.reason == "max_turns"
    finally:
        db.close()


def test_limits_check_is_a_no_op_when_none_are_configured():
    """An agent with no `limits:` block must not be throttled by accident."""
    from app.services.orchestration import RateLimits

    db = SessionLocal()
    try:
        from app.repositories.sessions import AgentSessionRepository
        RateLimits(db, AgentSessionRepository(db)).check(uuid.uuid4(), None, None)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# L4 -- malformed ids
# ---------------------------------------------------------------------------


def test_chat_rejects_a_malformed_conversation_id_as_a_client_error(client):
    response = client.post("/api/chat", json={
        "message": "hello", "conversation_id": "not-a-uuid",
    })
    assert response.status_code == 400, "uuid.UUID()'s ValueError escaped as a 500"
    assert response.json()["error_code"] == "INVALID_REQUEST"


def test_chat_with_an_unknown_conversation_id_does_not_adopt_it(client, script):
    """A stale sessionStorage.saarthi_cid used to be resurrected as the primary
    key of a brand-new conversation, making the id a client-controlled value."""
    dead_id = str(uuid.uuid4())
    script.queue("ok")
    _, body = chat(client, "hello", DEFAULT_AGENT, conversation_id=dead_id)

    assert body["conversation_id"] != dead_id

    db = SessionLocal()
    try:
        exists = db.execute(
            text("SELECT 1 FROM conversations WHERE id = :id"), {"id": dead_id},
        ).fetchone()
    finally:
        db.close()
    assert exists is None, "the caller's id was materialised as a real conversation"
