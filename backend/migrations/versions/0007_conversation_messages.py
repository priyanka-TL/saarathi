"""conversation_messages -- the chat history, append-only

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-03 12:00:06.000000

Created after `agent_sessions` because it references it. That ordering is the
reason this schema needs no deferred `add_constraint` step: every FK is declared
inline in the CREATE TABLE that owns it.

PER-MESSAGE AGENT ATTRIBUTION IS MANDATORY
------------------------------------------
`ck_conversation_messages_assistant_attribution` requires `agent_id` on every
assistant row. That is what makes a multi-agent conversation legible after the
fact: no reply is ever anonymous, and `MessageRepository.distinct_agent_sequence`
can reconstruct the hand-off order from the messages alone, which is what the
flow breadcrumb renders.

NO `agent_config_id`
--------------------
It was declared alongside the rest of the attribution block and never had a
writer -- the value passed was `getattr(agent, "config_id", None)` against a
dataclass with no such field, so every row in every database held NULL. Config
provenance is available through `agent_configs.checksum`, which the registry
already carries per agent.

`uq_conversation_messages_seq` is UNIQUE on (conversation_id, seq), and
`ConversationRepository.next_seq_for_update` allocates `seq` with an atomic
`UPDATE ... RETURNING message_count`, so two concurrent turns cannot both claim
the same slot.

FK POLICY
---------
  conversation_id  -> ON DELETE CASCADE   (messages belong to the conversation)
  agent_id         -> ON DELETE SET NULL  (attribution is lost, the message is
                                           not -- but see the CHECK above: this
                                           can only fire for non-assistant rows)
  agent_session_id -> ON DELETE SET NULL
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0007"
down_revision: Union[str, Sequence[str], None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SYSTEM_ACTOR = "system"


def upgrade() -> None:
    from sqlalchemy.dialects import postgresql

    postgresql.ENUM("user", "assistant", "system", "tool",
                    name="message_role_enum").create(op.get_bind())

    op.create_table(
        "conversation_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", postgresql.ENUM("user", "assistant", "system", "tool",
                                          name="message_role_enum", create_type=False),
                  nullable=False),
        sa.Column("content", sa.String(), nullable=False),
        # attribution
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("agent_session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("route_reason", sa.String(), nullable=True),
        sa.Column("route_confidence", sa.Numeric(precision=4, scale=3), nullable=True),
        # interaction affordances
        sa.Column("options", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("selected_option_id", sa.String(), nullable=True),
        # telemetry
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("request_id", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), server_default=SYSTEM_ACTOR, nullable=False),
        sa.Column("updated_by", sa.String(), server_default=SYSTEM_ACTOR, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"],
                                name="fk_conversation_messages_conversation_id_conversations",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"],
                                name="fk_conversation_messages_agent_id_agents",
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["agent_session_id"], ["agent_sessions.id"],
                                name="fk_conversation_messages_agent_session_id_agent_sessions",
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_conversation_messages"),
        sa.UniqueConstraint("conversation_id", "seq", name="uq_conversation_messages_seq"),
        sa.CheckConstraint("seq > 0", name="seq"),
        # Every assistant reply names the agent that produced it.
        sa.CheckConstraint("role <> 'assistant' OR agent_id IS NOT NULL",
                           name="assistant_attribution"),
        sa.CheckConstraint("options IS NULL OR role = 'assistant'",
                           name="options_only_assistant"),
        sa.CheckConstraint(
            "COALESCE(prompt_tokens, 0) >= 0 AND COALESCE(completion_tokens, 0) >= 0",
            name="tokens"),
        sa.CheckConstraint("COALESCE(latency_ms, 0) >= 0", name="latency"),
        sa.CheckConstraint(
            "route_confidence IS NULL OR (route_confidence >= 0 AND route_confidence <= 1)",
            name="confidence"),
    )

    op.create_index("ix_conversation_messages_conversation_seq", "conversation_messages",
                    ["conversation_id", "seq"])
    op.create_index("ix_conversation_messages_agent_time", "conversation_messages",
                    ["agent_id", sa.text("created_at DESC")])
    op.create_index("ix_conversation_messages_request", "conversation_messages",
                    ["request_id"], postgresql_where=sa.text("request_id IS NOT NULL"))
    op.create_index("ix_conversation_messages_session", "conversation_messages",
                    ["agent_session_id"],
                    postgresql_where=sa.text("agent_session_id IS NOT NULL"))


def downgrade() -> None:
    op.drop_index("ix_conversation_messages_session", table_name="conversation_messages",
                  postgresql_where=sa.text("agent_session_id IS NOT NULL"))
    op.drop_index("ix_conversation_messages_request", table_name="conversation_messages",
                  postgresql_where=sa.text("request_id IS NOT NULL"))
    op.drop_index("ix_conversation_messages_agent_time", table_name="conversation_messages")
    op.drop_index("ix_conversation_messages_conversation_seq", table_name="conversation_messages")
    op.drop_table("conversation_messages")

    from sqlalchemy.dialects import postgresql
    postgresql.ENUM(name="message_role_enum").drop(op.get_bind())
