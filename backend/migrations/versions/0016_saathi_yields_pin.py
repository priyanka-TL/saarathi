"""Let the Saathi agent yield its session pin

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-11 15:30:00.000000

WHAT THIS FIXES
---------------
A Saathi conversation could never be left. Its flow ends conversationally
("Good luck!") but never reports `status: "COMPLETED"`, so `is_complete()` is
never true, the session never becomes terminal, and RouterService Gate 2 pinned
the conversation to Saathi for good -- swallowing even an explicit request for a
different agent. Verified in the live database: of six Saathi sessions, ZERO ever
reached `completed`, while the two interview agents reach it routinely.

`routing.yields_to_keyword` lets such an agent give the turn up when the router
has positive evidence for a different one. See RoutingSpec for why it defaults
to False: an interview must NEVER yield, because a user answering "I want to
tell my story about attendance" is talking to it, not asking to leave.

Applied ONLY to `saathi`. `record_stories` and `capture_discussion` are
untouched and stay absolutely pinned.

WHY A NEW VERSION AND NOT AN EDIT
---------------------------------
Same rule as 0013 and 0015: a config change is a new active version, never an
in-place UPDATE, so the previous config stays one activate call away.

SCOPE-AWARE and IDEMPOTENT, for the reasons 0015 spells out: a tenant with its
own Saathi config would otherwise keep the stuck behaviour, and a re-run must
not churn versions.
"""
import hashlib
import json
from typing import Any, Dict, Sequence, Tuple, Union

from alembic import op

revision: str = "0016"
down_revision: Union[str, Sequence[str], None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SYSTEM_ACTOR = "system"

#: The only agent that yields. An interview must not, ever.
PROVIDER = "saathi"


def canonical(spec: Dict[str, Any]) -> Tuple[str, str]:
    """The exact bytes and checksum `app.domain.agent_spec.canonical_json`
    produces, reproduced with the standard library so this migration keeps
    working when that function moves."""
    payload = json.dumps(spec, separators=(",", ":"), sort_keys=True)
    return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def upgrade() -> None:
    from sqlalchemy import text

    bind = op.get_bind()

    rows = bind.execute(
        text("""
            SELECT c.id, c.agent_id, c.tenant_id, c.organization_id, c.config
              FROM agent_configs c
             WHERE c.is_active
               AND c.config->'remote'->>'provider' = :provider
        """),
        {"provider": PROVIDER},
    ).fetchall()

    for row in rows:
        config = dict(row.config)
        routing = dict(config.get("routing") or {})

        if routing.get("yields_to_keyword") is True:
            continue  # already set -- no version churn on a re-run

        routing["yields_to_keyword"] = True
        config["routing"] = routing
        payload, checksum = canonical(config)

        # DEACTIVATE BEFORE INSERT: uq_agent_configs_one_active is a partial
        # unique index and is not DEFERRABLE. Scoped to this row's own
        # (tenant, organization) -- an unscoped deactivate would clear every
        # other scope's active row too.
        bind.execute(
            text("""
                UPDATE agent_configs
                   SET is_active = FALSE, updated_by = :actor
                 WHERE agent_id = :agent_id
                   AND tenant_id = :tenant_id
                   AND organization_id = :organization_id
                   AND is_active
            """),
            {
                "actor": SYSTEM_ACTOR,
                "agent_id": row.agent_id,
                "tenant_id": row.tenant_id,
                "organization_id": row.organization_id,
            },
        )
        bind.execute(
            text("""
                INSERT INTO agent_configs (agent_id, tenant_id, organization_id,
                                           version, checksum, config, is_active,
                                           activated_at, created_by, updated_by)
                SELECT CAST(:agent_id AS uuid),
                       CAST(:tenant_id AS varchar),
                       CAST(:organization_id AS varchar),
                       COALESCE(MAX(version), 0) + 1,
                       CAST(:checksum AS varchar), CAST(:config AS jsonb),
                       TRUE, now(),
                       CAST(:actor AS varchar), CAST(:actor AS varchar)
                  FROM agent_configs
                 WHERE agent_id = CAST(:agent_id AS uuid)
                   AND tenant_id = CAST(:tenant_id AS varchar)
                   AND organization_id = CAST(:organization_id AS varchar)
            """),
            {
                "agent_id": row.agent_id,
                "tenant_id": row.tenant_id,
                "organization_id": row.organization_id,
                "checksum": checksum,
                "config": payload,
                "actor": SYSTEM_ACTOR,
            },
        )


def downgrade() -> None:
    """Reactivate the version that preceded the yield flag.

    A re-activation rather than a reverse edit -- the point of writing a new
    version instead of mutating one.
    """
    from sqlalchemy import text

    bind = op.get_bind()

    rows = bind.execute(
        text("""
            SELECT c.agent_id, c.tenant_id, c.organization_id, c.version
              FROM agent_configs c
             WHERE c.is_active
               AND c.config->'remote'->>'provider' = :provider
               AND c.config->'routing'->>'yields_to_keyword' = 'true'
        """),
        {"provider": PROVIDER},
    ).fetchall()

    for row in rows:
        previous = bind.execute(
            text("""
                SELECT version FROM agent_configs
                 WHERE agent_id = :agent_id AND tenant_id = :tenant_id
                   AND organization_id = :organization_id AND version < :version
                 ORDER BY version DESC LIMIT 1
            """),
            {
                "agent_id": row.agent_id,
                "tenant_id": row.tenant_id,
                "organization_id": row.organization_id,
                "version": row.version,
            },
        ).scalar()
        if previous is None:
            continue

        bind.execute(
            text("""
                UPDATE agent_configs SET is_active = FALSE
                 WHERE agent_id = :agent_id AND tenant_id = :tenant_id
                   AND organization_id = :organization_id AND is_active
            """),
            {
                "agent_id": row.agent_id,
                "tenant_id": row.tenant_id,
                "organization_id": row.organization_id,
            },
        )
        bind.execute(
            text("""
                UPDATE agent_configs
                   SET is_active = TRUE, activated_at = now()
                 WHERE agent_id = :agent_id AND tenant_id = :tenant_id
                   AND organization_id = :organization_id AND version = :version
            """),
            {
                "agent_id": row.agent_id,
                "tenant_id": row.tenant_id,
                "organization_id": row.organization_id,
                "version": previous,
            },
        )
