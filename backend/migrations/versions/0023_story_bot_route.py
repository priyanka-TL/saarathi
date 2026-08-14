"""Point Record Stories at its own logged-in bot

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-14 10:00:00.000000

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

This is the same move 0019 made for `capture_discussion`, and the same four
fields are deliberately left alone. THE ROUTE IS THE ONLY EDIT.

WHAT IS DELIBERATELY NOT TOUCHED
--------------------------------
`flow_name` STAYS 'guest-mi-story'. The bot and the flow are separate keys on
separate Mitra tables: `bot_route` picks the CompanyBot (resolved as
`CompanyBot.objects.get(company=..., route=...)`, which is why the company must
match too), while `flow_name` picks the story branch at finalisation. v1
`/api/end-story/` resolves the story bot from the SessionFlowName enum and picks
the PDF renderer from this value; any other value falls through to the generic
path, which renders an empty template into a valid, downloadable, COMPLETELY
BLANK PDF -- a 200 from every call and nothing logged anywhere. The predicate
below REQUIRES the current value, so a row that has drifted is skipped rather
than half-converted.

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
asking who the user is, and it is exactly what 0019 tried and 0020 reverted.
It cannot be used here, for a reason that is about the SIBLING agent:

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
regression 0020 exists to undo.

The profile questions are removed on the MITRA side instead, in the
`/saarthi_story_flow` bot's own configuration, which costs the sibling agent
nothing. Saarthi already identifies the logged-in user to Mitra through the
derived email (`MitraProvider.open_session`), so the interview still runs as
that user with their own profile and story history; only the extra ELEVATE
columns are withheld.

If the richer profile is wanted later, the safe route is to give this agent its
OWN Mitra `company` slug -- which isolates its Profile row -- and only then set
`send_user_profile`. Enabling the flag on the shared row is the unsafe route.

WHY 0010 IS NOT EDITED
----------------------
Same rule as 0013, 0015, 0016 and 0019: history is append-only. 0010 records
what was seeded then and its bot_route literal stays correct as a record of
that. Editing it would also BREAK THE FRESH-DATABASE PATH -- the predicate below
keys on the old route, so a database seeded with the new one would match nothing
here. Doing all the work in one place means a fresh `make migrate` and an
upgrade of a live database land on the identical state.

IN-FLIGHT SESSIONS. `MitraProvider.open_session` re-supplies `bot_route` from
the current config on EVERY turn, so a story interview that is mid-flight when
this runs keeps its `remote_session_id` but starts handshaking with the new bot,
while Mitra's ChatSession row still references the old CompanyBot. Apply during
a quiet window, or sweep open story sessions afterwards. Recorded in
backend/docs/runbook.md.

SCOPE-AWARE and IDEMPOTENT, for the reasons 0015, 0016 and 0019 spell out: a
tenant with its own record_stories config must move too, and a re-run must not
churn versions.
"""
import hashlib
import json
from typing import Any, Dict, Sequence, Tuple, Union

from alembic import op

revision: str = "0023"
down_revision: Union[str, Sequence[str], None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SYSTEM_ACTOR = "system"

#: The one agent this migration may touch. `capture_discussion` is on the same
#: provider and the same Mitra deployment, and differs by exactly one route and
#: one flow name -- which is precisely why the predicate below names all four.
AGENT_KEY = "record_stories"

OLD_BOT_ROUTE = "/guided_guest"
NEW_BOT_ROUTE = "/saarthi_story_flow"

#: Must stay as it is. See the module docstring: this is the value that selects
#: the story renderer at finalisation, and changing it produces a blank PDF with
#: no error.
FLOW_NAME = "guest-mi-story"


def apply(config: Dict[str, Any]) -> Dict[str, Any]:
    """This migration's edit to one config, as a pure function.

    MODULE-LEVEL AND PUBLIC so the test that pins the effective `record_stories`
    spec can compose 0010 -> 0013 -> here and validate the result, exactly as
    tests/unit/test_capture_discussion_config.py composes the sibling chain. A
    closure inside upgrade() would have forced that test to restate the edit,
    which is how a pin comes to assert something the migration no longer does.

    THE ROUTE IS THE ONLY KEY WRITTEN. `send_user_profile` is deliberately not
    added -- see the module docstring for why that is about the sibling agent's
    report and not about this one.
    """
    config = dict(config)
    remote = dict(config.get("remote") or {})
    options = dict(remote.get("options") or {})
    options["bot_route"] = NEW_BOT_ROUTE
    remote["options"] = options
    config["remote"] = remote
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

    bind = op.get_bind()

    # FOUR CONDITIONS, NOT ONE. The agent key alone would be enough today, but
    # the other three are what make it impossible for this to touch
    # capture_discussion or saathi if the catalogue grows a second story agent
    # -- and requiring the CURRENT route makes the whole statement a no-op on a
    # re-run, rather than something that needs a separate guard.
    rows = bind.execute(
        text("""
            SELECT c.agent_id, c.tenant_id, c.organization_id, c.config
              FROM agent_configs c
              JOIN agents a ON a.id = c.agent_id
             WHERE c.is_active
               AND a.key = :agent_key
               AND c.config->'remote'->>'provider' = 'mitra'
               AND c.config->'remote'->>'flow_name' = :flow_name
               AND c.config->'remote'->'options'->>'bot_route' = :old_route
        """),
        {"agent_key": AGENT_KEY, "flow_name": FLOW_NAME, "old_route": OLD_BOT_ROUTE},
    ).fetchall()

    for row in rows:
        _rewrite(bind, row, apply)


def downgrade() -> None:
    """Re-activate the version that preceded the route change.

    A re-activation rather than a reverse edit -- the point of writing a new
    version instead of mutating one. The previous config is the one that names
    /guided_guest.
    """
    from sqlalchemy import text

    bind = op.get_bind()

    rows = bind.execute(
        text("""
            SELECT c.agent_id, c.tenant_id, c.organization_id, c.version
              FROM agent_configs c
              JOIN agents a ON a.id = c.agent_id
             WHERE c.is_active
               AND a.key = :agent_key
               AND c.config->'remote'->'options'->>'bot_route' = :new_route
        """),
        {"agent_key": AGENT_KEY, "new_route": NEW_BOT_ROUTE},
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
