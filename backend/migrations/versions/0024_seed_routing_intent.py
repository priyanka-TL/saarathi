"""Give the two interview agents a routing.intent grid

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-14 12:00:00.000000

WHAT THIS FIXES
---------------
`saathi` sets `routing.yields_to_keyword`, so every pinned turn ran
`RouterService._yield_from_pin`. Its keyword step was a raw substring test, so
an inserted article defeated it and the turn fell through to an LLM classifier
-- one OpenRouter round trip, serialised in front of Saathi's own WebSocket
turn, on EVERY turn of every Saathi conversation.

The matcher itself is fixed in code (app/services/intent_match.py): keyword
phrases now match as ordered token subsequences with filler-only gaps, so
"capture a story" matches the keyword "capture story". That alone does not cover
the other half of the problem -- phrasings no fixed phrase list contains at all:

    "I want to start a discussion"   matches NONE of capture_discussion's
                                     six seeded keywords

and no realistic amount of adding phrases converges, because the cross product
of verbs and spacings is unbounded. `routing.intent` states the verbs and nouns
once and lets the matcher form the product.

WHY ONLY THESE TWO AGENTS
-------------------------
They are the yield TARGETS -- the agents a pinned Saathi conversation can be
handed to. `saathi` itself and `general_support` are excluded from the candidate
set in `_yield_from_pin` (the pinned agent cannot switch to itself, and the
default agent is never a yield target), so a grid on either would never be read
on that path.

The grid IS also read by Gate 3, the first-message keyword pre-route, which is
the intended second benefit: a first message like "I want to start a discussion"
now routes deterministically instead of paying the Gate 4 classifier.

WHY THE VERB AND NOUN LISTS ARE WHAT THEY ARE
---------------------------------------------
The nouns are the SUBJECT of each agent and are deliberately few: they are also
what the ambiguity gate (`RouterService._could_be_a_request_for`) tests, so a
loose noun list would re-open the LLM call on ordinary interview answers, which
is the cost this whole change exists to remove. "notes" is absent from the
discussion nouns for exactly that reason -- it is an ordinary word in an
interview answer, and "meeting notes" is already a keyword.

The verbs are open-class and safe to be generous with: a verb alone never
matches -- `intent_match` requires a verb followed by a noun within
`max_distance` tokens, in that order.

max_distance is WRITTEN OUT even though 4 is `IntentSpec`'s default, and that is
deliberate. The checksum is computed over the raw config dict, so a row that
omits the field takes whatever the model default happens to be AT READ TIME:
changing that default later would silently alter how every seeded agent routes,
with no new config version, no checksum change and nothing in the audit log. The
value 4 is chosen because "record a really long story" puts three tokens between
verb and noun, and beyond that the two are usually not in the same clause.

WHY A NEW VERSION AND NOT AN EDIT
---------------------------------
Same rule as 0013, 0015 and 0016: a config change is a new active version, never
an in-place UPDATE, so the previous config stays one activate call away.

SCOPE-AWARE and IDEMPOTENT, for the reasons 0015 spells out: a tenant with its
own config for either agent would otherwise keep the old routing, and a re-run
must not churn versions.
"""
import hashlib
import json
from typing import Any, Dict, Sequence, Tuple, Union

from alembic import op

revision: str = "0024"
down_revision: Union[str, Sequence[str], None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SYSTEM_ACTOR = "system"

#: agent key -> the intent grid it gains.
#:
#: KEYED BY AGENT KEY, not by provider, because these two agents share a
#: provider (mitra) but ask about different subjects -- the whole point of the
#: grid is that they are distinguishable.
INTENT_BY_AGENT: Dict[str, Dict[str, Any]] = {
    "record_stories": {
        "verbs": [
            "record", "capture", "share", "tell", "write",
            "submit", "create", "narrate", "start", "begin",
        ],
        # "experience" alongside "story": users describe what they did as an
        # experience at least as often as they call it a story.
        "nouns": ["story", "experience"],
        "max_distance": 4,
    },
    "capture_discussion": {
        "verbs": [
            "record", "capture", "summarise", "summarize",
            "note", "start", "begin", "document",
        ],
        # "chaupal" is the local name for the gathering and is already a
        # keyword; it earns a place here too because it is the one noun that is
        # unambiguous on its own.
        "nouns": ["discussion", "meeting", "chaupal", "minutes"],
        "max_distance": 4,
    },
}


def apply(config: Dict[str, Any], agent_key: str) -> Dict[str, Any]:
    """This migration's edit to one config, as a pure function.

    MODULE-LEVEL AND PUBLIC so the tests that pin the effective `record_stories`
    and `capture_discussion` specs can compose 0010 -> 0013 -> ... -> here and
    validate the result, exactly as they already compose the earlier ones. A
    closure inside upgrade() would have forced those tests to restate the edit,
    which is how a pin comes to assert something the migration no longer does.

    Returns the config UNCHANGED when the agent has no grid to gain or already
    carries one -- the same predicate `upgrade()` uses, so a test composing this
    sees exactly what the database gets.
    """
    intent = INTENT_BY_AGENT.get(agent_key)
    if intent is None:
        return config

    routing = dict(config.get("routing") or {})
    if routing.get("intent"):
        return config

    config = dict(config)
    routing["intent"] = dict(intent)
    config["routing"] = routing
    return config


def canonical(spec: Dict[str, Any]) -> Tuple[str, str]:
    """The exact bytes and checksum `app.domain.agent_spec.canonical_json`
    produces, reproduced with the standard library so this migration keeps
    working when that function moves."""
    payload = json.dumps(spec, separators=(",", ":"), sort_keys=True)
    return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def upgrade() -> None:
    from sqlalchemy import text

    bind = op.get_bind()

    for agent_key in INTENT_BY_AGENT:
        rows = bind.execute(
            text("""
                SELECT c.id, c.agent_id, c.tenant_id, c.organization_id, c.config
                  FROM agent_configs c
                  JOIN agents a ON a.id = c.agent_id
                 WHERE c.is_active AND a.key = :agent_key
            """),
            {"agent_key": agent_key},
        ).fetchall()

        for row in rows:
            config = apply(dict(row.config), agent_key)

            # `apply` is a no-op when the config already carries a grid -- an
            # operator's, or a re-run of this migration. Either way, do not
            # churn a version.
            if config == row.config:
                continue

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
    """Reactivate the version that preceded the intent grid.

    A re-activation rather than a reverse edit -- the point of writing a new
    version instead of mutating one.

    The PREDICATE REQUIRES the grid to be present, so an operator's own later
    version (which this migration would have skipped) is never deactivated by a
    downgrade.
    """
    from sqlalchemy import text

    bind = op.get_bind()

    for agent_key in INTENT_BY_AGENT:
        rows = bind.execute(
            text("""
                SELECT c.agent_id, c.tenant_id, c.organization_id, c.version
                  FROM agent_configs c
                  JOIN agents a ON a.id = c.agent_id
                 WHERE c.is_active AND a.key = :agent_key
                   AND c.config->'routing'->'intent' IS NOT NULL
            """),
            {"agent_key": agent_key},
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
