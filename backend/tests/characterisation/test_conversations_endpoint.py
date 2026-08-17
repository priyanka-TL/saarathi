"""GET /api/conversations and GET /api/conversations/<id>/messages -- power
the sidebar's recent-conversations list and resuming one into the chat pane.

Real Flask app + real Postgres, matching this directory's established
convention (see test_session_routes.py). Identity is controlled by
monkeypatching Authenticator.authenticate directly (same pattern as
tests/integration/test_config_versioning_lifecycle.py's admin_client
fixture) rather than minting a real, ELEVATE_JWT_SECRET-verified bearer token,
so tests don't depend on a real LLM call just to resolve "who am I."
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.database.engine import SessionLocal
from app.domain.core import UserContext


def _seed_conversation(user: UserContext, title, last_message_at, message_count=1) -> uuid.UUID:
    db = SessionLocal()
    try:
        conv_id = uuid.uuid4()
        db.execute(text("""
            INSERT INTO conversations
                (id, tenant_code, user_id, title, status, locale, message_count, metadata, last_message_at)
            VALUES
                (:id, :tenant_code, :user_id, :title, 'active', 'en', :message_count, '{}', :last_message_at)
        """), {
            "id": conv_id, "tenant_code": user.tenant_code, "user_id": user.user_id,
            "title": title, "message_count": message_count, "last_message_at": last_message_at,
        })
        db.commit()
        return conv_id
    finally:
        db.close()


def _general_support_agent() -> tuple[uuid.UUID, str]:
    """Reuses the real, permanent general_support agent as the FK target for
    a seeded assistant message -- deliberately NOT a throwaway inserted row.
    A fixed-name throwaway (`agents.name` is unique) collides with itself on
    a second test run and, if left uncleaned, accumulates cruft exactly like
    an earlier, unrelated test-hygiene bug in test_admin_routes.py already
    did once this session. Reusing a real agent sidesteps both problems."""
    db = SessionLocal()
    try:
        row = db.execute(text("SELECT id, name FROM agents WHERE key = 'general_support'")).fetchone()
        assert row is not None, "general_support agent must exist (src/config/agents/general_support.yaml)"
        return row.id, row.name
    finally:
        db.close()


def _seed_message(
    conversation_id: uuid.UUID, seq: int, role: str, content: str,
    agent_id=None, options=None, selected_option_id=None, agent_session_id=None,
    attachments=None,
) -> uuid.UUID:
    db = SessionLocal()
    try:
        msg_id = uuid.uuid4()
        db.execute(text("""
            INSERT INTO conversation_messages
                (id, conversation_id, seq, role, content, agent_id, options, selected_option_id,
                 agent_session_id, attachments)
            VALUES
                (:id, :conversation_id, :seq, :role, :content, :agent_id, :options, :selected_option_id,
                 :agent_session_id, :attachments)
        """), {
            "id": msg_id, "conversation_id": conversation_id, "seq": seq, "role": role, "content": content,
            "agent_id": agent_id, "options": json.dumps(options) if options is not None else None,
            "selected_option_id": selected_option_id, "agent_session_id": agent_session_id,
            "attachments": json.dumps(attachments) if attachments is not None else None,
        })
        db.commit()
        return msg_id
    finally:
        db.close()


@pytest.fixture()
def as_user(monkeypatch):
    """Authenticate the test client as a fresh, controlled identity.

    Patches `Authenticator.authenticate()` directly -- simpler than minting a
    real, ELEVATE_JWT_SECRET-verified bearer token per identity. The lambda
    accepts and ignores a `token` argument, since `get_current_user` now always
    passes one (None when the client sends no Authorization header).
    """
    def _patch(user: UserContext):
        monkeypatch.setattr(
            "app.services.identity.Authenticator.authenticate",
            lambda self, token=None: user,
        )
        return user
    return _patch


def _new_user(**overrides) -> UserContext:
    defaults = dict(
        user_id=f"user_{uuid.uuid4().hex[:8]}",
        email="test@example.com",
        display_name="Test User",
        tenant_code=f"TENANT_{uuid.uuid4().hex[:8]}",
    )
    defaults.update(overrides)
    return UserContext(**defaults)


def test_returns_own_conversations_newest_first_capped_at_limit(client, as_user):
    user = _new_user()
    as_user(user)

    now = datetime.now(timezone.utc)
    ids_oldest_to_newest = [
        _seed_conversation(user, f"conv {i}", now - timedelta(minutes=(6 - i)))
        for i in range(6)
    ]

    response = client.get("/api/conversations?limit=5")
    body = response.json()

    assert response.status_code == 200
    returned_ids = [c["id"] for c in body["conversations"]]
    assert len(returned_ids) == 5
    # newest first -> the 5 most recently seeded, in reverse seed order
    expected = [str(i) for i in reversed(ids_oldest_to_newest[1:])]
    assert returned_ids == expected


def test_title_falls_back_when_null(client, as_user):
    user = _new_user()
    as_user(user)
    _seed_conversation(user, None, datetime.now(timezone.utc))

    body = client.get("/api/conversations").json()

    assert body["conversations"][0]["title"] == "New conversation"


def test_does_not_leak_another_users_conversations(client, as_user):
    owner = _new_user()
    other = _new_user()
    _seed_conversation(owner, "owner's conversation", datetime.now(timezone.utc))

    as_user(other)
    body = client.get("/api/conversations").json()

    assert body["conversations"] == []


def test_archived_conversations_are_excluded(client, as_user):
    user = _new_user()
    as_user(user)
    conv_id = _seed_conversation(user, "will be archived", datetime.now(timezone.utc))

    db = SessionLocal()
    try:
        db.execute(text("UPDATE conversations SET status = 'archived' WHERE id = :id"), {"id": conv_id})
        db.commit()
    finally:
        db.close()

    body = client.get("/api/conversations").json()

    assert body["conversations"] == []


# ---------------------------------------------------------------------------
# GET /api/conversations/<id>/messages
# ---------------------------------------------------------------------------


def test_messages_returned_in_order_with_agent_name_and_options(client, as_user):
    user = _new_user()
    as_user(user)
    conv_id = _seed_conversation(user, "hello there", datetime.now(timezone.utc))
    agent_id, agent_name = _general_support_agent()

    _seed_message(conv_id, 1, "user", "hello there")
    _seed_message(
        conv_id, 2, "assistant", "Pick one",
        agent_id=agent_id,
        options=[{"id": "opt_1", "label": "English", "value": "en"},
                 {"id": "opt_2", "label": "Hindi", "value": "hi"}],
        selected_option_id="opt_2",
    )
    _seed_message(conv_id, 3, "user", "Hindi please")

    response = client.get(f"/api/conversations/{conv_id}/messages")
    body = response.json()

    assert response.status_code == 200
    assert body["conversation_id"] == str(conv_id)
    msgs = body["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert [m["content"] for m in msgs] == ["hello there", "Pick one", "Hindi please"]

    assistant_msg = msgs[1]
    assert assistant_msg["agent_name"] == agent_name
    assert assistant_msg["options"] == [
        {"id": "opt_1", "label": "English", "value": "en"},
        {"id": "opt_2", "label": "Hindi", "value": "hi"},
    ]
    assert assistant_msg["selected_option_id"] == "opt_2"

    user_msg = msgs[0]
    assert user_msg["agent_name"] is None
    assert user_msg["options"] is None


def test_messages_404_for_unknown_conversation(client, as_user):
    as_user(_new_user())

    response = client.get(f"/api/conversations/{uuid.uuid4()}/messages")

    assert response.status_code == 404


def test_messages_404_for_another_users_conversation(client, as_user):
    owner = _new_user()
    conv_id = _seed_conversation(owner, "owner's conversation", datetime.now(timezone.utc))
    _seed_message(conv_id, 1, "user", "secret")

    as_user(_new_user())
    response = client.get(f"/api/conversations/{conv_id}/messages")

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Session replay on resume.
#
# The "story is ready + download report" notice is generated client-side from
# the /api/chat response; it is NOT a stored message. Replaying only the
# transcript therefore lost the report link on every page reload. The messages
# endpoint returns the conversation's latest session so the client can rebuild
# that state from server truth.
# ---------------------------------------------------------------------------

def _seed_session(conversation_id: uuid.UUID, agent_id, state: str, **fields) -> uuid.UUID:
    db = SessionLocal()
    try:
        sess_id = uuid.uuid4()
        terminal = state in ("completed", "failed", "abandoned")
        db.execute(text("""
            INSERT INTO agent_sessions
                (id, conversation_id, agent_id, state, step, result_ref, report_url,
                 remote_session_id, remote_profile_id,
                 created_at, finalized_at, ended_at)
            VALUES
                (:id, :conversation_id, :agent_id, :state, :step, :result_ref, :report_url,
                 :remote_session_id, :remote_profile_id,
                 :started_at, :finalized_at, :ended_at)
        """), {
            # ck sess_active_has_remote: any non-pending/failed/abandoned state
            # must carry remote_session_id. ck sess_completed_has_result: a
            # completed session must carry result_ref.
            "remote_session_id": f"remote-{uuid.uuid4().hex[:8]}",
            "remote_profile_id": "380",
            "id": sess_id,
            "conversation_id": conversation_id,
            "agent_id": agent_id,
            "state": state,
            "step": fields.get("step", 0),
            "result_ref": fields.get("result_ref"),
            "report_url": fields.get("report_url"),
            # `started_at` is the fixture's own name for it; the column is
            # created_at, which is what the session serializer reads back out.
            "started_at": fields.get("started_at", datetime.now(timezone.utc)),
            # ck_agent_sessions_terminal: terminal states require ended_at.
            "finalized_at": datetime.now(timezone.utc) if terminal else None,
            "ended_at": datetime.now(timezone.utc) if terminal else None,
        })
        db.commit()
        return sess_id
    finally:
        db.close()


def test_messages_returns_completed_session_so_a_reload_keeps_the_report_link(client, as_user):
    """THE regression test: refresh must not lose the download button."""
    user = _new_user()
    as_user(user)
    conv_id = _seed_conversation(user, "my story", datetime.now(timezone.utc))
    agent_id, _ = _general_support_agent()
    _seed_message(conv_id, 1, "user", "record a story")

    url = "https://qa-mohini-static.example.org/chatbot/storymedia/4119/story.pdf"
    sess_id = _seed_session(
        conv_id, agent_id, "completed", step=16, result_ref="4119", report_url=url,
    )

    body = client.get(f"/api/conversations/{conv_id}/messages").json()

    assert body["sessions"], "a reload has no other way to recover the report link"
    assert body["sessions"][0]["id"] == str(sess_id)
    assert body["sessions"][0]["state"] == "completed"
    assert body["sessions"][0]["report_url"] == url
    assert body["sessions"][0]["result_ref"] == "4119"
    assert body["sessions"][0]["step"] == 16


def test_messages_returns_an_empty_session_list_for_a_conversation_that_never_had_one(client, as_user):
    """Plain LLM conversations have no session; the key must still be present
    (an empty list) so the client can iterate without guarding for undefined."""
    user = _new_user()
    as_user(user)
    conv_id = _seed_conversation(user, "just chatting", datetime.now(timezone.utc))
    _seed_message(conv_id, 1, "user", "hello")

    body = client.get(f"/api/conversations/{conv_id}/messages").json()

    assert "sessions" in body
    assert body["sessions"] == []


def test_messages_returns_every_session_oldest_first(client, as_user):
    """A conversation can hold several sessions over its life and the client
    needs all of them -- returning only the newest is what hid a completed
    story's report link behind a later agent's session."""
    user = _new_user()
    as_user(user)
    conv_id = _seed_conversation(user, "two stories", datetime.now(timezone.utc))
    agent_id, _ = _general_support_agent()

    older = datetime.now(timezone.utc) - timedelta(hours=2)
    older_id = _seed_session(conv_id, agent_id, "abandoned", started_at=older)
    newest_id = _seed_session(
        conv_id, agent_id, "completed", result_ref="4119",
        report_url="https://static.example.org/x.pdf",
        started_at=datetime.now(timezone.utc),
    )

    body = client.get(f"/api/conversations/{conv_id}/messages").json()

    assert [s["id"] for s in body["sessions"]] == [str(older_id), str(newest_id)]
    assert body["sessions"][-1]["result_ref"] == "4119"


def test_session_is_not_exposed_across_tenants(client, as_user):
    """The existing 404 scoping must still gate the new field -- it carries a
    report URL, so leaking it would be worse than leaking the transcript."""
    owner = _new_user()
    as_user(owner)
    conv_id = _seed_conversation(owner, "private story", datetime.now(timezone.utc))
    agent_id, _ = _general_support_agent()
    _seed_session(
        conv_id, agent_id, "completed", result_ref="4119",
        report_url="https://static.example.org/secret.pdf",
    )

    as_user(_new_user())
    response = client.get(f"/api/conversations/{conv_id}/messages")

    assert response.status_code == 404
    assert "secret.pdf" not in response.text


# ---------------------------------------------------------------------------
# The recent-conversations list itself.
#
# Reported as "I can't see any of my conversations". Two causes, both here:
# every "New chat" archived the conversation just finished (and archived rows
# are excluded), and empty shells crowded out the real ones.
# ---------------------------------------------------------------------------

def test_finished_conversations_stay_in_the_list_after_starting_a_new_chat(client, script):
    """THE regression test for the empty sidebar.

    /api/reset used to archive the conversation being left, so the list could
    never show more than the one in progress -- everything the user had already
    finished disappeared the moment they clicked New chat.
    """
    from tests.characterisation.conftest import DEFAULT_AGENT, chat

    titles = ["first question", "second question", "third question"]
    for title in titles:
        script.queue("ok")
        chat(client, title, DEFAULT_AGENT)
        client.post("/api/reset")

    body = client.get("/api/conversations?limit=5").json()
    listed = [c["title"] for c in body["conversations"]]

    assert listed == list(reversed(titles)), (
        f"every finished conversation must remain listed, newest first; got {listed}"
    )


def test_empty_conversations_do_not_take_up_slots_in_the_list(client, script):
    """A reset leaves behind a conversation nobody typed into. Listing those
    shows 'New conversation / No messages yet' rows that push real history out
    of the top 5."""
    from tests.characterisation.conftest import DEFAULT_AGENT, chat

    script.queue("ok")
    chat(client, "a real conversation", DEFAULT_AGENT)
    client.post("/api/reset")          # leaves an empty conversation behind
    client.post("/api/reset")          # and another

    body = client.get("/api/conversations?limit=5").json()

    assert [c["title"] for c in body["conversations"]] == ["a real conversation"]


def test_last_active_timestamp_is_not_hours_stale(client, script):
    """last_message_at was written with a naive utcnow() into a timestamptz
    column, so Postgres read it in the server's zone and stored every
    conversation hours in the past -- the sidebar said "Last active 5 hours
    ago" about a chat that had just happened."""
    from tests.characterisation.conftest import DEFAULT_AGENT, chat

    script.queue("ok")
    chat(client, "just now", DEFAULT_AGENT)

    db = SessionLocal()
    try:
        row = db.execute(text("""
            SELECT now() - last_message_at AS staleness
            FROM conversations WHERE title = 'just now'
        """)).fetchone()
    finally:
        db.close()

    assert row is not None
    assert row.staleness < timedelta(minutes=1), (
        f"last_message_at is {row.staleness} behind now() -- timezone skew is back"
    )


# ---------------------------------------------------------------------------
# The reported bug: reopening a finished story from history lost its
# Download PDF button once another agent had been used.
#
# Reproduced from live data -- conversation 838dd5fe held TWO sessions:
#   record_stories      completed      story=4122  report=YES   21:47
#   capture_discussion  awaiting_user  story=None  report=no    22:51
# Returning only the newest handed the client the discussion session, whose
# state is neither 'finalizing' nor 'completed', so nothing rendered. The
# report was never lost -- the API returned the wrong one of the two.
# ---------------------------------------------------------------------------

def test_a_later_agents_session_does_not_hide_a_completed_storys_report(client, as_user):
    """THE regression test for the missing Download PDF button."""
    user = _new_user()
    as_user(user)
    conv_id = _seed_conversation(user, "what is a learning circle", datetime.now(timezone.utc))
    agent_id, _ = _general_support_agent()

    report = "https://qa-mohini-static.example.org/chatbot/storymedia/4122/story.pdf"
    story_id = _seed_session(
        conv_id, agent_id, "completed", step=16, result_ref="4122", report_url=report,
        started_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    discussion_id = _seed_session(
        conv_id, agent_id, "awaiting_user", step=2,
        started_at=datetime.now(timezone.utc),
    )

    body = client.get(f"/api/conversations/{conv_id}/messages").json()
    by_id = {s["id"]: s for s in body["sessions"]}

    assert str(story_id) in by_id, (
        "the completed story session must survive a later agent's session -- "
        "this is exactly what made the Download PDF button disappear"
    )
    assert by_id[str(story_id)]["report_url"] == report
    assert by_id[str(story_id)]["state"] == "completed"
    # And the later session is still there, so polling/resume keeps working.
    assert str(discussion_id) in by_id
    assert body["sessions"][-1]["id"] == str(discussion_id), "ordered oldest first"


def test_messages_carry_their_agent_session_id(client, as_user):
    """The client anchors each session's completion notice to that session's
    last message. Without this the report link renders at the very bottom,
    below a later agent's turns."""
    user = _new_user()
    as_user(user)
    conv_id = _seed_conversation(user, "anchored", datetime.now(timezone.utc))
    agent_id, _ = _general_support_agent()
    sess_id = _seed_session(conv_id, agent_id, "completed", result_ref="4122",
                            report_url="https://static.example.org/x.pdf")

    _seed_message(conv_id, 1, "user", "hello")
    _seed_message(conv_id, 2, "assistant", "hi there", agent_id=agent_id, agent_session_id=sess_id)

    msgs = client.get(f"/api/conversations/{conv_id}/messages").json()["messages"]

    assert msgs[0]["agent_session_id"] is None, "user messages belong to no session"
    assert msgs[1]["agent_session_id"] == str(sess_id)


# ---------------------------------------------------------------------------
# The reported bug: reopening a conversation from history lost the agent
# breadcrumb.
#
# A conversation that had walked Capture Discussion -> Record Story -> General
# Support came back showing only "Context: General Support Agent / Sub-context:
# Resumed conversation" -- the whole journey collapsed to whoever spoke last.
#
# The journey was never missing from the database: conversation_messages.agent_id
# holds it, and ConversationService.flow_payload() already reconstructs it on
# every /api/chat turn. It simply was not on THIS endpoint, so the client had
# nothing to restore from. These tests pin `flow` onto the resume contract.
# ---------------------------------------------------------------------------

def _flow(client, conversation_id):
    return client.get(f"/api/conversations/{conversation_id}/messages").json()["flow"]


def test_reopening_a_multi_agent_conversation_returns_the_full_journey(
    client, script, second_llm_agent
):
    """THE regression test. Three stops, and the last agent repeats an earlier
    one -- the exact shape of the reported bug (Capture Discussion -> Record
    Story -> General Support), plus proof that dedup is against the previous
    stop only, so an agent may legitimately appear twice."""
    from tests.characterisation.conftest import DEFAULT_AGENT, chat

    second_name, _key = second_llm_agent

    script.queue("a")
    _, first = chat(client, "start here", DEFAULT_AGENT)
    conv_id = first["conversation_id"]

    script.queue("b")
    chat(client, "switch", second_name, conversation_id=conv_id)

    script.queue("c")
    _, live = chat(client, "and back", DEFAULT_AGENT, conversation_id=conv_id)

    resumed = _flow(client, conv_id)

    assert resumed["stops"] == [DEFAULT_AGENT, second_name, DEFAULT_AGENT]
    assert resumed["stops"] == live["flow"]["stops"], (
        "resuming must show the identical breadcrumb the live turn showed -- "
        "this is what regressed when only /api/chat returned `flow`"
    )
    assert resumed["title"] == live["flow"]["title"]


def test_history_flow_current_index_points_at_the_last_stop(
    client, script, second_llm_agent
):
    """The client highlights stops[current_index]. Off-by-one here would mark
    the wrong agent as the one in play on every resumed conversation."""
    from tests.characterisation.conftest import DEFAULT_AGENT, chat

    second_name, _key = second_llm_agent

    script.queue("a")
    _, first = chat(client, "one", DEFAULT_AGENT)
    conv_id = first["conversation_id"]
    script.queue("b")
    chat(client, "two", second_name, conversation_id=conv_id)

    flow = _flow(client, conv_id)

    assert flow["stops"] == [DEFAULT_AGENT, second_name]
    assert flow["current_index"] == len(flow["stops"]) - 1


def test_consecutive_turns_with_one_agent_are_a_single_stop(client, script):
    """A long single-agent conversation must not produce a breadcrumb with one
    stop per turn."""
    from tests.characterisation.conftest import DEFAULT_AGENT, chat

    script.queue("a")
    _, first = chat(client, "one", DEFAULT_AGENT)
    conv_id = first["conversation_id"]
    for text_ in ("two", "three"):
        script.queue("reply")
        chat(client, text_, DEFAULT_AGENT, conversation_id=conv_id)

    flow = _flow(client, conv_id)

    assert flow["stops"] == [DEFAULT_AGENT]
    assert flow["current_index"] == 0


def test_flow_is_empty_when_no_agent_has_spoken_yet(client, as_user):
    """User messages carry no agent_id, so a conversation nobody has been
    answered in has no journey at all. The client falls back to Home on this,
    which is why it must be an empty list and -1 rather than absent or null."""
    user = _new_user()
    as_user(user)
    conv_id = _seed_conversation(user, "unanswered", datetime.now(timezone.utc))
    _seed_message(conv_id, 1, "user", "hello?")

    flow = _flow(client, conv_id)

    assert flow["stops"] == []
    assert flow["current_index"] == -1


# ---------------------------------------------------------------------------
# Documents survive a conversation being reopened
# ---------------------------------------------------------------------------

_PDF = {
    "file_name": "MIP_student-focus-primary-grades",
    "format": "pdf",
    "media_type": "application/pdf",
    "url": "https://qa-mohini-static.shikshalokam.org/chatbot/2/x/1786-MIP.pdf",
}
_DOCX = {
    **_PDF,
    "format": "docx",
    "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "url": "https://qa-mohini-static.shikshalokam.org/chatbot/2/x/1786-MIP.docx",
}


def test_reopening_a_conversation_returns_its_documents(client, as_user):
    """THE REQUIREMENT: the files are still there, and still on the right
    message, when the conversation is reopened from the sidebar.

    Nothing else in the UI links to a generated document, so an omission here
    does not degrade the experience -- it loses the file.
    """
    user = _new_user()
    as_user(user)
    conv_id = _seed_conversation(user, "attendance is low", datetime.now(timezone.utc))
    agent_id, _ = _general_support_agent()

    _seed_message(conv_id, 1, "user", "attendance is low")
    _seed_message(conv_id, 2, "assistant", "Tell me more.", agent_id=agent_id)
    _seed_message(
        conv_id, 3, "assistant", "Your plan is ready to download.",
        agent_id=agent_id, attachments=[_PDF, _DOCX],
    )
    _seed_message(conv_id, 4, "assistant", "Anything else?", agent_id=agent_id)

    body = client.get(f"/api/conversations/{conv_id}/messages").json()
    msgs = body["messages"]

    # RAW, exactly as stored -- the same asymmetry `options` documents.
    assert msgs[2]["attachments"] == [_PDF, _DOCX]

    # ...and nowhere else. A later turn must not inherit an earlier one's files.
    assert msgs[0]["attachments"] is None
    assert msgs[1]["attachments"] is None
    assert msgs[3]["attachments"] is None


def test_a_conversation_with_no_documents_reports_none(client, as_user):
    user = _new_user()
    as_user(user)
    conv_id = _seed_conversation(user, "hello", datetime.now(timezone.utc))
    agent_id, _ = _general_support_agent()
    _seed_message(conv_id, 1, "user", "hello")
    _seed_message(conv_id, 2, "assistant", "hi there", agent_id=agent_id)

    body = client.get(f"/api/conversations/{conv_id}/messages").json()

    assert all(m["attachments"] is None for m in body["messages"])


def test_a_user_message_cannot_carry_documents(client, as_user):
    """Enforced by ck_conversation_messages_attachments_only_assistant, the same
    guard `options` has had since it was added. Nothing produces one, so the
    constraint is what keeps that true."""
    import sqlalchemy.exc

    user = _new_user()
    as_user(user)
    conv_id = _seed_conversation(user, "hello", datetime.now(timezone.utc))

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        _seed_message(conv_id, 1, "user", "hello", attachments=[_PDF])
