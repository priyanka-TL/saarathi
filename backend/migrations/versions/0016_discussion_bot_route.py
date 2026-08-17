"""Point Capture Discussions at its own bot, without naming the caller

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-13 12:00:00.000000

A MERGE OF TWO MIGRATIONS THAT CONTRADICTED EACH OTHER
------------------------------------------------------
This was two revisions: one moved the bot route AND opted in to
`remote.options.send_user_profile`; the next took that opt-in straight back out,
one live observation later. Neither had been applied to a deployed database, so
they are collapsed here into the state they actually agreed on -- the route
moves, the flag is off. The history of WHY the flag is off is preserved below,
because it is the part that is easy to undo by accident.

WHAT THIS CHANGES
-----------------
`capture_discussion` drove `/shikshalokam_chaupal`, which is not Saarthi's bot:
the same route serves the Mitra web portal and the WhatsApp bot service, so its
prompts cannot move without moving theirs. It now drives
`/saarthi_discussion_flow`, a bot dedicated to this client. Mitra's own
/api/flow-connection-info/?flow_route=saarthi_discussion_flow answers
`{"bot_route": "/saarthi_discussion_flow"}`, so the agent is pointed at exactly
the bot that URL resolves to.

WHY `send_user_profile` IS WRITTEN AS AN EXPLICIT FALSE
-------------------------------------------------------
Opting in made the profile upsert carry the caller's ELEVATE profile, which
meant Saarthi wrote `Profile.first_name` -- and Mitra reads a non-empty
first_name as "we already know this person"
(chatbot/consumers/async_consumer.py, create_chat_session):

    step_number = 1
    if profile and profile.first_name and profile.first_name != '':
        challenges_step = CompanyStateMachine.objects.get(
            company_bot=..., name="CHALLENGES")
        step_number = challenges_step.step        # skips steps 1-5

Observed live on 13 Aug: profile 400 was written with first_name='PP' and the
discussion that followed opened at current_step=6, with the AUTOSTART MESSAGE
recorded as the CHALLENGES answer and the bot going straight to SOLUTIONS.

Steps 1-5 are what populate `story.other_params`, and other_params is where
chatbot/pdf/shiksha_chaupal/mom_report.py::get_user_details reads everything
except the author -- location, organization, participants_count,
discussion_date, district, village, pri_member, school_representative. A
discussion that ran the full intro carries all of them; one that skips it
carries none. So the flag bought a reliable author line and cost four sections
of the minutes-of-meeting report to get it.

Passing user context to Discussion Capture is still wanted, but it cannot be
done by writing first_name while Mitra owns that skip. It needs a Mitra-side
change -- either the skip does not apply to this bot (whose intro is precisely
what collects the report fields), or get_user_details falls back to the Profile
for location/org. Then this flag goes back on and is purely additive.

The flag is stored as an explicit `False` rather than left absent. Both behave
identically at runtime -- MitraOptions.send_user_profile already defaults to
False -- but a stored false records a DECISION, and this file is the reason it
was made.

`flow_name` IS DELIBERATELY NOT TOUCHED, AND THAT IS THE WHOLE RISK
-------------------------------------------------------------------
Mitra's v1 `/api/end-story/` branches on `flow == 'guest-discussion'` to render
the MOM report: create_story_object -> save_chaupal_report -> get_mom_report_html.
Any other value falls through to the generic path, which renders an empty
template and hands it to Gotenberg -- producing a valid, downloadable,
COMPLETELY BLANK PDF, with a 200 from every call and nothing logged anywhere.

The bot and the flow are separate keys on separate Mitra tables: `bot_route`
picks the CompanyBot (resolved as `CompanyBot.objects.get(company=..., route=...)`,
which is why the company must match too), while `flow_name` picks the story
branch at finalisation. Only the first is moving. The predicate below requires
the second to still be 'guest-discussion', so a row that has drifted is skipped
rather than half-converted.

PREREQUISITE, AND IT IS NOT CHECKABLE FROM HERE
-----------------------------------------------
`/saarthi_discussion_flow` must already exist as an active CompanyBot under
company `shikshalokamstaging` on the Mitra deployment this scope points at. A
route that does not resolve fails at socket-open time, on the user's first turn,
not here. Verify before applying; `scripts/verify_discussion_report.py` checks
the other half (that finalisation still renders a MOM report, not a blank one).

WHY 0010 IS NOT EDITED
----------------------
Same rule as 0013 and 0015: history is append-only for anything already applied.
0010 records what was seeded then and its bot_route literal stays correct as a
record of that. Editing it would also BREAK THE FRESH-DATABASE PATH -- the
predicate below keys on the old route, so a database seeded with the new one
would match nothing here and never get the flag written. Doing all the work in
one place means a fresh `make migrate` and an upgrade of a live database land on
the identical state.

SCOPE-AWARE and IDEMPOTENT, for the reasons 0015 spells out: a tenant with its
own capture_discussion config must move too, and a re-run must not churn
versions -- which requiring the OLD route guarantees, since a converted row no
longer matches.
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

#: The one agent this migration may touch. `record_stories` is on the same
#: provider and the same Mitra deployment, and differs by exactly one route and
#: one flow name -- which is precisely why the predicate below names all four.
AGENT_KEY = "capture_discussion"

OLD_BOT_ROUTE = "/shikshalokam_chaupal"
NEW_BOT_ROUTE = "/saarthi_discussion_flow"

#: Must stay as it is. See the module docstring: this is the value that selects
#: the MOM renderer, and changing it produces a blank PDF with no error.
FLOW_NAME = "guest-discussion"


def apply(config: Dict[str, Any]) -> Dict[str, Any]:
    """This migration's edit to one config, as a pure function.

    MODULE-LEVEL AND PUBLIC so the test that pins the effective
    `capture_discussion` spec can compose 0010 -> 0013 -> here and validate the
    result, exactly as tests/unit/test_capture_discussion_config.py already
    composes the first two. A closure inside upgrade() would have forced that
    test to restate the edit, which is how a pin comes to assert something the
    migration no longer does.
    """
    config = dict(config)
    remote = dict(config.get("remote") or {})
    options = dict(remote.get("options") or {})
    options["bot_route"] = NEW_BOT_ROUTE
    # EXPLICIT FALSE, not an absent key -- see the module docstring.
    options["send_user_profile"] = False
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
    # record_stories or saathi if the catalogue grows a second discussion agent
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
    /shikshalokam_chaupal and omits send_user_profile, so both halves of this
    migration are undone by the same step.
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
