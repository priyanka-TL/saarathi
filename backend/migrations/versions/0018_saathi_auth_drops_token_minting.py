"""Saathi's remote.auth drops its token-minting fields

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-12 12:00:00.000000

WHY
---
Saathi used to authenticate to its own REST/WS surface with a connection-level
credential shared by every user of a tenant's Saathi agent -- either a
hardcoded token (`scheme: "static_token"`) or one shared ELEVATE service login
(`scheme: "elevate_login"`, minted via `remote.auth.token_endpoint` +
`identifier_env`/`secret_env`, or `token_env` for the static case). That
directly contradicted the provider's own "per-user assistant" description: two
different logged-in people got the same identity.

Saarthi now has real end-user login (the frontend authenticates directly
against ELEVATE's user service), so `app/providers/saathi/provider.py` uses
the CALLER'S OWN `UserContext.token` instead -- never minted, never cached.
`app/providers/saathi/auth.py` (LoginTokenProvider/StaticTokenProvider) is
deleted, and `RemoteAuthSpec` (app/domain/agent_spec.py, STRICT / extra=forbid)
lost the fields that existed only to support those two schemes:
`token_endpoint`, `tenant_code`, `identifier_env`, `secret_env`, `token_env`.

Any active Saathi row still carrying those keys now FAILS PYDANTIC VALIDATION
on load (extra fields forbidden) -- AgentRegistry.reload would skip that agent
and log, exactly the silent-loss failure mode 0013's own docstring warns about.
This migration rewrites `remote.auth` for every such row to the new shape,
which is now IDENTICAL to a guest platform's: `{"scheme": "origin_header",
"credential_env": <preserved>}`. Saathi's Origin credential (gating admission,
per Django Channels' AllowedHostsOriginValidator) is unrelated to this change
and is carried over unchanged.

EVERY ACTIVE ROW AT EVERY SCOPE, not just the seeded one -- same reasoning as
0013, 0015, 0016, 0017: a tenant-scoped customisation left in the old shape
would silently vanish from the registry rather than failing loudly.

A NEW VERSION IS WRITTEN, NEVER AN IN-PLACE EDIT, following 0013/0015/0016/0017.
SCOPE-AWARE and IDEMPOTENT: a row whose auth has already been converted (no
`elevate_login`/`static_token` scheme left) is skipped, so a re-run churns no
versions.
"""
import hashlib
import json
from typing import Any, Dict, Sequence, Tuple, Union

from alembic import op

revision: str = "0018"
down_revision: Union[str, Sequence[str], None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SYSTEM_ACTOR = "system"

PROVIDER = "saathi"

#: The two schemes this migration retires. Any other scheme (i.e.
#: "origin_header", already Mitra's shape) is left untouched.
_LEGACY_SCHEMES = ("elevate_login", "static_token")

#: Fallback if a row somehow carries no credential_env at all -- matches the
#: default 0013 used for Saathi's Origin credential.
_DEFAULT_CREDENTIAL_ENV = "SAATHI_ORIGIN_URL"


def canonical(spec: Dict[str, Any]) -> Tuple[str, str]:
    """The exact bytes and checksum `app.domain.agent_spec.canonical_json`
    produces, reproduced with the standard library so this migration keeps
    working when that function moves."""
    payload = json.dumps(spec, separators=(",", ":"), sort_keys=True)
    return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def new_auth_block(old_auth: Dict[str, Any]) -> Dict[str, Any]:
    """The old auth block, stripped to the two fields that still mean
    anything: `scheme` (always "origin_header" now) and `credential_env`
    (preserved verbatim -- it is a variable NAME, not a secret).
    """
    return {
        "scheme": "origin_header",
        "credential_env": old_auth.get("credential_env") or _DEFAULT_CREDENTIAL_ENV,
    }


def upgrade() -> None:
    from sqlalchemy import text

    bind = op.get_bind()

    rows = bind.execute(
        text("""
            SELECT c.id, c.agent_id, c.tenant_id, c.organization_id, c.version, c.config
              FROM agent_configs c
             WHERE c.is_active
               AND c.config->'remote'->>'provider' = :provider
               AND c.config->'remote'->'auth'->>'scheme' = ANY(:schemes)
        """),
        {"provider": PROVIDER, "schemes": list(_LEGACY_SCHEMES)},
    ).fetchall()

    for row in rows:
        config = dict(row.config)
        remote = dict(config["remote"])
        remote["auth"] = new_auth_block(dict(remote.get("auth") or {}))
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
    """Reactivate the version that preceded this conversion.

    A re-activation rather than a reverse translation -- the old
    token_endpoint/identifier_env/secret_env/token_env VALUES were never
    stored anywhere this migration can read (only variable NAMES ever were,
    and 0013's defaults are not necessarily what a tenant had customised), so
    reconstructing them would be a guess dressed up as a restore. The prior
    version row already has the real ones and is one activate away, same as
    0013/0015/0016/0017.
    """
    from sqlalchemy import text

    bind = op.get_bind()

    rows = bind.execute(
        text("""
            SELECT c.agent_id, c.tenant_id, c.organization_id, c.version
              FROM agent_configs c
             WHERE c.is_active
               AND c.config->'remote'->>'provider' = :provider
               AND c.config->'remote'->'auth'->>'scheme' = 'origin_header'
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
