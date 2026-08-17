"""Give every LLM agent a max_tokens ceiling

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-13 19:00:00.000000

WHAT THIS FIXES
---------------
POST /api/chat answered 502 on a FUNDED OpenRouter account.

`model.max_tokens` was null on every LLM agent, and null does not mean "no
limit" to OpenRouter -- it means "reserve credit for the model's entire context
window". A reply that would really be a few hundred tokens was therefore priced
at 65,536 and refused:

    code: 402, limit_source: openrouter_credits
    "You requested up to 65536 tokens, but can only afford 60029"

Verified against the live key: the identical request with `max_tokens: 1024`
succeeds, with none at all returns 402. The two numbers in one log line make the
mechanism plain -- the same key at the same moment could afford 60,029 tokens of
qwen and 3,121 of gemini, i.e. remaining credit divided by each model's rate.

The whole turn died because BOTH LLM paths hit it: RouterService's classifier
402s (fixed in code, app/services/router_service.py::ROUTER_MAX_TOKENS -- it is
not a config row, so it cannot be fixed here), the turn falls back to the
default agent, and that 402s too.

WHY 2048
--------
This is a chat agent whose answers are prose, so the ceiling has to be
comfortable rather than tight -- roughly 1,500 words. It bounds the REQUEST, not
the answer: nothing observed comes close, and the point is to stop a few-hundred
token reply being priced as if it were sixty-five thousand.

Topping the account up would also make the symptom disappear, which is exactly
why this is worth fixing properly -- it would come straight back the next time
the balance dipped below the cost of a full-window completion, and the failure
names credits rather than the unbounded request that caused it.

SELECTS ON THE VALUE, not just the key: an agent that already carries a ceiling
is left alone, so a re-run churns no versions and a deliberately-tuned agent is
never overwritten.
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

#: Comfortable for prose, and two orders of magnitude below the 65,536 that was
#: being reserved. See the module docstring.
MAX_TOKENS = 2048


def apply(config: Dict[str, Any]) -> Dict[str, Any]:
    """This migration's edit to one config, as a pure function.

    Module-level and public, like 0019.apply() and 0020.apply(), so a test can
    compose the migration chain instead of restating its end state.
    """
    config = dict(config)
    model = dict(config.get("model") or {})
    model["max_tokens"] = MAX_TOKENS
    config["model"] = model
    return config


def canonical(spec: Dict[str, Any]) -> Tuple[str, str]:
    """The exact bytes and checksum `app.domain.agent_spec.canonical_json`
    produces, reproduced with the standard library so this migration keeps
    working when that function moves."""
    payload = json.dumps(spec, separators=(",", ":"), sort_keys=True)
    return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _rewrite(bind, row, mutate) -> None:
    """Deactivate this row's active config and insert `mutate(config)` as the
    next version, in the row's OWN scope.

    DEACTIVATE BEFORE INSERT: uq_agent_configs_one_active is a partial unique
    index and is not DEFERRABLE. Scoped to this row's own
    (tenant_id, organization_id) -- an unscoped deactivate would clear every
    other scope's active row too.
    """
    from sqlalchemy import text

    config = mutate(dict(row.config))
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

    # EVERY llm agent, not one named key: the defect is the absent ceiling, and
    # any agent carrying `model` without one has it. `remote_flow` agents have
    # no `model` block at all and cannot match.
    #
    # `->>` AND NOT `->`. The seed writes `"max_tokens": None`, so the key is
    # PRESENT holding JSON null -- `jsonb_typeof(...) = 'null'`. `->` returns
    # that JSON null as a jsonb value, which is not SQL NULL, so `-> ... IS
    # NULL` is FALSE and this statement silently matched nothing on the first
    # attempt. `->>` casts to text and yields SQL NULL for both spellings, so it
    # catches an absent key and an explicit null alike.
    rows = op.get_bind().execute(
        text("""
            SELECT c.agent_id, c.tenant_id, c.organization_id, c.config
              FROM agent_configs c
             WHERE c.is_active
               AND c.config ? 'model'
               AND c.config->'model'->>'max_tokens' IS NULL
        """)
    ).fetchall()

    for row in rows:
        _rewrite(op.get_bind(), row, apply)


def downgrade() -> None:
    """Re-activate the version that carried no ceiling.

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
               AND c.config->'model'->>'max_tokens' = :value
        """),
        {"value": str(MAX_TOKENS)},
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
