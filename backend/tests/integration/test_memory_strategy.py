"""`memory.strategy` decides whether history is read at all.

`recent()` used to consult only `history_turns`, which defaults to 10 and is left
at its default by every `strategy: "none"` agent -- so `record_stories` and
`capture_discussion` each paid a ten-row SELECT plus ten
`MessageDTO.model_validate` calls on EVERY turn, for a list that provably cannot
be read: `RemoteFlowAgentHandler` never touches `ctx.history`, and its docstring
says so as a structural guarantee.

Small per turn, and on three of the four configured agents.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.database.engine import SessionLocal
from app.domain.core import UserContext
from app.repositories.conversations import ConversationRepository
from app.repositories.messages import MessageRepository


@pytest.fixture
def db_session():
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def conversation(db_session):
    user = UserContext(
        user_id=f"u-{uuid.uuid4().hex[:8]}",
        email="mem@example.com",
        display_name="Mem",
        tenant_code="MEM_TENANT",
    )
    conv = ConversationRepository(db_session).get_or_create(None, user)
    db_session.commit()
    return conv


class _Memory:
    """The two fields `recent()` reads, without pulling in the whole spec."""

    def __init__(self, strategy="recent", history_turns=10):
        self.strategy = strategy
        self.history_turns = history_turns


def _fill(db_session, conv, n=4):
    repo = MessageRepository(db_session)
    agent_id = db_session.execute(text("select id from agents limit 1")).scalar_one()
    for seq in range(1, n + 1):
        repo.insert(conv.id, seq, "assistant", f"m{seq}", agent_id=agent_id)
    db_session.commit()


def test_strategy_none_returns_nothing_and_issues_no_query(db_session, conversation):
    """THE ASSERTION THAT MATTERS IS THE SECOND ONE. Returning `[]` while still
    running the SELECT would satisfy every behavioural test and save nothing --
    which is exactly what the old code did, one layer up."""
    _fill(db_session, conversation)

    statements = []
    original = db_session.execute

    def _spy(statement, *args, **kwargs):
        statements.append(statement)
        return original(statement, *args, **kwargs)

    db_session.execute = _spy
    try:
        rows = MessageRepository(db_session).recent(
            conversation.id, _Memory(strategy="none"),
        )
    finally:
        db_session.execute = original

    assert rows == []
    assert statements == [], "strategy 'none' still hit the database"


def test_strategy_recent_is_unchanged(db_session, conversation):
    """The guard must not have turned memory off for the agents that have it."""
    _fill(db_session, conversation, n=4)

    rows = MessageRepository(db_session).recent(
        conversation.id, _Memory(strategy="recent", history_turns=3),
    )

    assert [r.content for r in rows] == ["m2", "m3", "m4"], (
        "the last N, in chronological order"
    )


def test_the_guard_keys_on_strategy_and_not_on_history_turns(db_session, conversation):
    """KEYED ON `strategy`, NEVER ON AN AGENT KEY OR A WINDOW SIZE. A
    `strategy: "none"` agent that also sets a non-default `history_turns` must
    still read nothing -- the config's single source of truth for "has memory" is
    the strategy field, which `LlmAgentHandler` already branches on."""
    _fill(db_session, conversation)

    rows = MessageRepository(db_session).recent(
        conversation.id, _Memory(strategy="none", history_turns=50),
    )

    assert rows == []


def test_the_two_remote_agents_really_are_configured_for_no_memory(db_session):
    """The guard above is worth nothing if no agent actually sets it. Read off
    the seeded config rather than asserted from memory, so a config change that
    quietly re-enables history shows up here."""
    rows = db_session.execute(text("""
        select a.key, c.config #>> '{memory,strategy}' as strategy
        from agents a
        join agent_configs c on c.agent_id = a.id
        where a.key in ('capture_discussion', 'record_stories')
          and c.is_active
    """)).all()

    assert rows, "neither remote agent is configured -- the seed is missing"
    for row in rows:
        assert row.strategy == "none", (
            f"{row.key} now reads history on every turn; "
            "RemoteFlowAgentHandler still cannot use it"
        )
