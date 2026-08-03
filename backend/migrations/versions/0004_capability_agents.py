"""capability_agents -- which agents sit under which capability

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-03 12:00:03.000000

A join table rather than a JSONB array on `capabilities`, so membership keeps a
real FK to `agents`: a dangling agent reference would render a button that fails
at click time, and the FK makes that unrepresentable.

NO TENANT COLUMNS. It inherits the scope of the capability it belongs to. A
tenant that needs different membership creates its own capability row at its own
scope, with its own rows here.

`label_override` replaces `agents.name` INSIDE this capability only, so one
agent can appear under several capabilities with different wording and order in
each.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SYSTEM_ACTOR = "system"


def upgrade() -> None:
    from sqlalchemy.dialects import postgresql

    op.create_table(
        "capability_agents",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("capability_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("display_order", sa.Integer(), server_default="100", nullable=False),
        sa.Column("label_override", sa.String(), nullable=True),
        sa.Column("is_visible", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        # Holds `action`, e.g.
        # {"action": {"type": "start_agent", "autostart": "..."}}. The autostart
        # text is sent with autostart:true so the server does not title the
        # conversation from the UI's canned opener.
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_by", sa.String(), server_default=SYSTEM_ACTOR, nullable=False),
        sa.Column("updated_by", sa.String(), server_default=SYSTEM_ACTOR, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["capability_id"], ["capabilities.id"],
                                name="fk_capability_agents_capability_id_capabilities",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"],
                                name="fk_capability_agents_agent_id_agents", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_capability_agents"),
        sa.UniqueConstraint("capability_id", "agent_id", name="uq_capability_agents_membership"),
    )

    op.create_index("ix_capability_agents_capability", "capability_agents",
                    ["capability_id", "display_order"])


def downgrade() -> None:
    op.drop_index("ix_capability_agents_capability", table_name="capability_agents")
    op.drop_table("capability_agents")
