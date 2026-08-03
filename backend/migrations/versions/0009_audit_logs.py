"""audit_logs -- configuration and session lifecycle changes, append-only

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-03 12:00:08.000000

NO FOREIGN KEYS, DELIBERATELY
-----------------------------
An audit row must outlive its subject. A deleted agent's `config_activate` rows
are exactly the rows an investigation needs, and any FK would either cascade
them away or block the delete. `entity_type` + `entity_id` are a soft reference
and are indexed as one.

Last of the nine table migrations, and the only one with no dependencies, so it
could sit anywhere in the order. It is last because it is the only table nothing
else joins to.

THE ACTOR IS `created_by`
-------------------------
This table used to spell the same concept `actor`, which meant the one table
whose entire purpose is provenance was the one that named it differently from
the other eight. It is now part of the standard audit block like everywhere
else.

`id` is a BigInteger identity rather than a UUID -- the only such PK in the
schema. Audit rows are written in bulk, read in time order, and never
referenced by id from elsewhere, so a monotonic key is both smaller and better
clustered than a random UUID.

There is no `config_sync` action. It belonged to the YAML-era startup
reconciliation, which no longer exists; nothing could write it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0009"
down_revision: Union[str, Sequence[str], None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SYSTEM_ACTOR = "system"

_ACTIONS = (
    "config_create", "config_activate",
    "agent_enable", "agent_disable",
    "session_finalize", "session_abandon",
    "capability_create", "capability_update", "capability_delete",
    "capability_agent_set",
)


def upgrade() -> None:
    from sqlalchemy.dialects import postgresql

    postgresql.ENUM(*_ACTIONS, name="audit_action_enum").create(op.get_bind())

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("action", postgresql.ENUM(*_ACTIONS, name="audit_action_enum",
                                            create_type=False), nullable=False),
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("request_id", sa.String(), nullable=True),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("note", sa.String(), nullable=True),
        # created_by IS the actor.
        sa.Column("created_by", sa.String(), server_default=SYSTEM_ACTOR, nullable=False),
        sa.Column("updated_by", sa.String(), server_default=SYSTEM_ACTOR, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_audit_logs"),
    )

    op.create_index("ix_audit_logs_entity", "audit_logs",
                    ["entity_type", "entity_id", sa.text("created_at DESC")])
    op.create_index("ix_audit_logs_action_time", "audit_logs",
                    ["action", sa.text("created_at DESC")])


def downgrade() -> None:
    op.drop_index("ix_audit_logs_action_time", table_name="audit_logs")
    op.drop_index("ix_audit_logs_entity", table_name="audit_logs")
    op.drop_table("audit_logs")

    from sqlalchemy.dialects import postgresql
    postgresql.ENUM(name="audit_action_enum").drop(op.get_bind())
