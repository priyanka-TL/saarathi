"""Saathi's config: download host, keyword yielding, and Origin-only auth

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-12 09:00:00.000000

A MERGE OF FOUR MIGRATIONS OVER THE SAME ROWS
----------------------------------------------
This was four consecutive revisions, each selecting exactly the same set (every
active config whose `remote.provider` is 'saathi') and setting one key on it:

    download host      remote.allowed_hosts += SAATHI_DOWNLOAD_HOST
    yields pin         routing.yields_to_keyword = true
    yield threshold    routing.yield_confidence_threshold = 0.85
    auth simplified    remote.auth -> {scheme: origin_header, credential_env}

None had been applied to a deployed database. They are collapsed here because
they touch DISJOINT KEYS -- so applying them in one pass produces exactly the
config the four-step chain produced, and the only difference is that a scope
gains one new `agent_configs` version instead of four. Each edit's rationale is
kept below; they are independent and are documented independently.

1. THE DOWNLOAD HOST
--------------------
Saathi serves documents from a different host than its API, and
`app/providers/transport/http.py::url_is_permitted` requires an exact host match
against `remote.allowed_hosts`. Without the static host in that list every
download is silently DROPPED -- the attachment path logs the host and moves on
rather than raising, so the failure looks like "the reply had no document".

Read once, at migration time, from `SAATHI_DOWNLOAD_HOST` and baked into the
row -- the same move 0013 made. An empty value skips this edit entirely rather
than appending an empty host.

2. YIELDS TO KEYWORD
--------------------
`routing.yields_to_keyword` lets an open Saathi session hand the turn back when
the user types a capability keyword, instead of swallowing it. Saathi is a
per-user assistant with no terminal state -- without this, an open session is a
trap: nothing the user types can reach another agent.

3. YIELD CONFIDENCE THRESHOLD
-----------------------------
0.85, deliberately high. The yield above is only taken when the router is
confident; a marginal match keeps the turn with Saathi rather than bouncing a
user out of an assistant mid-thought on a weak keyword hit.

4. AUTH DROPS TOKEN MINTING
---------------------------
The old block carried `token_endpoint`, `tenant_code`, and either
`identifier_env`/`secret_env` or `token_env` -- the machinery for MINTING an
ELEVATE token. Saathi admits on the Origin header alone, exactly as Mitra does,
so all of that was unreachable configuration describing a login that never
happens. What survives is `scheme` (now always "origin_header") and
`credential_env`, which is preserved VERBATIM because it is a variable NAME and
not a secret -- credentials never enter a config row (see CLAUDE.md).

Only the two legacy schemes are converted. A row already carrying
"origin_header" is left alone, which is what makes this idempotent.

SCOPE-AWARE and IDEMPOTENT. A tenant with its own saathi config must move too,
so the predicate selects by provider rather than by scope. Re-running is a
no-op: the merged edit is compared against the existing config and a row that
would not change is skipped, which is a stronger guarantee than the four
separate key-presence guards it replaces.
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
#: at migration time, and baked into the row -- the same move 0013 made.
DOWNLOAD_HOST = os.getenv(
    "SAATHI_DOWNLOAD_HOST", "qa-mohini-static.shikshalokam.org",
).strip().lower()

#: High on purpose -- see the module docstring, section 3.
YIELD_CONFIDENCE_THRESHOLD = 0.85

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


def apply(config: Dict[str, Any]) -> Dict[str, Any]:
    """All four edits to one config, as a pure function.

    MODULE-LEVEL AND PUBLIC so the test that pins the effective saathi spec can
    compose 0012 -> 0013 -> here and validate the result, rather than restating
    the edits -- which is how a pin comes to assert something the migration no
    longer does.

    The four edits touch disjoint keys, so they are order-independent and the
    result equals what the original four-migration chain produced.
    """
    config = dict(config)
    remote = dict(config.get("remote") or {})
    routing = dict(config.get("routing") or {})

    # 1. Download host -- skipped entirely when the env var is empty.
    if DOWNLOAD_HOST:
        hosts = list(remote.get("allowed_hosts") or [])
        known = {h.strip().lower() for h in hosts if isinstance(h, str)}
        if DOWNLOAD_HOST not in known:
            remote["allowed_hosts"] = hosts + [DOWNLOAD_HOST]

    # 2 & 3. Keyword yielding, and the confidence it requires.
    routing["yields_to_keyword"] = True
    routing["yield_confidence_threshold"] = YIELD_CONFIDENCE_THRESHOLD

    # 4. Auth -- only the two legacy schemes convert; origin_header is already
    #    the target shape and is left exactly as it is.
    auth = dict(remote.get("auth") or {})
    if auth.get("scheme") in _LEGACY_SCHEMES:
        remote["auth"] = new_auth_block(auth)

    config["remote"] = remote
    config["routing"] = routing
    return config


def _rewrite(bind, row, config: Dict[str, Any]) -> None:
    """Deactivate this row's active config and insert `config` as the next
    version, in the row's OWN scope.

    DEACTIVATE BEFORE INSERT: uq_agent_configs_one_active is a partial unique
    index and is not DEFERRABLE. Scoped to this row's own
    (tenant_id, organization_id) -- an unscoped deactivate would clear every
    other scope's active row too.
    """
    from sqlalchemy import text

    payload, checksum = canonical(config)
    scope = {
        "agent_id": row.agent_id,
        "tenant_id": row.tenant_id,
        "organization_id": row.organization_id,
    }

    bind.execute(
        text("""
            UPDATE agent_configs
               SET is_active = FALSE, updated_by = :actor
             WHERE agent_id = :agent_id
               AND tenant_id = :tenant_id
               AND organization_id = :organization_id
               AND is_active
        """),
        {**scope, "actor": SYSTEM_ACTOR},
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
        {**scope, "checksum": checksum, "config": payload, "actor": SYSTEM_ACTOR},
    )


def upgrade() -> None:
    from sqlalchemy import text

    bind = op.get_bind()

    # By PROVIDER, not by scope: a tenant with its own saathi config must move
    # too. The auth-scheme condition that the fourth migration carried in its
    # WHERE clause is a per-row branch inside apply() instead, because the other
    # three edits apply regardless of which scheme a row currently names.
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
        existing = dict(row.config)
        updated = apply(existing)
        if updated == existing:
            continue  # nothing to change -- no version churn on a re-run
        _rewrite(bind, row, updated)


def downgrade() -> None:
    """Re-activate the version that preceded this migration, per scope.

    A re-activation rather than a reverse edit -- the point of writing a new
    version instead of mutating one. One step undoes all four edits together,
    which is the other half of what merging them bought.
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
        scope = {
            "agent_id": row.agent_id,
            "tenant_id": row.tenant_id,
            "organization_id": row.organization_id,
        }
        previous = bind.execute(
            text("""
                SELECT version FROM agent_configs
                 WHERE agent_id = :agent_id AND tenant_id = :tenant_id
                   AND organization_id = :organization_id AND version < :version
                 ORDER BY version DESC LIMIT 1
            """),
            {**scope, "version": row.version},
        ).scalar()
        if previous is None:
            # Nothing to fall back to -- leave the row active rather than
            # deactivating it and leaving the scope with NO active config,
            # which would hide the agent entirely.
            continue

        bind.execute(
            text("""
                UPDATE agent_configs SET is_active = FALSE, updated_by = :actor
                 WHERE agent_id = :agent_id AND tenant_id = :tenant_id
                   AND organization_id = :organization_id AND is_active
            """),
            {**scope, "actor": SYSTEM_ACTOR},
        )
        bind.execute(
            text("""
                UPDATE agent_configs
                   SET is_active = TRUE, activated_at = now(), updated_by = :actor
                 WHERE agent_id = :agent_id AND tenant_id = :tenant_id
                   AND organization_id = :organization_id AND version = :version
            """),
            {**scope, "version": previous, "actor": SYSTEM_ACTOR},
        )
