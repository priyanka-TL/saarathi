"""Seed the Saathi agent, its config and its sidebar card

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-10 06:05:00.000000

Requires 0011, which adds 'saathi_flow' to agent_type_enum -- Postgres will not
let one transaction use a label it added itself.

WHAT SAATHI IS, AND WHY THE CONFIG LOOKS LIKE MITRA'S
-----------------------------------------------------
Saathi runs the SAME Django application as Mitra (project shikshalokam_mohini),
so `remote.connection` carries the same fields and the same six paths. Verified
against the live QA deployment on 2026-08-10:

  GET /api/flow-connection-info/?flow_route=saathi
    -> {"websocket_url": "ws/common/", "bot_route": "/saathi-bot",
        "create_story": "none"}

`create_story: "none"` is why `produces_artifact` is false and `finalize_path`
is null: there is no story to submit and no PDF to fetch. Setting a
finalize_path here would be rejected at config-write time.

TIMEOUTS ARE MEASURED, NOT COPIED. Four live turns against /saathi-bot took
4.1s, 6.6s and 8.9s (vector search and web search are both enabled on that
bot). The Mitra defaults -- 60s first turn, 45s after -- have ample headroom,
so they are kept rather than tightened to numbers a slower query would breach.

DEPLOYMENT-SPECIFIC VALUES come from SAATHI_* environment variables where set,
falling back to the QA values below. Change them afterwards with
`POST /api/agents/saathi/config`, optionally at a tenant's scope.

CREDENTIALS ARE NOT HERE. The Origin header and the ELEVATE login live in
`.env` (SAATHI_ORIGIN_URL, SAATHI_EMAIL / SAATHI_PASSWORD or
SAATHI_ACCESS_TOKEN) -- a config row is readable through the admin API.
"""
import hashlib
import json
import os
from typing import Any, Dict, Sequence, Tuple, Union

from alembic import op

revision: str = "0012"
down_revision: Union[str, Sequence[str], None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Same value as the audit block's server default -- a seeded row was created
#: by the system, not by a user. Defined here rather than imported: a migration
#: must not depend on application code it may outlive.
SYSTEM_ACTOR = "system"

AGENT_KEY = "saathi"
CAPABILITY_KEY = "saathi_assistant"

#: Fallbacks for a deployment whose .env carries no SAATHI_* keys. These are the
#: QA values, verified live.
_DEFAULTS = {
    "base_url": "https://qa.saathi.shikshalokam.org",
    "ws_url": "wss://qa.saathi.shikshalokam.org/ws/common/",
    "user_agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "allowed_hosts": "",
    "connect_timeout_s": "10",
    "read_timeout_s": "30",
    "ws_connect_timeout_s": "10",
    "ip_city": "Bengaluru",
    "ip_state": "Karnataka",
    "ip_zip": "562130",
    "bot_route": "/saathi-bot",
    "company": "shikshalokamstaging",
}


def _env(name: str, key: str) -> str:
    value = os.getenv(name)
    return value if value else _DEFAULTS[key]


def _connection_block() -> Dict[str, Any]:
    """The `remote.connection` this deployment's environment describes.

    The six paths are Saathi's API contract and identical to Mitra's -- it is
    the same Django application -- so they are not environment-driven. Only the
    host, the timeouts and the address fields vary per deployment.
    """
    return {
        "base_url": _env("SAATHI_BASE_URL", "base_url"),
        "ws_url": _env("SAATHI_WS_URL", "ws_url"),
        "user_agent": _env("SAATHI_USER_AGENT", "user_agent"),
        "allowed_hosts": [
            h.strip()
            for h in _env("SAATHI_ALLOWED_HOSTS", "allowed_hosts").split(",")
            if h.strip()
        ],
        "connect_timeout_s": float(_env("SAATHI_CONNECT_TIMEOUT_S", "connect_timeout_s")),
        "read_timeout_s": float(_env("SAATHI_READ_TIMEOUT_S", "read_timeout_s")),
        "ws_connect_timeout_s": float(_env("SAATHI_WS_CONNECT_TIMEOUT_S", "ws_connect_timeout_s")),
        "ip_city": _env("SAATHI_IP_CITY", "ip_city"),
        "ip_state": _env("SAATHI_IP_STATE", "ip_state"),
        "ip_zip": _env("SAATHI_IP_ZIP", "ip_zip"),
        "paths": {
            "profile": "/api/profile/",
            "generate_session": "/api/generate-session/",
            "chat": "/api/companychat/",
            "get_story": "/api/get-story/",
            "finalize_v1": "/api/end-story/",
            "finalize_v2": "/api/end-story/v2/",
        },
    }


def seed_spec() -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "key": AGENT_KEY,
        "name": "Saathi",
        "description": (
            "School improvement companion. Answers questions about teaching practice, "
            "government schemes and learning frameworks, and builds structured 30-40 day "
            "improvement plans for a specific school challenge."
        ),
        "agent_type": "saathi_flow",
        "status": "enabled",
        "sort_order": 20,
        "capabilities": ["school_improvement", "question_answering", "action_planning"],
        "remote": {
            "provider": "saathi",
            # The flow_route Saathi's own /api/flow-connection-info/ answers to.
            "flow_name": "saathi",
            # Resolved from that same endpoint. Explicit, never inferred: a
            # wrong route reaches a DIFFERENT bot with no error, just the wrong
            # conversation (/saathi-profile is the context-collection bot).
            "bot_route": _env("SAATHI_BOT_ROUTE", "bot_route"),
            "company": _env("SAATHI_COMPANY", "company"),
            "default_language": "en",
            "supported_languages": ["en", "hi", "kn", "te"],
            "handshake": {"settle_ms": 1500, "timeout_ms": 5000},
            # Measured, see the module docstring: observed turns were 4-9s.
            "turn": {
                "first_turn_timeout_ms": 60000,
                "turn_timeout_ms": 45000,
                "idle_gap_ms": 8000,
            },
            # create_story: "none" -- nothing is ever finalised, so a terminal
            # turn completes the session directly (TurnFinalizer's no-artifact
            # path) instead of submitting a story that does not exist.
            "produces_artifact": False,
            "finalize_path": None,
            # Irrelevant while produces_artifact is false, but false is the
            # honest value: the socket authenticates as a REAL user, not a guest.
            "finalize_as_guest": False,
            "connection": _connection_block(),
        },
        "routing": {
            "keywords": [
                "saathi", "improvement plan", "school improvement",
                "action plan", "teaching practice",
            ],
            "priority": 80,
            # Pins the conversation: the remote session holds the interview
            # state, so the next turn must reach the same agent.
            "pin_session": True,
            "exit_keywords": ["/exit", "exit saathi", "stop saathi"],
            "router_selectable": True,
            "direct_selectable": True,
        },
        "memory": {"strategy": "recent", "history_turns": 10},
        # Saathi keeps its own history server-side (chat_history_limit: 1000),
        # so Saarthi's window only feeds routing, never the bot.
        "limits": {"max_tool_iterations": 0, "rate_limit_per_conversation_per_min": 20},
        "features": {"emit_options": True, "record_tool_executions": False},
    }


SEED_CAPABILITY = {
    "key": CAPABILITY_KEY,
    # Named for the agent, not for a category. "Listening at Scale" groups two
    # interview agents and earns an abstract name; this card holds exactly one
    # agent, so a category name would just be a second label for Saathi.
    "name": "Saathi - MI Companion",
    "description": "Real-time assistance to first-mile actors",
    # A REGISTERED name, not a guess. resolveIcon() falls back to AgentIcon for
    # anything it does not know, so a typo here costs a wrong glyph silently --
    # which is what "school" was doing. `sparkle` is registered in
    # frontend/src/components/icons/registry.js.
    "icon": "sparkle",
    # Same treatment as Listening at Scale: the card renders it as a pill.
    "badge": "SHIKSHALOKAM",
    "status": "active",
    # BETWEEN the two cards migration 0010 seeded -- Listening at Scale is 10
    # and SG Commons Portal is 20. Chosen so Saathi slots in without touching
    # either of those rows: 0010 is already applied everywhere, and reordering
    # its data would mean a migration that rewrites shipped seed values.
    "display_order": 15,
    # THE CARD ITSELF LAUNCHES SAATHI -- note the agentKey.
    #
    # Without it the frontend normalises `start_agent` to `none` (a card that
    # would reset the conversation and pin `undefined` is neutralised at parse
    # time), which is why the card was inert and its nested button did the
    # routing. With it, the card is the single control and
    # `capability_service` suppresses the redundant button.
    #
    # agentKey is spelled in the frontend's camelCase because this action dict
    # is passed through to the client verbatim; the backend only injects
    # agentKey for MEMBERSHIP rows, never for a card.
    "metadata": {"action": {"type": "start_agent", "agentKey": AGENT_KEY}},
}


def canonical(spec: Dict[str, Any]) -> Tuple[str, str]:
    """The exact bytes and checksum `app.domain.agent_spec.canonical_json`
    produces. Reproduced with the standard library so this migration keeps
    working when that function moves; test_seed_migration.py asserts they agree.
    """
    payload = json.dumps(spec, separators=(",", ":"), sort_keys=True)
    return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def upgrade() -> None:
    from sqlalchemy import text

    bind = op.get_bind()
    spec = seed_spec()
    config, checksum = canonical(spec)

    bind.execute(
        text("""
            INSERT INTO agents (tenant_id, organization_id, key, name, description,
                                agent_type, status, is_default, sort_order,
                                created_by, updated_by)
            VALUES ('default', 'default', :key, :name, :description,
                    CAST(:agent_type AS agent_type_enum),
                    CAST(:status AS agent_status_enum), FALSE, :sort_order,
                    :actor, :actor)
            ON CONFLICT (key) DO NOTHING
        """),
        {
            "key": spec["key"], "name": spec["name"], "description": spec["description"],
            "agent_type": spec["agent_type"], "status": spec["status"],
            "sort_order": spec["sort_order"], "actor": SYSTEM_ACTOR,
        },
    )

    # Only when the agent has NO default-scope config: anything already there is
    # an operator's, and superseding it would silently revert a deliberate change.
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
        {"key": spec["key"], "checksum": checksum, "config": config, "actor": SYSTEM_ACTOR},
    )

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
        {**{k: v for k, v in SEED_CAPABILITY.items() if k != "metadata"},
         "metadata": json.dumps(SEED_CAPABILITY["metadata"]),
         "actor": SYSTEM_ACTOR},
    )

    # Membership last: capability_agents holds a real FK to agents, so this
    # matches nothing unless the insert above has already run.
    #
    # THIS ROW IS NEVER RENDERED, AND IS NOT DEAD DATA. The card launches Saathi
    # itself (see SEED_CAPABILITY's action above), so `capability_service`
    # serves this capability with `agents: []` -- a nested button would be a
    # second control for the same thing.
    #
    # It exists because it is what the PROVIDER ENABLE GATE keys off: the
    # service hides a card whose configured agents were all filtered out, so
    # deleting this row would make the Saathi card render, and fail on click,
    # whenever SAATHI_ENABLED=0.
    #
    # The CASTs are required, not defensive: a bare parameter in a SELECT list
    # has no inferable type, unlike an INSERT ... VALUES with a target column.
    bind.execute(
        text("""
            INSERT INTO capability_agents (capability_id, agent_id, display_order,
                                           label_override, is_visible, metadata,
                                           created_by, updated_by)
            SELECT c.id, a.id, 10, CAST(:label AS varchar), TRUE,
                   CAST(:metadata AS jsonb), CAST(:actor AS varchar), CAST(:actor AS varchar)
            FROM capabilities c, agents a
            WHERE c.key = CAST(:capability_key AS varchar)
              AND c.tenant_id = 'default' AND c.organization_id = 'default'
              AND a.key = CAST(:agent_key AS varchar)
            ON CONFLICT (capability_id, agent_id) DO NOTHING
        """),
        {
            "capability_key": CAPABILITY_KEY,
            "agent_key": AGENT_KEY,
            "label": "Saathi",
            # No autostart: Saathi opens with its own greeting, and an injected
            # opener would be answered as if the user had typed it.
            "metadata": json.dumps({"action": {"type": "start_agent"}}),
            "actor": SYSTEM_ACTOR,
        },
    )


def downgrade() -> None:
    """Remove exactly what upgrade() wrote, identified structurally.

    Membership first (FK), then the capability, the config and the agent. Only
    the default-scope version-1 config is removed: a later version is an
    operator's and is not this migration's to delete.
    """
    from sqlalchemy import text

    bind = op.get_bind()
    bind.execute(
        text("""
            DELETE FROM capability_agents ca
            USING capabilities c, agents a
            WHERE ca.capability_id = c.id AND ca.agent_id = a.id
              AND c.key = :capability_key AND a.key = :agent_key
              AND c.tenant_id = 'default' AND c.organization_id = 'default'
        """),
        {"capability_key": CAPABILITY_KEY, "agent_key": AGENT_KEY},
    )
    bind.execute(
        text("DELETE FROM capabilities WHERE key = :key "
             "AND tenant_id = 'default' AND organization_id = 'default'"),
        {"key": CAPABILITY_KEY},
    )
    bind.execute(
        text("""
            DELETE FROM agent_configs c
            USING agents a
            WHERE c.agent_id = a.id AND a.key = :key
              AND c.tenant_id = 'default' AND c.organization_id = 'default'
              AND c.version = 1
        """),
        {"key": AGENT_KEY},
    )
    # GUARDED, because fk_agent_sessions_agent_id_agents is RESTRICT: an agent
    # that has served a turn cannot be deleted, and that is correct -- removing
    # it would orphan real sessions. So a downgrade after the agent has been
    # used leaves the agent row and removes only the card and the config, which
    # is enough to take it out of the sidebar. Found by running the downgrade
    # against a database that had held a live conversation.
    bind.execute(
        text("""
            DELETE FROM agents a
            WHERE a.key = :key
              AND NOT EXISTS (SELECT 1 FROM agent_sessions s WHERE s.agent_id = a.id)
        """),
        {"key": AGENT_KEY},
    )
