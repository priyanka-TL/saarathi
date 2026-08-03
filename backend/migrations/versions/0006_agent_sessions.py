"""agent_sessions -- one agent's episode within a conversation

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-03 12:00:05.000000

THE MULTI-AGENT UNIT
--------------------
A conversation accumulates one row here per agent hand-off, and
`uq_agent_sessions_one_open_per_conversation` allows at most one of them to be
non-terminal at a time. That single partial unique index is the whole mechanism
for "which agent is driving this conversation" -- it makes the question have
exactly one answer without the `conversations` row knowing anything about
agents, and it is what replaced the old `conversations.pinned_agent_id`.

Only agents declaring `routing.pin_session` get a session at all; stateless LLM
agents answer a turn and leave no episode behind.

COLUMNS THAT ARE DELIBERATELY ABSENT
------------------------------------
  * `remote_provider` -- the provider is a property of the agent's spec
    (`remote.provider`), not of one session. The column existed and was never
    written by anything, so every row held NULL.
  * `error_code`      -- same story. `error` is the record.
  * `started_at`      -- it was always exactly `created_at`. The session
    serializer still emits a "started_at" key, sourced from `created_at`, so
    the API contract is unchanged.

`ended_at` / `finalized_at` / `last_activity_at` stay: they are lifecycle
timestamps with meanings the audit block does not carry, and `ck_..._terminal`
depends on `ended_at`.

FK POLICY
---------
  conversation_id -> ON DELETE CASCADE  (sessions belong to the conversation)
  agent_id        -> ON DELETE RESTRICT (an agent with a live session must not
                                         be deletable; the record of what it
                                         was doing is not disposable)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0006"
down_revision: Union[str, Sequence[str], None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SYSTEM_ACTOR = "system"

#: The states that mean "this episode is over". Repeated in several partial
#: index predicates below, and mirrored by SessionService.TERMINAL.
_TERMINAL_SQL = "state NOT IN ('completed', 'failed', 'abandoned')"


def upgrade() -> None:
    from sqlalchemy.dialects import postgresql

    postgresql.ENUM(
        "pending", "authenticating", "in_progress", "awaiting_user",
        "finalizing", "completed", "failed", "abandoned",
        name="session_state_enum",
    ).create(op.get_bind())

    op.create_table(
        "agent_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", postgresql.ENUM(
            "pending", "authenticating", "in_progress", "awaiting_user",
            "finalizing", "completed", "failed", "abandoned",
            name="session_state_enum", create_type=False),
            server_default="pending", nullable=False),
        sa.Column("remote_session_id", sa.String(), nullable=True),
        sa.Column("remote_profile_id", sa.String(), nullable=True),
        sa.Column("remote_flow", sa.String(), nullable=True),
        sa.Column("remote_bot_route", sa.String(), nullable=True),
        # The agent's configured default language, copied in at creation:
        # MitraChannel._authenticate sends this column's value as the route.
        sa.Column("language", sa.String(), server_default="en", nullable=False),
        sa.Column("step", sa.Integer(), server_default="0", nullable=False),
        sa.Column("turn_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("result_ref", sa.String(), nullable=True),
        sa.Column("report_url", sa.String(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("state_data", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(), server_default=SYSTEM_ACTOR, nullable=False),
        sa.Column("updated_by", sa.String(), server_default=SYSTEM_ACTOR, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"],
                                name="fk_agent_sessions_conversation_id_conversations",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"],
                                name="fk_agent_sessions_agent_id_agents", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_agent_sessions"),
        # Two requests must never finalise the same remote interview. This
        # unique constraint plus claim_finalizing()'s conditional UPDATE are the
        # idempotency mechanism.
        sa.UniqueConstraint("remote_session_id", name="uq_agent_sessions_remote_session"),
        sa.CheckConstraint("language IN ('en', 'hi', 'kn', 'te')", name="language"),
        sa.CheckConstraint("step >= 0 AND turn_count >= 0", name="counters"),
        # A terminal state and an ended_at are the same fact; neither may exist
        # without the other.
        sa.CheckConstraint(
            "(state IN ('completed', 'failed', 'abandoned')) = (ended_at IS NOT NULL)",
            name="terminal"),
        sa.CheckConstraint(
            "state IN ('pending', 'failed', 'abandoned') OR remote_session_id IS NOT NULL",
            name="active_has_remote"),
        sa.CheckConstraint("state <> 'completed' OR result_ref IS NOT NULL",
                           name="completed_has_result"),
    )

    # THE multi-agent invariant: one OPEN episode per conversation, any number
    # of closed ones.
    op.create_index("uq_agent_sessions_one_open_per_conversation", "agent_sessions",
                    ["conversation_id"], unique=True,
                    postgresql_where=sa.text(_TERMINAL_SQL))
    op.create_index("ix_agent_sessions_sweep", "agent_sessions",
                    ["state", "last_activity_at"],
                    postgresql_where=sa.text(_TERMINAL_SQL))
    # Every episode of a conversation, newest first -- what the messages
    # endpoint returns as its `sessions` array.
    op.create_index("ix_agent_sessions_conversation", "agent_sessions",
                    ["conversation_id", sa.text("created_at DESC")])


def downgrade() -> None:
    op.drop_index("ix_agent_sessions_conversation", table_name="agent_sessions")
    op.drop_index("ix_agent_sessions_sweep", table_name="agent_sessions",
                  postgresql_where=sa.text(_TERMINAL_SQL))
    op.drop_index("uq_agent_sessions_one_open_per_conversation", table_name="agent_sessions",
                  postgresql_where=sa.text(_TERMINAL_SQL))
    op.drop_table("agent_sessions")

    from sqlalchemy.dialects import postgresql
    postgresql.ENUM(name="session_state_enum").drop(op.get_bind())
