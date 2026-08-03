"""Remove the YAML/DB provenance concept from agent_configs.

WHY
===
`agent_configs.source` distinguished a config written by the startup YAML sync
(`'yaml'`) from one written through the admin API (`'db'`). Migration 0007
deleted the last of that machinery: there is no YAML, no ConfigSyncService, and
no code path that can ever write `'yaml'` again. A column with one reachable
value carries no information and invites the reader to believe a second source
of configuration still exists somewhere. The database is the only source.

WHAT THIS DOES
==============
1. **Deletes superseded YAML-era config rows.** Migration 0007 superseded them
   with a new version rather than editing them, deliberately, so that the old
   config stayed one `activate` call away. That safety argument does not survive
   contact with the schema: those rows carry `bot_route_env` / `company_env` /
   `${VAR}`, which `BaseAgentSpec` (extra="forbid") rejects. Activating one does
   not roll anything back -- `activate_config_version` does not validate, so the
   reload that follows SKIPS the agent and it disappears from routing and the
   sidebar with only a log line. They are not rollback targets, they are a trap.

   Only rows that are BOTH inactive AND legacy-shaped are removed. The predicate
   is `is_legacy` from migration 0007, repeated here rather than imported: a
   migration must keep working when the code it once matched moves on.

2. **Drops `source`,** rebuilding `ix_agent_cfg_lookup` without it first --
   Postgres would otherwise drop the index silently along with the column, and
   the lookup it serves (agent + scope, newest version first) still matters.

3. **Neutralises `created_by`.** Its server default was `'startup-sync'`, naming
   the thing that no longer exists. New rows get `'system'`; existing rows
   carrying the old marker are backfilled, since the process they name never ran
   against any database that reaches this migration.

WHAT IT DELIBERATELY LEAVES ALONE
=================================
`audit_log_action_enum` still contains `'config_sync'`, and `audit_logs` still
holds rows using it. An audit trail records what happened, not what the schema
currently permits; rewriting history to match a refactor is the one thing an
audit log must never do. The value is unreachable from application code, which
is enough.

DOWNGRADE restores the column, the enum and the index shape. It cannot restore
the deleted rows -- they are gone, and re-synthesising configs that never
validate would be worse than their absence.
"""
from typing import Any, Dict
import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def is_legacy(config_text: str) -> bool:
    """True when a stored config was written for the YAML-era schema.

    Verbatim from migration 0007, on purpose. Both migrations must agree on what
    "legacy" means, and neither may import application code to find out.
    """
    if "${" in config_text:
        return True
    try:
        config = json.loads(config_text)
    except ValueError:
        return False
    remote = config.get("remote")
    if not isinstance(remote, dict):
        return False
    if "bot_route_env" in remote or "company_env" in remote:
        return True
    return not (remote.get("bot_route") and remote.get("company"))


def upgrade() -> None:
    from sqlalchemy import text

    bind = op.get_bind()

    # ---- 1. delete superseded YAML-era rows -------------------------------
    #
    # Fetched and filtered in Python rather than matched with SQL LIKE: the
    # legacy test is structural (does `remote` carry the old keys, or lack the
    # new ones), and a text pattern would both miss shapes and catch a prompt
    # that merely mentions `${`.
    rows = bind.execute(
        text("""
            SELECT c.id, c.config::text AS config_text
            FROM agent_configs c
            WHERE c.is_active = FALSE
        """)
    ).fetchall()

    doomed = [r.id for r in rows if is_legacy(r.config_text)]
    if doomed:
        bind.execute(
            text("DELETE FROM agent_configs WHERE id = ANY(:ids)"),
            {"ids": doomed},
        )

    # ---- 2. drop `source` -------------------------------------------------
    #
    # Rebuild the index first. Dropping a column silently drops every index that
    # references it, so relying on the column drop would leave the (agent,
    # scope, version DESC) lookup unindexed with nothing in the diff to show it.
    op.drop_index("ix_agent_cfg_lookup", table_name="agent_configs")
    op.create_index(
        "ix_agent_cfg_lookup",
        "agent_configs",
        ["agent_id", "tenant_id", "organization_id", sa.text("version DESC")],
    )

    op.drop_column("agent_configs", "source")
    postgresql.ENUM(name="config_source_enum").drop(bind, checkfirst=True)

    # ---- 3. neutralise created_by ----------------------------------------
    op.alter_column(
        "agent_configs",
        "created_by",
        existing_type=sa.String(),
        existing_nullable=False,
        server_default="system",
    )
    bind.execute(
        text("UPDATE agent_configs SET created_by = 'system' WHERE created_by = 'startup-sync'")
    )


def downgrade() -> None:
    from sqlalchemy import text

    bind = op.get_bind()

    op.alter_column(
        "agent_configs",
        "created_by",
        existing_type=sa.String(),
        existing_nullable=False,
        server_default="startup-sync",
    )

    config_source_enum = postgresql.ENUM("yaml", "db", name="config_source_enum")
    config_source_enum.create(bind, checkfirst=True)

    # Nullable, then backfilled, then NOT NULL -- adding a NOT NULL column with
    # no default to a populated table fails outright.
    op.add_column(
        "agent_configs",
        sa.Column(
            "source",
            postgresql.ENUM("yaml", "db", name="config_source_enum", create_type=False),
            nullable=True,
        ),
    )
    bind.execute(text("UPDATE agent_configs SET source = 'db'"))
    op.alter_column("agent_configs", "source", nullable=False)

    op.drop_index("ix_agent_cfg_lookup", table_name="agent_configs")
    op.create_index(
        "ix_agent_cfg_lookup",
        "agent_configs",
        ["agent_id", "tenant_id", "organization_id", "source", sa.text("version DESC")],
    )
