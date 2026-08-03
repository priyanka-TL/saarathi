"""agent_configs -- versioned, tenant-scoped agent configuration

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-03 12:00:01.000000

THE ONLY SOURCE OF AGENT CONFIGURATION. There is no YAML, no config sync and no
provenance column: every row has the same origin, so a `source` marker would
only imply that some second store still existed.

TWO INVARIANTS THE DDL ENFORCES
-------------------------------
  * `uq_agent_configs_scope_version` -- version numbering is PER SCOPE, so a
    tenant's v1 is its own and does not jump when an unrelated tenant edits.
  * `uq_agent_configs_one_active` -- exactly one active row per (agent, scope).
    It is a PARTIAL UNIQUE INDEX with no DEFERRABLE option, so it is checked
    PER STATEMENT: writers must DEACTIVATE BEFORE INSERTING, never the other
    way round. `app/routers/admin.py` does this in that order for a reason.

Each row carries its OWN checksum, computed from its own content. That is what
keeps HandlerFactory's `(spec.key, checksum)` cache from serving one tenant's
handler -- and its system prompt -- to another. Two tenants whose configs are
byte-identical DO share a handler, which is correct.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_SCOPE = "default"
SYSTEM_ACTOR = "system"


def upgrade() -> None:
    from sqlalchemy.dialects import postgresql

    op.create_table(
        "agent_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(), server_default=DEFAULT_SCOPE, nullable=False),
        sa.Column("organization_id", sa.String(), server_default=DEFAULT_SCOPE, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(), server_default=SYSTEM_ACTOR, nullable=False),
        sa.Column("updated_by", sa.String(), server_default=SYSTEM_ACTOR, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"],
                                name="fk_agent_configs_agent_id_agents", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_agent_configs"),
        sa.UniqueConstraint("agent_id", "tenant_id", "organization_id", "version",
                            name="uq_agent_configs_scope_version"),
        sa.CheckConstraint("version > 0", name="version"),
        sa.CheckConstraint("is_active = FALSE OR activated_at IS NOT NULL", name="activated"),
        # A config that cannot name its own agent is not a config. Cheap
        # structural guard against inserting the wrong JSON entirely.
        sa.CheckConstraint("config ? 'agent_type' AND config ? 'key' AND config ? 'name'",
                           name="shape"),
        sa.CheckConstraint("tenant_id <> '' AND organization_id <> ''", name="scope"),
    )

    op.create_index("uq_agent_configs_one_active", "agent_configs",
                    ["agent_id", "tenant_id", "organization_id"],
                    unique=True, postgresql_where=sa.text("is_active"))
    op.create_index("ix_agent_configs_lookup", "agent_configs",
                    ["agent_id", "tenant_id", "organization_id", sa.text("version DESC")])


def downgrade() -> None:
    op.drop_index("ix_agent_configs_lookup", table_name="agent_configs")
    op.drop_index("uq_agent_configs_one_active", table_name="agent_configs",
                  postgresql_where=sa.text("is_active"))
    op.drop_table("agent_configs")
