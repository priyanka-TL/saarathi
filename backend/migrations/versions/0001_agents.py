"""agents -- the agent catalogue

Revision ID: 0001
Revises:
Create Date: 2026-08-03 12:00:00.000000

FIRST OF TEN. `0001`-`0009` create one table each in foreign key dependency
order; `0010` seeds the default catalogue. There are no ALTER migrations in
this history and there should not be: the schema was built on a fresh database,
so everything a table needs is in its own CREATE TABLE.

`agents` comes first because `agent_configs`, `capability_agents`,
`agent_sessions`, `conversation_messages` and `tool_executions` all point at it.

WHY `key` IS GLOBALLY UNIQUE EVEN THOUGH THE TABLE IS SCOPED
-----------------------------------------------------------
AgentRegistry caches its snapshot keyed by bare `key`, and the router,
orchestration and every open session look agents up that way. `tenant_id` /
`organization_id` record which scope OWNS an agent; they do not partition the
key space. A tenant differentiates behaviour with a tenant-scoped row in
`agent_configs`, not by minting a second agent with the same key.

CONSTRAINT NAMING
-----------------
CHECK constraints are declared with a BARE suffix. `Base.metadata`'s naming
convention (`"ck": "ck_%(table_name)s_%(constraint_name)s"`) is re-applied by
Alembic at `op.create_table()` time, so `name='key_slug'` renders as
`ck_agents_key_slug`. Passing the qualified name here would produce
`ck_agents_ck_agents_key_slug`, which is what the previous schema did.
Unique/FK/PK/index names do NOT go through that interpolation and are written
out in full.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_SCOPE = "default"
SYSTEM_ACTOR = "system"


def upgrade() -> None:
    from sqlalchemy.dialects import postgresql

    # ENUM types are created explicitly, with create_type=False on the column,
    # so the type is not created a second time by the column definition.
    postgresql.ENUM("llm", "remote_flow", name="agent_type_enum").create(op.get_bind())
    postgresql.ENUM("enabled", "disabled", name="agent_status_enum").create(op.get_bind())

    op.create_table(
        "agents",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.String(), server_default=DEFAULT_SCOPE, nullable=False),
        sa.Column("organization_id", sa.String(), server_default=DEFAULT_SCOPE, nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("agent_type", postgresql.ENUM("llm", "remote_flow",
                                                name="agent_type_enum", create_type=False),
                  nullable=False),
        sa.Column("status", postgresql.ENUM("enabled", "disabled",
                                            name="agent_status_enum", create_type=False),
                  server_default="enabled", nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="100", nullable=False),
        # A REGISTRY KEY ('brain', 'globe'), never a path or a URL. The icons are
        # inline SVG in the frontend so they can inherit currentColor; an <img
        # src> could not follow the theme.
        sa.Column("icon", sa.String(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'::jsonb"), nullable=False),
        # The audit block, identical on all nine tables.
        sa.Column("created_by", sa.String(), server_default=SYSTEM_ACTOR, nullable=False),
        sa.Column("updated_by", sa.String(), server_default=SYSTEM_ACTOR, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_agents"),
        sa.UniqueConstraint("key", name="uq_agents_key"),
        sa.UniqueConstraint("name", name="uq_agents_name"),
        sa.CheckConstraint("key ~ '^[a-z][a-z0-9_]{1,62}$'", name="key_slug"),
        sa.CheckConstraint("tenant_id <> '' AND organization_id <> ''", name="scope"),
    )

    # At most ONE default agent globally. RouterService's Gate 5 fallback
    # returns the first is_default row it finds; two would make the last
    # resort of routing non-deterministic.
    op.create_index("uq_agents_single_default", "agents", ["is_default"], unique=True,
                    postgresql_where=sa.text("is_default"))
    op.create_index("ix_agents_enabled", "agents", ["sort_order", "key"],
                    postgresql_where=sa.text("status = 'enabled'"))
    # AgentRegistry.maybe_reload polls MAX(updated_at) here on a TTL.
    op.create_index("ix_agents_updated", "agents", [sa.text("updated_at DESC")])


def downgrade() -> None:
    op.drop_index("ix_agents_updated", table_name="agents")
    op.drop_index("ix_agents_enabled", table_name="agents",
                  postgresql_where=sa.text("status = 'enabled'"))
    op.drop_index("uq_agents_single_default", table_name="agents",
                  postgresql_where=sa.text("is_default"))
    op.drop_table("agents")

    from sqlalchemy.dialects import postgresql
    postgresql.ENUM(name="agent_status_enum").drop(op.get_bind())
    postgresql.ENUM(name="agent_type_enum").drop(op.get_bind())
