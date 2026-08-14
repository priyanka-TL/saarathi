"""How a delegated turn ended, on the message row

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-14 12:00:00.000000

WHY THESE ARE COLUMNS AND NOT JUST LOG FIELDS
---------------------------------------------
The instrumentation added alongside this already emits all four values on the
`"turn completed"` log line. That was not enough, and the reason is worth
recording because it is the whole justification for this migration.

`latency_ms` has been a column since 0007. That single fact is what made it
possible to answer "how slow is Capture Discussions, and since when" from 450
accumulated rows, across ten runs spanning eleven days, months after the fact --
with one query and no foresight required.

The log line has no such property. A ten-turn interview was driven on 14 Aug to
read exactly these fields; its output went to a terminal's stdout, no log file
was configured, and the measurement is simply gone. Repeating it would mean
running another interview and watching in real time -- which answers "what is
happening now" and never "what changed, and when".

So: the same values, on the same row as `latency_ms`, in the same telemetry
block. Nothing else about the pipeline changes.

WHAT EACH ONE ANSWERS
---------------------
`ws_end_reason`      WHY the turn stopped being awaited, and the only one of the
                     four that cannot be inferred from anything else:

                       finish_reason -- the platform said it was done. Clean.
                       idle_gap      -- fragments stopped arriving and the
                                        backstop flushed what had accumulated.
                                        The text is usable, but the turn paid the
                                        whole configured gap (8s, by default)
                                        adding nothing.
                       turn_timeout  -- the entire turn budget ran out with
                                        content in hand.

                     A turn's DURATION cannot distinguish these. Nine seconds
                     spent thinking and one second of thinking followed by an
                     eight-second wait for a frame that never came look
                     identical in `latency_ms`, and they call for opposite
                     remedies -- the first is the platform being slow, the
                     second is the platform not signalling end-of-turn, which no
                     amount of speedup upstream would fix.

`ws_first_frame_ms`  Time from sending the turn to the FIRST fragment: the
                     platform's real think time, and the closest thing to a
                     time-to-first-token available from this side.

`ws_last_frame_ms`   Time to the LAST fragment. `latency_ms - ws_last_frame_ms`
                     is time spent waiting for nothing, and is the direct
                     measure of what an idle-gap ending costs.

`ws_fragments`       How many fragments the reply arrived in. 1 means the
                     platform is not streaming to us, which is what makes an
                     idle-gap ending likely in the first place.

NULLABLE, AND NULL IS MEANINGFUL. Only a turn delegated over a WebSocket has
any of this: an `llm` agent's reply and every `role='user'` row leave all four
NULL, as does a `remote_flow` turn recovered from a timeout rather than heard.
`ws_end_reason IS NOT NULL` is therefore the predicate that selects "turns this
telemetry describes", which is why the reporting query filters on it.

NO BACKFILL. Every row that exists predates the measurement, and inventing a
value for it would be indistinguishable from having measured one. That is the
same rule the CHECK on `latency_ms` exists to enforce at the other end.

NO INDEX, DELIBERATELY. The consuming query is an aggregate over the whole table
grouped by agent and reason -- a sequential scan either way at this size, and an
index would be write cost on the turn path for no read benefit. Add one when the
table is large enough for the aggregate to hurt, not before.

ASSISTANT-ONLY, and the CHECK is written with a BARE SUFFIX like every other one
in this schema: `Base.metadata`'s naming convention interpolates
`%(constraint_name)s` and re-applies at `create_check_constraint()` time, so a
qualified name gets double-prefixed. The downgrade drops it with RAW SQL for the
mirror-image reason -- `op.drop_constraint` runs the name through that same
convention and re-prefixes an already-prefixed name, producing a "constraint
does not exist" failure. Both traps, and both workarounds, are 0014's.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: Union[str, Sequence[str], None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: The three values `app.providers.transport.frames.TurnEnd` can produce. Pinned
#: in the CHECK below so a typo in a future writer fails loudly at the row rather
#: than quietly becoming a fourth category in the reporting query.
#:
#: tests/integration/test_seed_migration.py-style guard: if TurnEnd grows a
#: member, this list and the CHECK must grow with it. That is the intended
#: friction -- a new ending is a new thing to explain, not a silent widening.
END_REASONS = ("finish_reason", "idle_gap", "turn_timeout")


def upgrade() -> None:
    op.add_column(
        "conversation_messages",
        sa.Column("ws_end_reason", sa.String(), nullable=True),
    )
    op.add_column(
        "conversation_messages",
        sa.Column("ws_first_frame_ms", sa.Integer(), nullable=True),
    )
    op.add_column(
        "conversation_messages",
        sa.Column("ws_last_frame_ms", sa.Integer(), nullable=True),
    )
    op.add_column(
        "conversation_messages",
        sa.Column("ws_fragments", sa.Integer(), nullable=True),
    )

    allowed = ", ".join(f"'{reason}'" for reason in END_REASONS)
    op.create_check_constraint(
        "ws_end_reason",
        "conversation_messages",
        f"ws_end_reason IS NULL OR ws_end_reason IN ({allowed})",
    )
    # Only an assistant turn is delegated to a platform, so only an assistant row
    # can carry any of this -- the same rule `options` and `attachments` follow.
    op.create_check_constraint(
        "ws_only_assistant",
        "conversation_messages",
        "(ws_end_reason IS NULL AND ws_first_frame_ms IS NULL "
        " AND ws_last_frame_ms IS NULL AND ws_fragments IS NULL) "
        "OR role = 'assistant'",
    )
    # Monotonic durations, for the same reason `latency_ms` has one: these come
    # from a monotonic clock, so a negative value is a bug in the writer rather
    # than an unusual measurement, and it must not be storable.
    op.create_check_constraint(
        "ws_timings",
        "conversation_messages",
        "COALESCE(ws_first_frame_ms, 0) >= 0 "
        "AND COALESCE(ws_last_frame_ms, 0) >= 0 "
        "AND COALESCE(ws_fragments, 0) >= 0",
    )


def downgrade() -> None:
    # RAW SQL for the drops -- see the module docstring. `op.drop_constraint`
    # re-applies the `ck_%(table_name)s_` prefix to a name that already has it.
    for suffix in ("ws_timings", "ws_only_assistant", "ws_end_reason"):
        op.execute(
            "ALTER TABLE conversation_messages "
            f"DROP CONSTRAINT IF EXISTS ck_conversation_messages_{suffix}"
        )
    for column in (
        "ws_fragments", "ws_last_frame_ms", "ws_first_frame_ms", "ws_end_reason",
    ):
        op.drop_column("conversation_messages", column)
