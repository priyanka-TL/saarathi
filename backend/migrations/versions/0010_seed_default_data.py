"""Seed the default catalogue: three agents, their configs, two capability cards

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-03 12:00:09.000000

`make migrate` ALONE MUST PRODUCE A WORKING APPLICATION
-------------------------------------------------------
There is no YAML, no config sync and no first-run flag: `agents` +
`agent_configs` are the only source of agent configuration, so if nothing seeds
them the registry loads zero agents and `app/core/bootstrap.py` refuses to
start. This migration is what makes a fresh database bootable.

ONE MIGRATION, NOT THREE
------------------------
The previous schema spread this across three files -- capability cards in one,
agents in another, and the Mitra `remote.connection` block patched in by a
third, after the config rows it had to modify were already written. On a fresh
database that ordering had a visible bug: capability MEMBERSHIP was inserted
before the agents existed, so the FK-guarded INSERT silently matched no rows and
the sidebar card rendered with no buttons under it, until a later migration
re-ran the same statement. Seeding everything here, in dependency order, removes
both the ordering hazard and the patch step: a spec is complete and valid the
moment it is written.

WHY THREE AGENTS WHEN ONLY TWO ARE USER-FACING
----------------------------------------------
`record_stories` and `capture_discussion` are the two Mitra interview agents the
sidebar offers. `general_support` is the routing floor: `RouterService`'s Gate 5
falls back to `registry.default()` so that routing can never fail, and
`uq_agents_single_default` allows exactly one such row. Without it every message
that matched no keyword and no LLM classification would have nowhere to go, and
bootstrap would raise. It is intentionally not a member of any capability card.

DEPLOYMENT-SPECIFIC VALUES
--------------------------
`remote.company`, `remote.bot_route` and the whole `remote.connection` block are
per-deployment. The connection block is read from this deployment's `MITRA_*`
environment (migrations/env.py imports app.core.settings, which loads
backend/.env, so it is available here), falling back to the QA values below.
Change any of it afterwards with one call per environment:

    POST /api/agents/{key}/config   (optionally with tenant_id/organization_id)

which writes a new version, deactivates the old one and leaves an audit row.
Doing that at a TENANT's scope is what lets two tenants use different Mitra
deployments.

THE CHECKSUM IS COMPUTED THE SAME WAY THE APPLICATION COMPUTES IT
-----------------------------------------------------------------
`app.domain.agent_spec.canonical_json` is
`sha256(json.dumps(spec, separators=(',',':'), sort_keys=True))` over the raw
spec dict. It is reproduced here with the standard library rather than imported,
because a migration must keep working when the application code it was written
against has moved on. `tests/integration/test_seed_migration.py` asserts the two
agree, so a change to canonical_json cannot drift away from this silently.

IDEMPOTENT
----------
Agents insert `ON CONFLICT (key) DO NOTHING`; a config row is written only when
the agent has no default-scope config at all. Re-running this migration on a
database an operator has since edited changes nothing.

WHO SEEDED ROWS BELONG TO
-------------------------
Every row written here carries `created_by` = `updated_by` = 'system', the same
value as the audit block's server default. A seeded row was created by the
system, not by a user, and that is all `created_by` claims -- see SYSTEM_ACTOR
below. `downgrade()` therefore identifies its own work structurally (keys,
default scope, version 1) rather than by a marker in that column.
"""
from typing import Any, Dict, Sequence, Union
import hashlib
import json
import os

from alembic import op

revision: str = "0010"
down_revision: Union[str, Sequence[str], None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: What this migration stamps on every row it writes.
#:
#: The same value as the audit block's server default, deliberately: a seeded
#: row was created by the system, not by a user, and that is the whole of what
#: created_by records. A distinct 'migration' marker would put a second spelling
#: of "nobody in particular" into a column whose vocabulary is meant to be small.
#:
#: Defined here rather than imported from app.models.orm -- a migration must not
#: depend on application code it may outlive.
SYSTEM_ACTOR = "system"

#: Fallbacks for a deployment whose .env does not carry the MITRA_* keys. The
#: paths are Mitra's API contract; the hosts and ip_* are the QA values, and
#: match the `company` / `bot_route` hardcoded in the specs below.
_CONNECTION_DEFAULTS = {
    "base_url": "https://qa-mohini.shikshalokam.org",
    "ws_url": "wss://qa-mohini.shikshalokam.org/ws/common/",
    "user_agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "allowed_hosts": "qa-mohini-static.shikshalokam.org",
    "connect_timeout_s": "10",
    "read_timeout_s": "30",
    "ws_connect_timeout_s": "10",
    "ip_city": "Bengaluru",
    "ip_state": "Karnataka",
    "ip_zip": "562130",
    "profile": "/api/profile/",
    "generate_session": "/api/generate-session/",
    "chat": "/api/companychat/",
    "get_story": "/api/get-story/",
    "finalize_v1": "/api/end-story/",
    "finalize_v2": "/api/end-story/v2/",
}


def _env(name: str, key: str) -> str:
    """This deployment's own value for `key`, or the QA fallback."""
    value = os.getenv(name)
    return value if value else _CONNECTION_DEFAULTS[key]


def _connection_block() -> Dict[str, Any]:
    """The `remote.connection` this deployment's environment describes.

    Sixteen `MITRA_*` keys used to live on `Settings`, which made them
    process-global: one Saarthi process could reach exactly one Mitra
    deployment, contradicting `agent_configs` being scoped per tenant. They
    belong in the config row, and this is where they enter it.
    """
    return {
        "base_url": _env("MITRA_BASE_URL", "base_url"),
        "ws_url": _env("MITRA_WS_URL", "ws_url"),
        "user_agent": _env("MITRA_USER_AGENT", "user_agent"),
        # Comma-separated in the env format, a list in the spec.
        "allowed_hosts": [
            h.strip()
            for h in _env("MITRA_ALLOWED_HOSTS", "allowed_hosts").split(",")
            if h.strip()
        ],
        "connect_timeout_s": float(_env("MITRA_CONNECT_TIMEOUT_S", "connect_timeout_s")),
        "read_timeout_s": float(_env("MITRA_READ_TIMEOUT_S", "read_timeout_s")),
        "ws_connect_timeout_s": float(_env("MITRA_WS_CONNECT_TIMEOUT_S", "ws_connect_timeout_s")),
        "ip_city": _env("MITRA_IP_CITY", "ip_city"),
        "ip_state": _env("MITRA_IP_STATE", "ip_state"),
        "ip_zip": _env("MITRA_IP_ZIP", "ip_zip"),
        "paths": {
            "profile": _env("MITRA_PROFILE_PATH", "profile"),
            "generate_session": _env("MITRA_GENERATE_SESSION_PATH", "generate_session"),
            "chat": _env("MITRA_CHAT_PATH", "chat"),
            "get_story": _env("MITRA_GET_STORY_PATH", "get_story"),
            "finalize_v1": _env("MITRA_FINALIZE_V1_PATH", "finalize_v1"),
            "finalize_v2": _env("MITRA_FINALIZE_V2_PATH", "finalize_v2"),
        },
    }


def seed_agents() -> list[Dict[str, Any]]:
    """The three shipped agents.

    A function rather than a module constant because the two remote_flow specs
    embed `_connection_block()`, which reads the environment -- evaluating that
    at import time would freeze whatever the environment looked like when the
    module was first loaded.

    The comments explaining WHY each non-obvious value is what it is are part of
    the data: they are the reason the value must not be "tidied up".
    """
    connection = _connection_block()
    return [
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
                # Flow.objects.get(flow_route=flow), and Mitra has no Flow row
                # for 'guest-mi-story' -- only 'guest-discussion' is registered
                # there (verified against QA: GET /api/flow-connection-info/
                # ?flow_route=guest-mi-story returns 404). v2 turns that missing
                # row into an HTTP 500, so every story finalisation failed. v1
                # resolves the same flow from the SessionFlowName enum ->
                # CompanyBot '/guest-story', which does exist, and is what the
                # Node bot uses for this flow too. Move to v2 once Mitra
                # registers the Flow row.
                "finalize_path": "/api/end-story/",
                "report_media_type": "application/pdf",
                "connection": connection,
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
                "Guided interview that distils the important points from a community "
                "discussion. Use when the user wants to record, summarise or capture a "
                "meeting or group conversation."
            ),
            "agent_type": "remote_flow",
            "sort_order": 15,
            "capabilities": ["guided_interview", "discussion_capture", "multilingual"],
            "remote": {
                "provider": "mitra",
                "flow_name": "guest-discussion",
                "company": "shikshalokamstaging",
                # NOT the story bot route. These two agents differ by exactly one
                # route and one flow name, and swapping either sends the
                # interview to the wrong Mitra bot with no error -- just the
                # wrong questions.
                "bot_route": "/shikshalokam_chaupal",
                # v1, NOT v2 -- and this one is about the PDF, not about which
                # table resolves the bot. v1 (create_story_object) is the ONLY
                # Mitra path with a chaupal branch: flow == GuestDiscussion ->
                # save_chaupal_report -> get_story_html -> get_mom_report_html,
                # the minutes-of-meeting report. v2 (generate_story) has no such
                # branch: it always runs save_generic_story and renders through
                # get_html_from_template, which RETURNS AN EMPTY STRING when no
                # PDFTemplates row matches this flow -- and save_project_story
                # feeds that empty string straight to Gotenberg. The result is a
                # valid, downloadable, completely BLANK PDF: story created,
                # story_media row present, get-story 200, nothing logged. That is
                # the bug this setting fixes.
                "finalize_path": "/api/end-story/",
                # The discussion flow uses NO token anywhere.
                # MitraChannel._authenticate already sends `access_token: None`
                # on the WebSocket, and Mitra's own client for this flow posts
                # `access_token: null` to end-story. Sending one here made Mitra
                # set auth=True and look up an AUTH-typed PDF template instead of
                # the GUEST one -- a second, independent route to the same
                # empty-template branch above.
                "finalize_as_guest": True,
                "connection": connection,
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
                # "cancel" are perfectly ordinary one-word answers to this
                # interview's own questions -- and abandoning the session is
                # irreversible while a missed command is trivially recoverable.
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
                "Handles general inquiries, business hours, and fallback for unrecognized "
                "requests."
            ),
            "agent_type": "llm",
            "status": "enabled",
            # THE routing floor -- see the module docstring. Exactly one agent
            # may carry this (uq_agents_single_default).
            "default": True,
            "sort_order": 30,
            "capabilities": ["general_qa", "fallback"],
            "prompt": (
                "You are an Education Support Assistant. Your scope is strictly limited to education-related topics, including schools, colleges, universities, admissions, examinations, scholarships, curriculum, teachers, students, educational policies, learning resources, and academic programs. Do not answer questions outside the education domain. For non-education queries, politely respond that you are designed to assist only with education-related topics. If an education-related query is unclear or lacks sufficient context, ask the user for clarification before answering.\n"
            ),
            "tools": [],
            "model": {
                "provider": "openrouter",
                # A literal: change it per deployment (or per tenant) through the
                # config API, like everything else. OPENROUTER_MODEL still exists
                # in .env because RouterService reads it for the router's OWN
                # classifier, which is not an agent.
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


#: The sidebar cards. `sg_commons` has no members on purpose -- it is a
#: `coming_soon` card advertising a thing that does not exist yet.
SEED_CAPABILITIES = [
    {
        "key": "listening_at_scale",
        "name": "Listening at Scale",
        "description": "Synthesize field insights into actionable knowledge",
        "icon": "brain",
        "badge": "SHIKSHALOKAM",
        "status": "active",
        "display_order": 10,
        "metadata": {"action": {"type": "display_card"}},
    },
    {
        "key": "sg_commons",
        "name": "SG Commons Portal",
        "description": "AI search for ecosystem assets",
        "icon": "globe",
        "badge": None,
        "status": "coming_soon",
        "display_order": 20,
        "metadata": {"action": {"type": "coming_soon",
                                "message": "SG Commons Portal is coming soon."}},
    },
]

#: Which agents sit under which card, and the message clicking each one sends.
#: The autostart text is delivered with autostart:true, so the server does not
#: title the conversation from it.
SEED_MEMBERSHIP = [
    ("listening_at_scale", "record_stories", "Record Stories", 10,
     "I want to record a story"),
    ("listening_at_scale", "capture_discussion", "Capture Discussions", 20,
     "I want to capture a discussion"),
]


def canonical(spec: Dict[str, Any]) -> tuple[str, str]:
    """Mirrors app.domain.agent_spec.canonical_json, standard library only."""
    payload = json.dumps(spec, separators=(",", ":"), sort_keys=True)
    return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def upgrade() -> None:
    from sqlalchemy import text

    bind = op.get_bind()

    # ------------------------------------------------------------------
    # 1. agents, and one active config apiece
    # ------------------------------------------------------------------
    for spec in seed_agents():
        config, checksum = canonical(spec)

        bind.execute(
            text("""
                INSERT INTO agents (tenant_id, organization_id, key, name, description,
                                    agent_type, status, is_default, sort_order,
                                    created_by, updated_by)
                VALUES ('default', 'default', :key, :name, :description,
                        CAST(:agent_type AS agent_type_enum),
                        CAST(:status AS agent_status_enum), :is_default, :sort_order,
                        :actor, :actor)
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
                "actor": SYSTEM_ACTOR,
            },
        )

        # Only when the agent has NO default-scope config. Anything already
        # there is an operator's, and superseding it would silently revert a
        # deliberate change.
        bind.execute(
            text("""
                INSERT INTO agent_configs (agent_id, tenant_id, organization_id, version,
                                           checksum, config, is_active, activated_at,
                                           created_by, updated_by)
                SELECT a.id, 'default', 'default', 1,
                       CAST(:checksum AS varchar), CAST(:config AS jsonb),
                       TRUE, now(), :actor, :actor
                FROM agents a
                WHERE a.key = CAST(:key AS varchar)
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_configs c
                      WHERE c.agent_id = a.id
                        AND c.tenant_id = 'default' AND c.organization_id = 'default'
                  )
            """),
            {"key": spec["key"], "checksum": checksum, "config": config,
             "actor": SYSTEM_ACTOR},
        )

    # ------------------------------------------------------------------
    # 2. capability cards
    # ------------------------------------------------------------------
    for cap in SEED_CAPABILITIES:
        bind.execute(
            text("""
                INSERT INTO capabilities (tenant_id, organization_id, key, name, description,
                                          icon, badge, status, display_order, metadata,
                                          created_by, updated_by)
                VALUES ('default', 'default', :key, :name, :description, :icon, :badge,
                        CAST(:status AS capability_status_enum), :display_order,
                        CAST(:metadata AS jsonb), :actor, :actor)
                ON CONFLICT (tenant_id, organization_id, key) DO NOTHING
            """),
            {**{k: v for k, v in cap.items() if k != "metadata"},
             "metadata": json.dumps(cap["metadata"]),
             "actor": SYSTEM_ACTOR},
        )

    # ------------------------------------------------------------------
    # 3. membership -- AFTER the agents exist, which is the whole point of
    #    doing all three in one migration. capability_agents has a real FK to
    #    agents, so this insert matches nothing if the agents are not there yet.
    # ------------------------------------------------------------------
    for capability_key, agent_key, label, order, autostart in SEED_MEMBERSHIP:
        # The CASTs are required, not defensive. A bare parameter in a SELECT
        # list has no inferable type -- Postgres answers "could not determine
        # data type of parameter $N" -- because unlike an INSERT ... VALUES
        # there is no target column to infer from.
        bind.execute(
            text("""
                INSERT INTO capability_agents (capability_id, agent_id, display_order,
                                               label_override, is_visible, metadata,
                                               created_by, updated_by)
                SELECT c.id, a.id, CAST(:display_order AS integer),
                       CAST(:label AS varchar), true,
                       jsonb_build_object('action',
                           jsonb_build_object('type', 'start_agent',
                                              'autostart', CAST(:autostart AS text))),
                       CAST(:actor AS varchar), CAST(:actor AS varchar)
                FROM capabilities c, agents a
                WHERE c.key = CAST(:capability_key AS varchar)
                  AND c.tenant_id = 'default' AND c.organization_id = 'default'
                  AND a.key = CAST(:agent_key AS varchar)
                ON CONFLICT (capability_id, agent_id) DO NOTHING
            """),
            {"capability_key": capability_key, "agent_key": agent_key, "label": label,
             "display_order": order, "autostart": autostart, "actor": SYSTEM_ACTOR},
        )


def downgrade() -> None:
    """Removes ONLY what this migration inserted.

    IT IDENTIFIES ITS OWN ROWS STRUCTURALLY, NOT BY ACTOR. `created_by` is
    'system' here -- the same value as the column's server default -- so it says
    "no user did this", which is true of plenty of rows this migration never
    wrote. The scoping below is what makes the deletes precise instead:

      * the seeded KEYS only, at the DEFAULT scope only, so a tenant's own
        capability or a differently-keyed agent is out of range entirely;
      * `agent_configs` only at `version = 1`, which is the only version this
        migration writes -- an operator's v2 is untouched;
      * and each config is deleted only if it is the agent's SOLE config, so an
        agent that has been reconfigured since keeps both its config and itself.

    An agent is dropped only once no configs remain, which is also what keeps
    the FK from `agent_configs` satisfied.
    """
    from sqlalchemy import text

    bind = op.get_bind()
    agent_keys = [spec["key"] for spec in seed_agents()]
    capability_keys = [cap["key"] for cap in SEED_CAPABILITIES]

    bind.execute(
        text("""
            DELETE FROM capability_agents ca
            USING capabilities c
            WHERE ca.capability_id = c.id
              AND c.key = ANY(:capability_keys)
              AND c.tenant_id = 'default' AND c.organization_id = 'default'
        """),
        {"capability_keys": capability_keys},
    )
    bind.execute(
        text("""
            DELETE FROM capabilities
            WHERE key = ANY(:capability_keys)
              AND tenant_id = 'default' AND organization_id = 'default'
        """),
        {"capability_keys": capability_keys},
    )
    bind.execute(
        text("""
            DELETE FROM agent_configs c
            USING agents a
            WHERE c.agent_id = a.id
              AND a.key = ANY(:agent_keys)
              AND c.version = 1
              AND c.tenant_id = 'default' AND c.organization_id = 'default'
              AND NOT EXISTS (
                  SELECT 1 FROM agent_configs other
                  WHERE other.agent_id = a.id AND other.id <> c.id
              )
        """),
        {"agent_keys": agent_keys},
    )
    bind.execute(
        text("""
            DELETE FROM agents a
            WHERE a.key = ANY(:agent_keys)
              AND NOT EXISTS (SELECT 1 FROM agent_configs c WHERE c.agent_id = a.id)
        """),
        {"agent_keys": agent_keys},
    )
