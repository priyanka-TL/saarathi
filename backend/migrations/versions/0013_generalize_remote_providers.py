"""Generalize delegated agents onto one provider architecture

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-10 12:00:00.000000

WHAT THIS DOES, AND WHY IT IS A DATA MIGRATION RATHER THAN A SCHEMA ONE
-----------------------------------------------------------------------
No table changes. `agent_sessions` was already provider-neutral
(`remote_session_id`, `remote_flow`, `remote_bot_route`, `state_data`), which is
what made this whole refactor a code-and-config change rather than a rewrite.

Two things move:

1. THE SECOND DELEGATED AGENT TYPE GOES AWAY. `saathi_flow` existed for exactly
   one reason -- two platforms had independent `*_ENABLED` switches and the
   registry hid agents BY TYPE, so folding them together would have meant
   enabling one to get the other. Enablement is now per provider
   (PROVIDERS_ENABLED), so the type has nothing left to express. Its agents
   become `remote_flow` and are told apart by `remote.provider`, which is where
   that distinction always belonged.

   The `saathi_flow` LABEL STAYS IN agent_type_enum, orphaned. Postgres cannot
   drop an enum label without recreating the type, and migration 0011 is applied
   and must not be edited. An unused label costs nothing.

2. `remote` IS RESHAPED into a provider-neutral envelope plus an opaque
   `options` block the provider types for itself. The rule that decided the
   split: a field is in the envelope if and only if Saarathi's own core reads
   it. Nothing is dropped and nothing new is required, so the rewrite is
   mechanical -- see `_to_envelope`.

EVERY ACTIVE ROW AT EVERY SCOPE IS CONVERTED, not just the seeded three at
('default','default'). A tenant-scoped row left in the old shape would fail
validation, and `AgentRegistry.reload` would skip that one agent and log --
which is a silent loss of that tenant's customisation, exactly the failure this
architecture is supposed to make impossible.

A NEW VERSION IS WRITTEN, NEVER AN IN-PLACE EDIT, following 0007/0009/0012. The
previous config stays one `POST /api/agents/{key}/config/{version}/activate`
call away, which is what makes the rollback a redeploy rather than a restore.
Checksums change once, invalidating the handler cache once; that is expected.

RETIRING ENVIRONMENT VARIABLES ARE READ HERE, ONCE, AND BAKED INTO THE ROW --
the same move 0009 made for sixteen MITRA_* keys. After this migration these are
no longer read by the application at all:

    SAATHI_TENANT_CODE      -> remote.auth.tenant_code
    ELEVATE_BASE_URL        -> remote.auth.token_endpoint
    SAATHI_LOGIN_MECHANISM  -> remote.auth.scheme
    MITRA_HOST_CEILING      -> PROVIDER_HOST_CEILING (renamed, still env)
    MITRA_MAX_OPEN_CHANNELS -> PROVIDER_MAX_OPEN_CHANNELS (renamed, still env)
    MITRA_IDLE_CLOSE_S      -> PROVIDER_IDLE_CLOSE_S (renamed, still env)
    MITRA_ENABLED           ) -> PROVIDERS_ENABLED
    SAATHI_ENABLED          )

CREDENTIALS ARE STILL NOT HERE, and that line does not move. `remote.auth` names
the VARIABLES holding the Origin header and the ELEVATE login; their values stay
in `.env`, because a config row is readable through the admin API.
"""
import hashlib
import json
import os
from typing import Any, Dict, Sequence, Tuple, Union

from alembic import op

revision: str = "0013"
down_revision: Union[str, Sequence[str], None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SYSTEM_ACTOR = "system"

#: The agent types whose configs carry a `remote` block. The second one is what
#: this migration retires.
_DELEGATED_TYPES = ("remote_flow", "saathi_flow")

#: Fallbacks for a deployment whose .env never carried the retiring keys. These
#: are the QA values the previous migrations already defaulted to.
_ELEVATE_BASE_URL_DEFAULT = "https://qa.elevate-apis.shikshalokam.org"
_SAATHI_TENANT_CODE_DEFAULT = "saathi"

#: Old SAATHI_LOGIN_MECHANISM value -> new remote.auth.scheme. The names changed
#: because "password" and "token" described the credential rather than the
#: exchange, and a third scheme could not have been named in that vocabulary.
_SCHEME_BY_MECHANISM = {
    "password": "elevate_login",
    "token": "static_token",
}

#: provider -> the environment variable that held its Origin credential before
#: `remote.auth.credential_env` named it. Used only when a row carries no
#: `origin_env` of its own, which is the case for every seeded row.
_DEFAULT_ORIGIN_ENV = {
    "mitra": "MITRA_ORIGIN_URL",
    "saathi": "SAATHI_ORIGIN_URL",
}


def canonical(spec: Dict[str, Any]) -> Tuple[str, str]:
    """The exact bytes and checksum `app.domain.agent_spec.canonical_json`
    produces. Reproduced with the standard library so this migration keeps
    working when that function moves; test_seed_migration.py asserts they agree.
    """
    payload = json.dumps(spec, separators=(",", ":"), sort_keys=True)
    return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _auth_block(provider: str, origin_env: str) -> Dict[str, Any]:
    """`remote.auth` for a provider, from the environment being retired.

    Names variables, never holds values -- see the module docstring.
    """
    credential_env = origin_env or _DEFAULT_ORIGIN_ENV.get(provider, "")

    if provider != "saathi":
        # A guest platform is admitted on the Origin header alone.
        return {"scheme": "origin_header", "credential_env": credential_env}

    mechanism = (os.getenv("SAATHI_LOGIN_MECHANISM") or "password").strip().lower()
    auth: Dict[str, Any] = {
        "scheme": _SCHEME_BY_MECHANISM.get(mechanism, "elevate_login"),
        "credential_env": credential_env,
        "token_endpoint": os.getenv("ELEVATE_BASE_URL") or _ELEVATE_BASE_URL_DEFAULT,
        "tenant_code": os.getenv("SAATHI_TENANT_CODE") or _SAATHI_TENANT_CODE_DEFAULT,
    }
    if auth["scheme"] == "static_token":
        auth["token_env"] = "SAATHI_ACCESS_TOKEN"
    else:
        auth["identifier_env"] = "SAATHI_EMAIL"
        auth["secret_env"] = "SAATHI_PASSWORD"
    return auth


def _to_envelope(remote: Dict[str, Any]) -> Dict[str, Any]:
    """The old `remote` block, rewritten into envelope + typed options.

    Mechanical: every value below comes from the row being converted. The only
    things read from anywhere else are the credential VARIABLE NAMES, which is
    what `_auth_block` assembles.
    """
    provider = remote.get("provider") or "mitra"
    connection = remote.get("connection") or {}
    paths = connection.get("paths") or {}

    envelope: Dict[str, Any] = {
        "provider": provider,
        "transport": "websocket",
        "base_url": connection.get("base_url", ""),
        "stream_url": connection.get("ws_url", ""),
        "allowed_hosts": connection.get("allowed_hosts", []),
        "timeouts": {
            "connect_s": connection.get("connect_timeout_s", 10.0),
            "read_s": connection.get("read_timeout_s", 30.0),
            "stream_connect_s": connection.get("ws_connect_timeout_s", 10.0),
        },
        "auth": _auth_block(provider, remote.get("origin_env") or ""),
        "flow_name": remote.get("flow_name", ""),
        "default_language": remote.get("default_language", "en"),
        "supported_languages": remote.get(
            "supported_languages", ["en", "hi", "kn", "te"],
        ),
        "turn": remote.get("turn") or {
            "first_turn_timeout_ms": 60000,
            "turn_timeout_ms": 45000,
            "idle_gap_ms": 8000,
        },
        "produces_artifact": remote.get("produces_artifact", True),
        "report_media_type": remote.get("report_media_type", "application/pdf"),
    }

    # User-Agent becomes one of the fixed headers rather than its own field, so
    # a platform expecting other fixed headers needs no schema change.
    user_agent = connection.get("user_agent")
    if user_agent:
        envelope["headers"] = {"User-Agent": user_agent}

    # ---- the provider's own block -------------------------------------
    options: Dict[str, Any] = {
        "bot_route": remote.get("bot_route", ""),
        "ip_city": connection.get("ip_city", ""),
        "ip_state": connection.get("ip_state", ""),
        "ip_zip": connection.get("ip_zip", ""),
        "completion_poll_every_turn": remote.get("completion_poll_every_turn", True),
    }
    handshake = remote.get("handshake")
    if handshake:
        options["handshake"] = handshake

    if provider == "saathi":
        # `paths` IS DELIBERATELY DROPPED for this provider. The old row carried
        # the OTHER platform's six paths -- both agent types shared one
        # connection model -- and Saathi's client never read them: it hardcoded
        # its own four, one of which (`/api/shikshalokam/read-elevate-profile/`)
        # the shared model could not even express. Carrying them over would
        # write a config that looks authoritative and is ignored. The defaults
        # in app/providers/saathi/spec.py are the paths that were actually used.
        return {**envelope, "options": options}

    options["company"] = remote.get("company", "")
    options["finalize_path"] = remote.get("finalize_path")
    options["finalize_as_guest"] = remote.get("finalize_as_guest", False)
    if paths:
        options["paths"] = paths
    return {**envelope, "options": options}


def upgrade() -> None:
    from sqlalchemy import text

    bind = op.get_bind()

    # 1. The second delegated agent type retires. Done FIRST so a config written
    #    below can carry agent_type='remote_flow' consistently with its agent
    #    row -- the admin route refuses a config whose type differs from it.
    bind.execute(
        text("""
            UPDATE agents
               SET agent_type = CAST('remote_flow' AS agent_type_enum),
                   updated_by = :actor
             WHERE agent_type = CAST('saathi_flow' AS agent_type_enum)
        """),
        {"actor": SYSTEM_ACTOR},
    )

    # 2. Every ACTIVE config carrying a `remote` block, at EVERY scope.
    rows = bind.execute(
        text("""
            SELECT c.id, c.agent_id, c.tenant_id, c.organization_id,
                   c.version, c.config
              FROM agent_configs c
             WHERE c.is_active
               AND c.config->>'agent_type' = ANY(:types)
             ORDER BY c.agent_id, c.tenant_id, c.organization_id
        """),
        {"types": list(_DELEGATED_TYPES)},
    ).fetchall()

    for row in rows:
        config = dict(row.config)
        remote = config.get("remote")
        if not isinstance(remote, dict):
            # An agent typed as delegated with no remote block cannot have been
            # working; leave it exactly as it is rather than inventing one.
            continue
        if "options" in remote:
            # Already converted -- a re-run, or a row written after the upgrade.
            continue

        config["agent_type"] = "remote_flow"
        config["remote"] = _to_envelope(remote)
        payload, checksum = canonical(config)

        # DEACTIVATE BEFORE INSERT. uq_agent_configs_one_active is a partial
        # unique index and is not DEFERRABLE, so the two cannot be reordered.
        # Scoped to this row's own (tenant, organization): an unscoped
        # deactivate would clear every OTHER scope's active row too.
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
    """Reactivate the pre-conversion version and restore the agent type.

    The old rows were never deleted, so this is a re-activation rather than a
    reverse translation -- which is the point of writing a new version instead
    of editing in place.
    """
    from sqlalchemy import text

    bind = op.get_bind()

    rows = bind.execute(
        text("""
            SELECT c.agent_id, c.tenant_id, c.organization_id, c.version
              FROM agent_configs c
             WHERE c.is_active
               AND c.config->'remote' ? 'options'
        """)
    ).fetchall()

    for row in rows:
        previous = bind.execute(
            text("""
                SELECT version FROM agent_configs
                 WHERE agent_id = :agent_id
                   AND tenant_id = :tenant_id
                   AND organization_id = :organization_id
                   AND version < :version
                 ORDER BY version DESC
                 LIMIT 1
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

    # The agent type follows its restored config.
    bind.execute(
        text("""
            UPDATE agents a
               SET agent_type = CAST('saathi_flow' AS agent_type_enum)
              FROM agent_configs c
             WHERE c.agent_id = a.id
               AND c.is_active
               AND c.config->>'agent_type' = 'saathi_flow'
        """)
    )
