"""conversations -- one chat thread

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-03 12:00:04.000000

IT OWNS NOTHING ABOUT AGENTS, AND THAT IS THE POINT
---------------------------------------------------
There is no `pinned_agent_id` column. Saarthi's product model is that many
agents pass through one conversation, so the three agent questions are answered
by the tables that actually hold agent state:

    which agent is driving right now?   the `agent_sessions` row whose state is
                                        not terminal -- at most one per
                                        conversation, enforced by
                                        uq_agent_sessions_one_open_per_conversation
                                        (migration 0006)
    which agent said this?              conversation_messages.agent_id, which
                                        ck_conversation_messages_assistant_attribution
                                        makes mandatory on assistant rows
                                        (migration 0007)
    which agents took part?             every agent_sessions row for the
                                        conversation

The old column duplicated the first of those. It was written under exactly the
condition that opened a session (`routing.pin_session`) and cleared exactly when
that session was abandoned, so it could only ever agree with `agent_sessions` or
be a bug -- and no constraint decided which. Deriving the answer removes the
second possibility, and removes the FK that made a conversation reference a
single agent.

`user_id` (not `external_user_id`) holds the identifier the user service issued,
as a string. It is NOT a foreign key and never will be: users are the user
service's records, and this value arrives as a JWT claim -- same reasoning as
`tenant_code`, which sits beside it. Every read scopes on the PAIR; that is the
ownership check.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SYSTEM_ACTOR = "system"


def upgrade() -> None:
    from sqlalchemy.dialects import postgresql

    postgresql.ENUM("active", "archived", name="conversation_status_enum").create(op.get_bind())

    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_code", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=True),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("status", postgresql.ENUM("active", "archived",
                                            name="conversation_status_enum", create_type=False),
                  server_default="active", nullable=False),
        sa.Column("locale", sa.String(), server_default="en", nullable=False),
        sa.Column("message_count", sa.Integer(), server_default="0", nullable=False),
        # Carries the `title_is_placeholder` flag, which lets the first thing a
        # user actually types replace a title generated for an autostart turn.
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(), server_default=SYSTEM_ACTOR, nullable=False),
        sa.Column("updated_by", sa.String(), server_default=SYSTEM_ACTOR, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_conversations"),
        sa.CheckConstraint("locale IN ('en', 'hi', 'kn', 'te')", name="locale"),
        sa.CheckConstraint("message_count >= 0", name="message_count"),
        sa.CheckConstraint("tenant_code <> '' AND user_id <> ''", name="owner"),
    )

    # The recent-conversations sidebar query, exactly. NULLS LAST because
    # last_message_at is only set at the END of a successful turn.
    op.create_index("ix_conversations_user_recent", "conversations",
                    ["tenant_code", "user_id", sa.text("last_message_at DESC NULLS LAST")],
                    postgresql_where=sa.text("status = 'active'"))
    op.create_index("ix_conversations_org", "conversations",
                    ["tenant_code", "organization_id",
                     sa.text("last_message_at DESC NULLS LAST")],
                    postgresql_where=sa.text(
                        "status = 'active' AND organization_id IS NOT NULL"))


def downgrade() -> None:
    op.drop_index("ix_conversations_org", table_name="conversations",
                  postgresql_where=sa.text("status = 'active' AND organization_id IS NOT NULL"))
    op.drop_index("ix_conversations_user_recent", table_name="conversations",
                  postgresql_where=sa.text("status = 'active'"))
    op.drop_table("conversations")

    from sqlalchemy.dialects import postgresql
    postgresql.ENUM(name="conversation_status_enum").drop(op.get_bind())
