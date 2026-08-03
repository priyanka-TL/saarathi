"""seed_agents

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-02 22:00:00.000000

REPLACES app/config/agents/*.yaml. The database is now the ONLY source of agent
configuration -- there is no file to sync from, no CONFIG_SYNC_MODE, and no
reconciliation step at startup.

WHY A MIGRATION RATHER THAN A SEED SCRIPT
-----------------------------------------
`make migrate` alone must produce a working application. The previous design
needed a documented "on a fresh database, set CONFIG_SYNC_MODE=safe for the
first run, then put it back" dance, which is exactly the kind of instruction
that gets skipped and then debugged. Migration 0006 already seeds the
capabilities catalogue this way; this is the same pattern for the agents those
capabilities point at.

IDEMPOTENT, AND THAT IS LOAD-BEARING
------------------------------------
Existing databases already hold these three agents, seeded from YAML, quite
possibly with an admin's edits on top. This migration must not overwrite those:

  * `agents` inserts are ON CONFLICT (key) DO NOTHING;
  * a config row is written only when the agent has NO default-scope config, or
    when its active one still contains a `${VAR}` reference.

THE `${VAR}` CASE IS A REAL UPGRADE, NOT A SAFETY NET. The old YAML seed stored
strings like `${OPENROUTER_MODEL:-qwen/qwen3.7-flash}` and expanded them against
os.environ every time the config was loaded. That expansion is removed along
with the YAML -- a config row holds literal values now -- so those rows would
fail validation on the next reload and the registry would come up with zero
agents. Superseding them here is what makes the upgrade survivable. A new
VERSION is inserted rather than an in-place update, so the pre-migration config
stays in the version list and remains one activate call away.

Anything else is somebody's configuration and is left completely alone.

DEPLOYMENT-SPECIFIC VALUES
--------------------------
`remote.company` and `remote.bot_route` below are the QA values. They are
per-deployment, and they are no longer read from the environment -- change them
with one call per environment:

    POST /api/agents/{key}/config   (optionally with tenant_id/organization_id)

which writes a new version, deactivates the old one and leaves an audit row.
Doing that at a TENANT's scope is what lets two tenants use different Mitra
companies; doing it at default scope changes the deployment default.

THE CHECKSUM IS COMPUTED HERE, THE SAME WAY THE APPLICATION COMPUTES IT
----------------------------------------------------------------------
`app.domain.agent_spec.canonical_json` is
`sha256(json.dumps(spec, separators=(',',':'), sort_keys=True))` over the raw
spec dict. It is reproduced with the standard library rather than imported,
because a migration must keep working when the application code it was written
against has moved on. `tests/integration/test_seed_migration.py` asserts the
two agree, so a change to canonical_json cannot drift away from this silently.
"""
from typing import Any, Dict, Sequence, Union
import hashlib
import json

from alembic import op

revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The three shipped agents, verbatim from the YAML files this replaces.
# Comments explaining WHY each non-obvious value is what it is have been kept:
# they are the reason the value must not be "tidied up".
SEED_AGENTS: list[Dict[str, Any]] = [
    {
        "schema_version": 1,
        "key": "record_stories",
        "name": "Record Stories",
        "description": (
            "Guided multi-turn interview that captures a practitioner's improvement story "
            "and produces a written narrative and PDF report. Use when the user wants to "
            "record, capture, narrate or submit a story or field experience."
        ),
        "agent_type": "remote_flow",
        "status": "enabled",
        "sort_order": 10,
        "capabilities": ["guided_interview", "story_capture", "pdf_report", "multilingual"],
        "remote": {
            "provider": "mitra",
            "flow_name": "guest-mi-story",
            # (email, company) identifies the Mitra profile. Per tenant: a
            # tenant-scoped config row with its own company gets its own
            # profiles and its own story history.
            "company": "shikshalokamstaging",
            # Explicit, never inferred from a name.
            "bot_route": "/guided_guest",
            "default_language": "en",
            "supported_languages": ["en", "hi", "kn", "te"],
            "handshake": {"settle_ms": 1500, "timeout_ms": 5000},
            "turn": {
                "first_turn_timeout_ms": 60000,
                "turn_timeout_ms": 45000,
                "idle_gap_ms": 8000,
            },
            "completion_poll_every_turn": True,
            # v1, NOT v2. v2 resolves the story bot via
            # Flow.objects.get(flow_route=flow), and Mitra has no Flow row for
            # 'guest-mi-story' -- only 'guest-discussion' is registered there
            # (verified against QA: GET /api/flow-connection-info/
            # ?flow_route=guest-mi-story returns 404). v2 turns that missing row
            # into an HTTP 500, so every story finalisation failed. v1 resolves
            # the same flow from the SessionFlowName enum -> CompanyBot
            # '/guest-story', which does exist, and is what the Node bot uses
            # for this flow too. Move to v2 once Mitra registers the Flow row.
            "finalize_path": "/api/end-story/",
            "report_media_type": "application/pdf",
        },
        "routing": {
            "keywords": [
                "record a story", "capture story", "share my story", "tell my story",
                "listening at scale", "story capture", "my story",
            ],
            "priority": 90,
            "pin_session": True,
            "exit_keywords": ["/exit", "cancel interview", "stop interview", "start over"],
            "router_selectable": True,
            "direct_selectable": True,
        },
        # mitra owns interview state entirely
        "memory": {"strategy": "none"},
        "limits": {"max_turns": 60, "rate_limit_per_conversation_per_min": 20},
        "retry": {"max_attempts": 2, "retry_on": ["timeout", "connection_error"]},
        "features": {"emit_options": True},
    },
    {
        "schema_version": 1,
        "key": "capture_discussion",
        "name": "Capture Discussions",
        "description": (
            "Guided interview that distils the important points from a community discussion. "
            "Use when the user wants to record, summarise or capture a meeting or group "
            "conversation."
        ),
        "agent_type": "remote_flow",
        "sort_order": 15,
        "capabilities": ["guided_interview", "discussion_capture", "multilingual"],
        "remote": {
            "provider": "mitra",
            "flow_name": "guest-discussion",
            "company": "shikshalokamstaging",
            # NOT the story bot route. These two agents differ by exactly one
            # route and one flow name, and swapping either sends the interview
            # to the wrong Mitra bot with no error -- just the wrong questions.
            "bot_route": "/shikshalokam_chaupal",
            # v1, NOT v2 -- and this one is about the PDF, not about which table
            # resolves the bot. v1 (create_story_object) is the ONLY Mitra path
            # with a chaupal branch: flow == GuestDiscussion -> save_chaupal_report
            # -> get_story_html -> get_mom_report_html, the minutes-of-meeting
            # report. v2 (generate_story) has no such branch: it always runs
            # save_generic_story and renders through get_html_from_template,
            # which RETURNS AN EMPTY STRING when no PDFTemplates row matches this
            # flow -- and save_project_story feeds that empty string straight to
            # Gotenberg. The result is a valid, downloadable, completely BLANK
            # PDF: story created, story_media row present, get-story 200, nothing
            # logged. That is the bug this setting fixes.
            "finalize_path": "/api/end-story/",
            # The discussion flow uses NO token anywhere. MitraChannel._authenticate
            # already sends `access_token: None` on the WebSocket, and Mitra's own
            # client for this flow posts `access_token: null` to end-story.
            # Sending one here made Mitra set auth=True and look up an AUTH-typed
            # PDF template instead of the GUEST one -- a second, independent route
            # to the same empty-template branch above.
            "finalize_as_guest": True,
        },
        "routing": {
            "keywords": [
                "capture discussion", "capture a discussion", "record discussion",
                "meeting notes", "community discussion", "chaupal",
            ],
            "priority": 88,
            "pin_session": True,
            # PHRASES, not the bare RoutingSpec defaults ["/exit","cancel","stop"].
            # Exit matching is exact (RouterService._is_exit), but "stop" and
            # "cancel" are perfectly ordinary one-word answers to this interview's
            # own questions -- and abandoning the session is irreversible while a
            # missed command is trivially recoverable.
            "exit_keywords": ["/exit", "cancel interview", "stop interview", "end discussion"],
            "router_selectable": True,
            "direct_selectable": True,
        },
        "memory": {"strategy": "none"},
        "limits": {"max_turns": 60, "rate_limit_per_conversation_per_min": 20},
        "features": {"emit_options": True},
    },
    {
        "schema_version": 1,
        "key": "general_support",
        "name": "General Support Agent",
        "description": (
            "Handles general inquiries, business hours, and fallback for unrecognized requests."
        ),
        "agent_type": "llm",
        "status": "enabled",
        "default": True,
        "sort_order": 30,
        "capabilities": ["general_qa", "fallback"],
        "prompt": (
            "You are a friendly General Support Assistant. Answer general questions, "
            "provide business hours, or politely ask the user to clarify if you don't "
            "know the answer.\n"
        ),
        "tools": [],
        "model": {
            "provider": "openrouter",
            # Was ${OPENROUTER_MODEL:-...}. A literal now: change it per
            # deployment (or per tenant) through the config API, like everything
            # else. OPENROUTER_MODEL still exists in .env because RouterService
            # reads it for the router's OWN classifier, which is not an agent.
            "name": "qwen/qwen3.7-flash",
            "temperature": 0.0,
            "max_tokens": None,
            "timeout_s": 30,
        },
        "routing": {"keywords": [], "priority": 10, "pin_session": False},
        "memory": {"strategy": "recent", "history_turns": 10, "include_other_agents": True},
        "retry": {"max_attempts": 3},
        "features": {"record_tool_executions": True},
    },
]


def canonical(spec: Dict[str, Any]) -> tuple[str, str]:
    """Mirrors app.domain.agent_spec.canonical_json, standard library only."""
    payload = json.dumps(spec, separators=(",", ":"), sort_keys=True)
    return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_legacy(config_text: str) -> bool:
    """True when a stored config was written for the YAML-era schema.

    Such a row no longer validates, so leaving it active means the registry
    loads zero agents and the application refuses to start. Two shapes, both
    produced by the sync this change removes:

      * a ``${VAR}`` reference -- expanded at load time by machinery that is
        gone, so it is now just a literal string where a number or a company
        slug is expected;
      * ``bot_route_env`` / ``company_env`` -- the environment indirection that
        `bot_route` and `company` replaced. RemoteSpec requires the latter two,
        so a config carrying only the former fails validation outright.

    Deliberately a TEXT test rather than a schema check: this migration does not
    import application code, so that it keeps working when that code moves on.
    """
    if "${" in config_text:
        return True
    try:
        config = json.loads(config_text)
    except ValueError:
        return False
    remote = config.get("remote")
    if not isinstance(remote, dict):
        return False
    if "bot_route_env" in remote or "company_env" in remote:
        return True
    # Required by RemoteSpec now; absent in every config the YAML sync wrote.
    return not (remote.get("bot_route") and remote.get("company"))


def upgrade() -> None:
    from sqlalchemy import text

    bind = op.get_bind()

    for spec in SEED_AGENTS:
        config, checksum = canonical(spec)

        bind.execute(
            text("""
                INSERT INTO agents (tenant_id, organization_id, key, name, description,
                                    agent_type, status, is_default, sort_order)
                VALUES ('default', 'default', :key, :name, :description,
                        CAST(:agent_type AS agent_type_enum),
                        CAST(:status AS agent_status_enum), :is_default, :sort_order)
                ON CONFLICT (key) DO NOTHING
            """),
            {
                "key": spec["key"],
                "name": spec["name"],
                "description": spec["description"],
                "agent_type": spec["agent_type"],
                "status": spec.get("status", "enabled"),
                "is_default": spec.get("default", False),
                "sort_order": spec.get("sort_order", 100),
            },
        )

        # Three cases, and only the first two write anything.
        #
        #   (a) NO default-scope config      -- fresh database: seed v1.
        #   (b) the ACTIVE one still carries a ${VAR} reference -- an existing
        #       database seeded by the old YAML sync. That substitution was
        #       resolved at load time by machinery this change removes, so the
        #       row would now fail validation and the registry would load zero
        #       agents. Supersede it with a new version carrying literals.
        #   (c) anything else -- somebody's configuration. Left alone.
        #
        # (b) inserts a NEW VERSION rather than updating in place, so the
        # pre-migration config stays in the version list and `POST
        # /api/agents/{key}/config/{version}/activate` can roll back to it.
        active = bind.execute(
            text("""
                SELECT c.id, c.version, c.config::text AS config_text
                FROM agent_configs c
                JOIN agents a ON a.id = c.agent_id
                WHERE a.key = :key
                  AND c.tenant_id = 'default' AND c.organization_id = 'default'
                  AND c.is_active
            """),
            {"key": spec["key"]},
        ).fetchone()

        if active is None:
            bind.execute(
                text("""
                    INSERT INTO agent_configs (agent_id, tenant_id, organization_id, version,
                                               source, checksum, config, is_active, activated_at,
                                               created_by)
                    SELECT a.id, 'default', 'default',
                           COALESCE((SELECT MAX(c.version) FROM agent_configs c
                                     WHERE c.agent_id = a.id
                                       AND c.tenant_id = 'default'
                                       AND c.organization_id = 'default'), 0) + 1,
                           'db', CAST(:checksum AS varchar), CAST(:config AS jsonb),
                           TRUE, now(), 'migration-0007'
                    FROM agents a
                    WHERE a.key = CAST(:key AS varchar)
                """),
                {"key": spec["key"], "checksum": checksum, "config": config},
            )
        elif is_legacy(active.config_text or ""):
            # DEACTIVATE BEFORE INSERTING. uq_agent_cfg_one_active is a partial
            # unique index checked per statement with no DEFERRABLE option, so
            # the other order raises a UniqueViolation and aborts the upgrade.
            bind.execute(
                text("UPDATE agent_configs SET is_active = FALSE WHERE id = :id"),
                {"id": active.id},
            )
            bind.execute(
                text("""
                    INSERT INTO agent_configs (agent_id, tenant_id, organization_id, version,
                                               source, checksum, config, is_active, activated_at,
                                               created_by)
                    SELECT a.id, 'default', 'default',
                           (SELECT MAX(c.version) FROM agent_configs c
                            WHERE c.agent_id = a.id
                              AND c.tenant_id = 'default'
                              AND c.organization_id = 'default') + 1,
                           'db', CAST(:checksum AS varchar), CAST(:config AS jsonb),
                           TRUE, now(), 'migration-0007'
                    FROM agents a
                    WHERE a.key = CAST(:key AS varchar)
                """),
                {"key": spec["key"], "checksum": checksum, "config": config},
            )

    # Migration 0006 seeds capability membership only for agents that already
    # exist, and on a fresh database none did -- it runs before this one. Now
    # that they do, fill in the membership it had to skip. Re-running its own
    # statement is safe: ON CONFLICT DO NOTHING, and it only ever touches the
    # default-scope 'listening_at_scale' capability.
    for agent_key, label, order, autostart in (
        ('record_stories', 'Record Stories', 10, 'I want to record a story'),
        ('capture_discussion', 'Capture Discussions', 20, 'I want to capture a discussion'),
    ):
        bind.execute(
            text("""
                INSERT INTO capability_agents (capability_id, agent_id, display_order,
                                               label_override, is_visible, metadata)
                SELECT c.id, a.id, CAST(:display_order AS integer),
                       CAST(:label AS varchar), true,
                       jsonb_build_object('action',
                           jsonb_build_object('type', 'start_agent',
                                              'autostart', CAST(:autostart AS text)))
                FROM capabilities c, agents a
                WHERE c.key = 'listening_at_scale'
                  AND c.tenant_id = 'default' AND c.organization_id = 'default'
                  AND a.key = CAST(:agent_key AS varchar)
                ON CONFLICT (capability_id, agent_id) DO NOTHING
            """),
            {"label": label, "display_order": order, "autostart": autostart,
             "agent_key": agent_key},
        )


def downgrade() -> None:
    """Removes ONLY what this migration inserted.

    Configs written since (any version > 1, or any non-default scope) belong to
    an operator, not to this migration, so an agent that has been edited is left
    entirely alone rather than being partially dismantled.
    """
    from sqlalchemy import text

    bind = op.get_bind()
    keys = tuple(spec["key"] for spec in SEED_AGENTS)

    bind.execute(
        text("""
            DELETE FROM agent_configs c
            USING agents a
            WHERE c.agent_id = a.id
              AND a.key = ANY(:keys)
              AND c.created_by = 'migration-0007'
              AND NOT EXISTS (
                  SELECT 1 FROM agent_configs other
                  WHERE other.agent_id = a.id AND other.id <> c.id
              )
        """),
        {"keys": list(keys)},
    )
    bind.execute(
        text("""
            DELETE FROM agents a
            WHERE a.key = ANY(:keys)
              AND NOT EXISTS (SELECT 1 FROM agent_configs c WHERE c.agent_id = a.id)
        """),
        {"keys": list(keys)},
    )
