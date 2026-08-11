"""Allow Saathi's document host, so its downloads are not dropped

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-11 09:05:00.000000

WHAT THIS FIXES
---------------
Saathi now offers generated documents mid-conversation, and the URLs point at
the shared static host (`qa-mohini-static.shikshalokam.org`) rather than at
Saathi's own API host. `remote.allowed_hosts` on the saathi agent is `[]`, and
the only implicitly-trusted host is the one in `remote.base_url` -- so every
download link would be dropped by the allowlist check with nothing but a warning
in the log. The feature would ship switched off.

The allowlist itself is not the problem and is not being weakened: a URL handed
to a user's browser from an upstream response is exactly the thing that wants an
allowlist. This adds the one host that is legitimate.

WHY A NEW VERSION AND NOT AN EDIT
---------------------------------
Same rule as 0013: a config change is a new active version, never an in-place
UPDATE, so the previous config stays one `POST /api/agents/saathi/config/{v}/activate`
call away. The checksum changes once, invalidating the handler cache once.

SCOPE-AWARE. Every active saathi config at EVERY scope is updated, not just
('default','default'). A tenant that customised its Saathi agent would otherwise
keep an empty allowlist and lose every download, which is exactly the kind of
silent per-tenant divergence this project's scope rules exist to prevent.

IDEMPOTENT: a config that already permits the host is skipped rather than
rewritten, so re-running produces no version churn.
"""
import hashlib
import json
import os
from typing import Any, Dict, Sequence, Tuple, Union

from alembic import op

revision: str = "0015"
down_revision: Union[str, Sequence[str], None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SYSTEM_ACTOR = "system"

#: The provider whose documents live on a different host from its API.
PROVIDER = "saathi"

#: Overridable for a deployment whose static assets live elsewhere. Read once,
#: at migration time, and baked into the row -- the same move 0009 and 0013 made.
DOWNLOAD_HOST = os.getenv(
    "SAATHI_DOWNLOAD_HOST", "qa-mohini-static.shikshalokam.org",
).strip().lower()


def canonical(spec: Dict[str, Any]) -> Tuple[str, str]:
    """The exact bytes and checksum `app.domain.agent_spec.canonical_json`
    produces, reproduced with the standard library so this migration keeps
    working when that function moves."""
    payload = json.dumps(spec, separators=(",", ":"), sort_keys=True)
    return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def upgrade() -> None:
    from sqlalchemy import text

    if not DOWNLOAD_HOST:
        return

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
        remote = dict(config.get("remote") or {})
        hosts = list(remote.get("allowed_hosts") or [])

        if DOWNLOAD_HOST in {h.strip().lower() for h in hosts if isinstance(h, str)}:
            continue  # already permitted -- no version churn on a re-run

        remote["allowed_hosts"] = hosts + [DOWNLOAD_HOST]
        config["remote"] = remote
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
    """Reactivate the version that preceded the allowlist change.

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
               -- CAST(...) rather than `:host::text`: SQLAlchemy's named-param
               -- parser reads the `::` as the start of another placeholder and
               -- emits a syntax error.
               AND c.config->'remote'->'allowed_hosts' @> to_jsonb(CAST(:host AS text))
        """),
        {"provider": PROVIDER, "host": DOWNLOAD_HOST},
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
