"""capabilities -- the sidebar's ADVANCED cards

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-03 12:00:02.000000

One row per card. Scoped, so a tenant customises by inserting its own row at
its own scope, shadowing the 'default' row with the same key -- no writes are
needed to onboard a tenant, and shipping a card to everyone is one insert at
default scope.

`capability_status_enum` is deliberately a SEPARATE type from
`agent_status_enum` even though it is a superset. `coming_soon` is a
capability-only state: a card advertised before the thing behind it exists (SG
Commons Portal). No agent handler knows how to serve one, so folding the two
enums together would put an unservable value within reach of the routing code.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_SCOPE = "default"
SYSTEM_ACTOR = "system"


def upgrade() -> None:
    from sqlalchemy.dialects import postgresql

    postgresql.ENUM("active", "disabled", "coming_soon",
                    name="capability_status_enum").create(op.get_bind())

    op.create_table(
        "capabilities",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.String(), server_default=DEFAULT_SCOPE, nullable=False),
        sa.Column("organization_id", sa.String(), server_default=DEFAULT_SCOPE, nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        # A REGISTRY KEY ('brain', 'globe'), never a path or a URL -- see 0001.
        sa.Column("icon", sa.String(), nullable=True),
        sa.Column("badge", sa.String(), nullable=True),
        sa.Column("status", postgresql.ENUM("active", "disabled", "coming_soon",
                                            name="capability_status_enum", create_type=False),
                  server_default="active", nullable=False),
        sa.Column("display_order", sa.Integer(), server_default="100", nullable=False),
        # Holds `action` -- what clicking the CARD does, e.g.
        # {"action": {"type": "display_card"}}. The set of action types is
        # closed and lives in the frontend's ChatPage registry; an unrecognised
        # one is normalised to inert rather than executed.
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_by", sa.String(), server_default=SYSTEM_ACTOR, nullable=False),
        sa.Column("updated_by", sa.String(), server_default=SYSTEM_ACTOR, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_capabilities"),
        # One row per key PER SCOPE -- that is what lets a tenant shadow the
        # default row with its own version of the same capability.
        sa.UniqueConstraint("tenant_id", "organization_id", "key",
                            name="uq_capabilities_scope_key"),
        # Same slug rule as agents.key, so a capability key and an agent key are
        # interchangeable-looking identifiers in config and in the API.
        sa.CheckConstraint("key ~ '^[a-z][a-z0-9_]{1,62}$'", name="key_slug"),
        sa.CheckConstraint("tenant_id <> '' AND organization_id <> ''", name="scope"),
    )

    op.create_index("ix_capabilities_scope", "capabilities",
                    ["tenant_id", "organization_id", "display_order"],
                    postgresql_where=sa.text("status <> 'disabled'"))


def downgrade() -> None:
    op.drop_index("ix_capabilities_scope", table_name="capabilities",
                  postgresql_where=sa.text("status <> 'disabled'"))
    op.drop_table("capabilities")

    from sqlalchemy.dialects import postgresql
    postgresql.ENUM(name="capability_status_enum").drop(op.get_bind())
