"""Stop naming the caller to Mitra on the discussion flow

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-13 16:00:00.000000

WHAT THIS UNDOES, AND WHAT IT KEEPS
-----------------------------------
0019 did two independent things. The bot move (/shikshalokam_chaupal ->
/saarthi_discussion_flow) is CORRECT and stays -- Mitra's own
/api/flow-connection-info/?flow_route=saarthi_discussion_flow answers
`{"bot_route": "/saarthi_discussion_flow"}`, so the agent is pointed at exactly
the bot that URL resolves to.

`send_user_profile` is the half that goes. It made the profile upsert carry the
caller's ELEVATE profile, which meant Saarthi wrote `Profile.first_name` -- and
Mitra reads a non-empty first_name as "we already know this person"
(chatbot/consumers/async_consumer.py, create_chat_session):

    step_number = 1
    if profile and profile.first_name and profile.first_name != '':
        challenges_step = CompanyStateMachine.objects.get(
            company_bot=..., name="CHALLENGES")
        step_number = challenges_step.step        # skips steps 1-5

WHY THAT IS WORSE THAN THE PROBLEM IT SOLVED
--------------------------------------------
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

THE CONFIG IS ONLY HALF THE FIX. Mitra has ALREADY STORED first_name='PP' on
profile 400, and the skip reads the stored value -- not what we send. Clearing
it is a write to Mitra's own database and is therefore a manual step, recorded
in backend/docs/runbook.md rather than performed here: this migration owns
Saarthi's config and nothing else.

SCOPE-AWARE and IDEMPOTENT, like 0016 and 0019: a tenant with its own
capture_discussion config must move too, and a re-run must not churn versions --
which the `send_user_profile = 'true'` predicate guarantees, since a converted
row no longer matches.
"""
import hashlib
import json
from typing import Any, Dict, Sequence, Tuple, Union

from alembic import op

revision: str = "0020"
down_revision: Union[str, Sequence[str], None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SYSTEM_ACTOR = "system"

#: The one agent this migration may touch. `record_stories` is on the same
#: provider and the same client and never opted in, so it cannot match the
#: predicate below -- but naming the key keeps that true if it ever does.
AGENT_KEY = "capture_discussion"


def apply(config: Dict[str, Any]) -> Dict[str, Any]:
    """This migration's edit to one config, as a pure function.

    MODULE-LEVEL AND PUBLIC for the same reason 0019.apply() is: the test that
    pins the effective capture_discussion spec composes 0010 -> 0013 -> 0019 ->
    here and validates the result, so the pin cannot assert something the
    migration no longer does.

    Written as an explicit `False` rather than by deleting the key. Both behave
    identically at runtime -- MitraOptions.send_user_profile already defaults to
    False -- but a stored false records a DECISION, and it is what makes the
    downgrade a straight re-activation of the version that carries true.
    """
    config = dict(config)
    remote = dict(config.get("remote") or {})
    options = dict(remote.get("options") or {})
    options["send_user_profile"] = False
    # bot_route IS NOT TOUCHED. 0019's other half is correct and stays.
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

    # Requiring the CURRENT value makes the statement a no-op on a re-run
    # rather than something that needs a separate guard, and makes it
    # impossible to touch an agent that never opted in.
    rows = bind.execute(
        text("""
            SELECT c.agent_id, c.tenant_id, c.organization_id, c.config
              FROM agent_configs c
              JOIN agents a ON a.id = c.agent_id
             WHERE c.is_active
               AND a.key = :agent_key
               AND c.config->'remote'->'options'->>'send_user_profile' = 'true'
        """),
        {"agent_key": AGENT_KEY},
    ).fetchall()

    for row in rows:
        _rewrite(bind, row, apply)


def downgrade() -> None:
    """Re-activate the version that still names the caller.

    A re-activation rather than a reverse edit -- the point of writing a new
    version instead of mutating one. Note this restores the CONFIG only; if the
    manual Mitra-side first_name clear was performed, that is not undone here
    and does not need to be. The flag alone re-creates the behaviour.
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
               AND c.config->'remote'->'options'->>'send_user_profile' = 'false'
        """),
        {"agent_key": AGENT_KEY},
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
