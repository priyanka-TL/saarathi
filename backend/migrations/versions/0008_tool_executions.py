"""tool_executions -- one tool invocation within an LLM agent turn

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-03 12:00:07.000000

Written only when an agent's spec sets `features.record_tool_executions`.

DESIGN DECISIONS BAKED INTO THE DDL
-----------------------------------
  1. ON DELETE CASCADE from message_id. Retention rides conversation
     retention automatically: deleting a conversation cascades to its
     messages, which cascade to their tool executions. No separate policy.

  2. ck_tool_executions_error -- status != 'success' implies error IS NOT NULL.
     A failure record that does not explain itself is unintelligible and
     should be rejected at write time, not discovered during a post-incident
     investigation.

  3. `result_excerpt` is TEXT, and truncation to 4096 chars happens at WRITE
     TIME in the repository layer rather than as a DB CHECK -- so the
     truncation is logged and observable, unlike a silent DB-side trim.

  4. agent_id is ON DELETE SET NULL, not CASCADE: the execution record is
     evidence of what happened. Losing agent attribution when an agent is
     decommissioned is acceptable; losing the record is not.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0008"
down_revision: Union[str, Sequence[str], None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SYSTEM_ACTOR = "system"


def upgrade() -> None:
    from sqlalchemy.dialects import postgresql

    postgresql.ENUM("success", "error", "timeout", name="tool_status_enum").create(op.get_bind())

    op.create_table(
        "tool_executions",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("tool_name", sa.String(), nullable=False),
        sa.Column("iteration", sa.SmallInteger(), server_default="1", nullable=False),
        sa.Column("arguments", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("result_excerpt", sa.Text(), nullable=True),
        sa.Column("result_bytes", sa.Integer(), nullable=True),
        sa.Column("status", postgresql.ENUM("success", "error", "timeout",
                                            name="tool_status_enum", create_type=False),
                  nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), server_default=SYSTEM_ACTOR, nullable=False),
        sa.Column("updated_by", sa.String(), server_default=SYSTEM_ACTOR, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["conversation_messages.id"],
                                name="fk_tool_executions_message_id_conversation_messages",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"],
                                name="fk_tool_executions_agent_id_agents", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_tool_executions"),
        sa.CheckConstraint("iteration BETWEEN 1 AND 10", name="iteration"),
        sa.CheckConstraint("duration_ms >= 0", name="duration"),
        sa.CheckConstraint("status = 'success' OR error IS NOT NULL", name="error"),
    )

    # The primary lookup path -- every execution for one message.
    op.create_index("ix_tool_executions_message", "tool_executions", ["message_id"])
    # Per-tool latency and volume queries.
    op.create_index("ix_tool_executions_name_time", "tool_executions",
                    ["tool_name", sa.text("created_at DESC")])
    # Error rate by tool -- partial, because failures are the rare case.
    op.create_index("ix_tool_executions_failures", "tool_executions",
                    ["tool_name", sa.text("created_at DESC")],
                    postgresql_where=sa.text("status <> 'success'"))


def downgrade() -> None:
    op.drop_index("ix_tool_executions_failures", table_name="tool_executions",
                  postgresql_where=sa.text("status <> 'success'"))
    op.drop_index("ix_tool_executions_name_time", table_name="tool_executions")
    op.drop_index("ix_tool_executions_message", table_name="tool_executions")
    op.drop_table("tool_executions")

    from sqlalchemy.dialects import postgresql
    postgresql.ENUM(name="tool_status_enum").drop(op.get_bind())
