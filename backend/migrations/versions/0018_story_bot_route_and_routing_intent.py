"""Point Record Stories at its own bot, and give both Mitra agents an intent grid

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-14 10:00:00.000000

A MERGE OF TWO MIGRATIONS OVER OVERLAPPING ROWS
-----------------------------------------------
This was two revisions: one moved `record_stories` onto its own bot, the next
seeded a `routing.intent` grid for both `record_stories` and
`capture_discussion`. Neither had been applied to a deployed database. They are
collapsed here into one pass per agent -- `record_stories` gains both edits,
`capture_discussion` gains only the grid, which is exactly what the two-step
chain produced. The two edits are independent and are guarded independently, so
a row whose route has already drifted still gets its grid.

PART 1 -- THE STORY BOT ROUTE
==============================

WHAT THIS CHANGES
-----------------
`record_stories` drove `/guided_guest`, a GUEST interview whose opening steps
collect the practitioner's name and profile details. Saarthi users are already
logged in against ELEVATE, so those questions are redundant. It now drives
`/saarthi_story_flow`, a bot dedicated to the logged-in Saarthi flow with the
profile questions removed on the Mitra side.

The route is the value Mitra's own resolver answers with. Verified against QA:

    GET /api/flow-connection-info/?flow_route=saarthi_story_flow
    {"flow_route": "saarthi_story_flow", "websocket_url": "ws/common/",
     "bot_route": "/saarthi_story_flow", "create_story": "all"}

so the agent is pointed at exactly the bot the portal URL
(.../mohini/common-chat?flow=saarthi_story_flow) resolves to, on the SAME API
host and the SAME `ws/common/` socket this agent already uses. `create_story:
"all"` is why `produces_artifact` stays true.

This is the same move 0016 made for `capture_discussion`, and the same four
fields are deliberately left alone. THE ROUTE IS THE ONLY EDIT ON THIS HALF.

WHAT IS DELIBERATELY NOT TOUCHED
--------------------------------
`flow_name` STAYS 'guest-mi-story'. The bot and the flow are separate keys on
separate Mitra tables: `bot_route` picks the CompanyBot (resolved as
`CompanyBot.objects.get(company=..., route=...)`, which is why the company must
match too), while `flow_name` picks the story branch at finalisation. v1
`/api/end-story/` resolves the story bot from the SessionFlowName enum and picks
the PDF renderer from this value; any other value falls through to the generic
path, which renders an empty template into a valid, downloadable, COMPLETELY
BLANK PDF -- a 200 from every call and nothing logged anywhere. The guard below
REQUIRES the current value, so a row that has drifted is skipped rather than
half-converted.

`finalize_path` STAYS '/api/end-story/' (v1). Mitra has no Flow row for
'guest-mi-story', and v2 resolves the story bot with
`Flow.objects.get(flow_route=flow)` -- a missing row surfaces as HTTP 500, not
404, so every finalisation would fail deterministically. Move to v2 only
together with a Mitra-side Flow row.

`finalize_as_guest` STAYS false. This agent sends its token in the v1 body; the
sibling discussion agent sends `access_token: null`. Mitra derives
`auth = access_token is not None` and picks the PDF template's user_type from
it, so unifying these two renders a blank PDF for one of them.

`send_user_profile` IS NOT ADDED, AND THAT IS THE REQUIREMENT
-------------------------------------------------------------
Passing the caller's ELEVATE profile would be the obvious way to stop a bot
asking who the user is, and it is exactly what the discussion agent tried and
0016 now records as reverted. It cannot be used here, for a reason that is about
the SIBLING agent:

Mitra resolves a profile by (email, company) -- `post_profile` does
`Profile.objects.filter(email=email, company=company).first()`. `record_stories`
and `capture_discussion` carry the SAME company ('shikshalokamstaging') and the
same derived email, so they share ONE Profile row. Writing `first_name` from
this agent is therefore also visible to the discussion agent, and Mitra reads a
non-empty first_name as "we already know this person"
(chatbot/consumers/async_consumer.py, create_chat_session):

    step_number = 1
    if profile and profile.first_name and profile.first_name != '':
        challenges_step = CompanyStateMachine.objects.get(
            company_bot=..., name="CHALLENGES")
        step_number = challenges_step.step        # skips steps 1-5

Steps 1-5 are what populate `story.other_params`, which is where the
minutes-of-meeting report reads location, organization, participants_count,
discussion_date, district, village, pri_member and school_representative. So
opting in here would silently re-break Capture Discussion's report -- the
regression 0016 exists to prevent.

The profile questions are removed on the MITRA side instead, in the
`/saarthi_story_flow` bot's own configuration, which costs the sibling agent
nothing. Saarthi already identifies the logged-in user to Mitra through the
derived email (`MitraProvider.open_session`), so the interview still runs as
that user with their own profile and story history; only the extra ELEVATE
columns are withheld.

If the richer profile is wanted later, the safe route is to give this agent its
OWN Mitra `company` slug -- which isolates its Profile row -- and only then set
`send_user_profile`. Enabling the flag on the shared row is the unsafe route.

IN-FLIGHT SESSIONS. `MitraProvider.open_session` re-supplies `bot_route` from
the current config on EVERY turn, so a story interview that is mid-flight when
this runs keeps its `remote_session_id` but starts handshaking with the new bot,
while Mitra's ChatSession row still references the old CompanyBot. Apply during
a quiet window, or sweep open story sessions afterwards. Recorded in
backend/docs/runbook.md.

PART 2 -- THE ROUTING INTENT GRID
==================================
`routing.intent` gives the router a verb x noun grid per agent, keyed by AGENT
KEY rather than by provider: these two agents share a provider (mitra) but ask
about different subjects, and the whole point of the grid is that they are
distinguishable. `max_distance` bounds how far apart the verb and noun may sit
in the user's sentence before the match stops counting.

WHY 0010 IS NOT EDITED
======================
Same rule as 0013 and 0015: history is append-only for anything already applied.
0010 records what was seeded then and its bot_route literal stays correct as a
record of that. Editing it would also BREAK THE FRESH-DATABASE PATH -- the guard
below keys on the old route, so a database seeded with the new one would match
nothing here. Doing all the work in one place means a fresh `make migrate` and an
upgrade of a live database land on the identical state.

SCOPE-AWARE and IDEMPOTENT, for the reasons 0015 spells out: a tenant with its
own config must move too, and a re-run must not churn versions -- guaranteed
here by comparing the computed config against the stored one.
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

#: The agent whose bot route moves. `capture_discussion` is on the same provider
#: and the same Mitra deployment and differs by exactly one route and one flow
#: name -- which is precisely why the guard below names all three fields.
STORY_AGENT_KEY = "record_stories"

OLD_BOT_ROUTE = "/guided_guest"
NEW_BOT_ROUTE = "/saarthi_story_flow"

#: Must stay as it is. See the module docstring: this is the value that selects
#: the story PDF renderer, and changing it produces a blank PDF with no error.
FLOW_NAME = "guest-mi-story"

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
    """Both of this migration's edits to one config, as a pure function.

    MODULE-LEVEL AND PUBLIC so the tests that pin the effective `record_stories`
    and `capture_discussion` specs can compose 0010 -> 0013 -> ... -> here and
    validate the result. A closure inside upgrade() would have forced those
    tests to restate the edit, which is how a pin comes to assert something the
    migration no longer does.

    Returns the config UNCHANGED when neither edit applies, which is what makes
    upgrade() idempotent.

    THE TWO EDITS ARE GUARDED INDEPENDENTLY. A record_stories row whose route
    has already drifted off /guided_guest still gains its intent grid; it simply
    does not get the route rewritten.
    """
    config = dict(config)

    # -- Part 1: the story bot route, record_stories only ------------------
    #
    # THREE CONDITIONS, NOT ONE. The agent key alone would be enough today, but
    # the others make it impossible for this to touch capture_discussion or
    # saathi if the catalogue grows a second story agent -- and requiring the
    # CURRENT route makes the edit a no-op on a re-run.
    if agent_key == STORY_AGENT_KEY:
        remote = dict(config.get("remote") or {})
        options = dict(remote.get("options") or {})
        if (
            remote.get("provider") == "mitra"
            and remote.get("flow_name") == FLOW_NAME
            and options.get("bot_route") == OLD_BOT_ROUTE
        ):
            options["bot_route"] = NEW_BOT_ROUTE
            remote["options"] = options
            config["remote"] = remote

    # -- Part 2: the intent grid, for whichever agent has one --------------
    intent = INTENT_BY_AGENT.get(agent_key)
    if intent is not None:
        routing = dict(config.get("routing") or {})
        # An operator's own grid is never overwritten.
        if not routing.get("intent"):
            routing["intent"] = dict(intent)
            config["routing"] = routing

    return config


def canonical(spec: Dict[str, Any]) -> Tuple[str, str]:
    """The exact bytes and checksum `app.domain.agent_spec.canonical_json`
    produces, reproduced with the standard library so this migration keeps
    working when that function moves."""
    payload = json.dumps(spec, separators=(",", ":"), sort_keys=True)
    return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
            existing = dict(row.config)
            updated = apply(existing, agent_key)
            if updated == existing:
                continue  # nothing to change -- no version churn on a re-run
            _rewrite(bind, row, updated)


def downgrade() -> None:
    """Re-activate the version that preceded this migration, per scope.

    A re-activation rather than a reverse edit -- the point of writing a new
    version instead of mutating one. One step undoes the route move and the
    intent grid together, which is the other half of what merging them bought.
    """
    from sqlalchemy import text

    bind = op.get_bind()

    for agent_key in INTENT_BY_AGENT:
        rows = bind.execute(
            text("""
                SELECT c.agent_id, c.tenant_id, c.organization_id, c.version
                  FROM agent_configs c
                  JOIN agents a ON a.id = c.agent_id
                 WHERE c.is_active
                   AND a.key = :agent_key
                   AND c.config->'routing'->'intent' IS NOT NULL
            """),
            {"agent_key": agent_key},
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
