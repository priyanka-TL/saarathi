"""The WebSocket turn diagnostics, as COLUMNS rather than log fields.

WHY THIS IS A SCHEMA CHANGE AND NOT JUST LOGGING. `latency_ms` has been a column
since migration 0007, and that single fact is what made it possible to answer
"how slow is Capture Discussions, and since when" from hundreds of accumulated
rows, months after the fact, with one query and no foresight required.

A log line has no such property. A ten-turn interview was driven specifically to
read exactly these four values; its output went to a terminal's stdout, no log
file was configured, and the measurement is gone. Repeating it means running
another interview and watching in real time -- which answers "what is happening
now" and never "what changed, and when".

WHAT THE COLUMNS ANSWER, and why `latency_ms` cannot:

  Nine seconds spent thinking, and one second of thinking followed by an
  eight-second wait for a frame that never arrived, are INDISTINGUISHABLE in
  `latency_ms`. They call for opposite remedies -- the first is the platform
  being slow, the second is the platform not signalling end-of-turn, which no
  amount of speedup upstream would fix. `ws_end_reason` is the only field that
  separates them.

These tests run against a REAL Postgres, because three CHECK constraints are the
substance of migration 0022 and an in-memory fake would enforce none of them.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.database.engine import SessionLocal
from app.domain.core import UserContext
from app.repositories.conversations import ConversationRepository
from app.repositories.messages import MessageRepository

#: The three values `app.providers.transport.frames.TurnEnd` can produce, pinned
#: in the CHECK. A fourth ending is a new thing to explain, not a silent widening
#: of the reporting query's categories.
END_REASONS = ("finish_reason", "idle_gap", "turn_timeout")


@pytest.fixture
def db_session():
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def conversation(db_session):
    user = UserContext(
        user_id=f"u-{uuid.uuid4().hex[:8]}",
        email="ws@example.com",
        display_name="WS",
        tenant_code="WS_TENANT",
    )
    conv = ConversationRepository(db_session).get_or_create(None, user)
    db_session.commit()
    return conv


def _agent_id(db_session) -> uuid.UUID:
    """Any seeded agent: the `assistant_attribution` CHECK requires one on an
    assistant row, and which agent it is does not matter here."""
    return db_session.execute(text("select id from agents limit 1")).scalar_one()


def _insert(db_session, conv, *, role="assistant", ws=None, seq=1, agent_id=None):
    return MessageRepository(db_session).insert(
        conv.id, seq, role, "content",
        agent_id=agent_id if role == "assistant" else None,
        ws=ws,
    )


# ---------------------------------------------------------------------------
# The values reach the row
# ---------------------------------------------------------------------------


def test_a_delegated_turns_diagnostics_land_on_the_message_row(db_session, conversation):
    """ONE `ws` ARGUMENT, NOT FOUR. These four always travel together and come
    from one source -- the request's timing record -- so they are passed as the
    mapping that record already accumulated."""
    msg = _insert(db_session, conversation, agent_id=_agent_id(db_session), ws={
        "ws_end_reason": "idle_gap",
        "ws_first_frame_ms": 640,
        "ws_last_frame_ms": 1180,
        "ws_fragments": 3,
    })
    db_session.commit()

    assert msg.ws_end_reason == "idle_gap"
    assert msg.ws_first_frame_ms == 640
    assert msg.ws_last_frame_ms == 1180
    assert msg.ws_fragments == 3


def test_only_the_four_known_keys_are_read_from_the_timing_record(
    db_session, conversation,
):
    """The timing record carries more than these four (`remote_reestablished`,
    for one). Anything else must be IGNORED rather than silently becoming a
    column -- the repository names the four explicitly instead of splatting."""
    msg = _insert(db_session, conversation, agent_id=_agent_id(db_session), ws={
        "ws_end_reason": "finish_reason",
        "remote_reestablished": True,
        "ws_finish_reason": "stop",
        "something_invented": 1,
    })
    db_session.commit()

    assert msg.ws_end_reason == "finish_reason"
    assert not hasattr(msg, "remote_reestablished")


def test_a_turn_with_no_websocket_leaves_all_four_null(db_session, conversation):
    """An `llm` agent's reply. NULL IS MEANINGFUL: `ws_end_reason IS NOT NULL` is
    the predicate that selects "turns this telemetry describes", which is why the
    reporting query filters on it."""
    msg = _insert(db_session, conversation, agent_id=_agent_id(db_session), ws=None)
    db_session.commit()

    assert msg.ws_end_reason is None
    assert msg.ws_first_frame_ms is None
    assert msg.ws_last_frame_ms is None
    assert msg.ws_fragments is None


# ---------------------------------------------------------------------------
# The three CHECK constraints -- the substance of migration 0022
# ---------------------------------------------------------------------------


def test_an_unknown_end_reason_is_refused_by_the_database(db_session, conversation):
    """A typo in a future writer must fail loudly at the row rather than quietly
    becoming a fourth category in the reporting query."""
    with pytest.raises(IntegrityError, match="ws_end_reason"):
        # The repository flushes, so the CHECK fires here rather than at commit.
        _insert(db_session, conversation, agent_id=_agent_id(db_session), ws={
            "ws_end_reason": "gave_up",
        })
    db_session.rollback()


@pytest.mark.parametrize("reason", END_REASONS)
def test_every_end_reason_the_transport_can_produce_is_accepted(
    db_session, conversation, reason,
):
    """The other half of the constraint. A CHECK that rejected a value the
    transport really emits would fail the turn it was measuring."""
    _insert(db_session, conversation, agent_id=_agent_id(db_session),
            ws={"ws_end_reason": reason})
    db_session.commit()


def test_a_user_row_cannot_carry_websocket_diagnostics(db_session, conversation):
    """ASSISTANT-ONLY, the same rule `options` and `attachments` follow: only an
    assistant turn is delegated to a platform."""
    with pytest.raises(IntegrityError, match="ws_only_assistant"):
        # Raw SQL: the repository would refuse to attach `ws` to a user row long
        # before the database saw it, and the CHECK is the backstop being tested.
        db_session.execute(text(
            "insert into conversation_messages "
            "(id, conversation_id, seq, role, content, ws_end_reason, "
            " created_by, updated_by) "
            "values (:id, :conv, 1, 'user', 'hi', 'idle_gap', 'system', 'system')"
        ), {"id": uuid.uuid4(), "conv": conversation.id})
    db_session.rollback()


@pytest.mark.parametrize("field", [
    "ws_first_frame_ms", "ws_last_frame_ms", "ws_fragments",
])
def test_a_negative_duration_is_not_storable(db_session, conversation, field):
    """These come from a MONOTONIC clock, so a negative value is a bug in the
    writer rather than an unusual measurement -- the same reason `latency_ms` has
    a CHECK of its own."""
    with pytest.raises(IntegrityError, match="ws_timings"):
        _insert(db_session, conversation, agent_id=_agent_id(db_session),
                ws={"ws_end_reason": "finish_reason", field: -1})
    db_session.rollback()


# ---------------------------------------------------------------------------
# The query the whole exercise exists to make possible
# ---------------------------------------------------------------------------


def test_the_wasted_time_query_runs_against_the_real_schema(db_session, conversation):
    """THE POINT OF ALL FOUR COLUMNS. `latency_ms - ws_last_frame_ms` is time the
    turn spent waiting for nothing, and grouping it by `ws_end_reason` is what
    turns "Discussion Capture feels slow" into a number with a cause attached.

    Pinned as a test because a column nobody can query is a column nobody will
    read: this is the exact shape the runbook query takes, run against the real
    schema so a renamed column breaks here rather than in six months.
    """
    agent_id = _agent_id(db_session)
    repo = MessageRepository(db_session)
    repo.insert(conversation.id, 1, "assistant", "quick", agent_id=agent_id,
                latency_ms=9000, ws={
                    "ws_end_reason": "idle_gap",
                    "ws_last_frame_ms": 1000,
                    "ws_fragments": 1,
                })
    repo.insert(conversation.id, 2, "assistant", "honest", agent_id=agent_id,
                latency_ms=3000, ws={
                    "ws_end_reason": "finish_reason",
                    "ws_last_frame_ms": 2950,
                    "ws_fragments": 4,
                })
    db_session.commit()

    rows = db_session.execute(text("""
        select m.ws_end_reason,
               count(*) n,
               round(avg(m.latency_ms)) avg_ms,
               round(avg(m.latency_ms - m.ws_last_frame_ms)) avg_wasted_ms
        from conversation_messages m
        where m.role = 'assistant'
          and m.ws_end_reason is not null
          and m.conversation_id = :conv
        group by 1
    """), {"conv": conversation.id}).all()

    wasted = {r.ws_end_reason: r.avg_wasted_ms for r in rows}
    # The whole argument for the columns, in two numbers: an idle-gap ending
    # threw away 8 of its 9 seconds; a clean one wasted 50ms.
    assert wasted["idle_gap"] == 8000
    assert wasted["finish_reason"] == 50

