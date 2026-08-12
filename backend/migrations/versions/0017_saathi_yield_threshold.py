"""Give the Saathi yield decision its own, stricter confidence threshold

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-11 17:00:00.000000

WHAT THIS FIXES
---------------
0016 let `saathi` yield its session pin when the router has positive evidence
for a different agent, so an open-ended assistant conversation could finally
be left. But `_yield_from_pin`'s classifier step checked the confidence
against the CANDIDATE agent's own `routing.confidence_threshold` --
`record_stories`' default of 0.5, a bar tuned for cheap first-message routing
(Gate 4), where being wrong just costs one extra turn.

Reusing that same low bar to interrupt an ALREADY-IN-PROGRESS `saathi`
conversation is a materially different, higher-stakes decision, and 0.5 was
low enough that an ordinary, topically-adjacent reply -- observed live: a
quick-reply answer with no story content at all -- could clear it and yield
to `record_stories` unasked.

`routing.yield_confidence_threshold` (added on `RoutingSpec` alongside this
migration) is checked on the PINNED agent instead, and only applies to the
yield decision -- Gate 4's own routing into `record_stories` from a fresh
conversation is untouched and still uses that agent's 0.5. Set to 0.85 here:
high enough that an explicit, unambiguous request ("I want to record a
story") still clears it, low enough that it isn't effectively unreachable.

Applied ONLY to `saathi`, the one agent 0016 gave `yields_to_keyword`.

WHY A NEW VERSION AND NOT AN EDIT
---------------------------------
Same rule as 0013, 0015 and 0016: a config change is a new active version,
never an in-place UPDATE, so the previous config stays one activate call
away.

SCOPE-AWARE and IDEMPOTENT, for the reasons 0015 and 0016 spell out: a tenant
with its own Saathi config would otherwise keep the low-threshold behaviour,
and a re-run must not churn versions.
"""
import hashlib
import json
from typing import Any, Dict, Sequence, Tuple, Union

from alembic import op

revision: str = "0017"
down_revision: Union[str, Sequence[str], None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SYSTEM_ACTOR = "system"

#: The only agent that yields. An interview must not, ever.
PROVIDER = "saathi"

#: High enough that an explicit, unambiguous request still clears it; low
#: enough that it isn't effectively unreachable.
YIELD_CONFIDENCE_THRESHOLD = 0.85


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

        if routing.get("yield_confidence_threshold") == YIELD_CONFIDENCE_THRESHOLD:
            continue  # already set -- no version churn on a re-run

        routing["yield_confidence_threshold"] = YIELD_CONFIDENCE_THRESHOLD
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
    """Reactivate the version that preceded the yield threshold.

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
               AND (c.config->'routing'->>'yield_confidence_threshold')::float = :threshold
        """),
        {"provider": PROVIDER, "threshold": YIELD_CONFIDENCE_THRESHOLD},
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
