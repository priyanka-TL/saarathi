"""Move the MITRA_* connection settings into the agent config row.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-03 10:00:00.000000

WHY
===
Sixteen `MITRA_*` keys described WHICH Mitra deployment an agent talks to --
base URL, WebSocket URL, user agent, allowed hosts, three timeouts, three
`ip_*` fields and the six endpoint paths. Living on `Settings` made them
process-global: one Saarthi process could reach exactly one Mitra deployment,
which contradicts `agent_configs` being scoped (tenant_id, organization_id).

`MitraConnectionSpec` already existed as an optional per-agent override layered
on top of those settings. This migration completes the move: the settings are
deleted, `remote.connection` becomes REQUIRED, and it is now the only source.
`MITRA_COMPANY` / `MITRA_STORY_BOT_ROUTE` / `MITRA_DISCUSSION_BOT_ROUTE` made
the same journey in 0007; this is the rest of it.

Four Mitra keys stay in the environment, and each for a structural reason:
`MITRA_ENABLED` (read at container build, before any config is loaded),
`MITRA_ORIGIN_URL` (a credential -- see RemoteSpec.origin_env),
`MITRA_HOST_CEILING` (the operator's backstop ON config, so config cannot hold
it) and `MITRA_MAX_OPEN_CHANNELS` / `MITRA_IDLE_CLOSE_S` (bounds on one
process-wide pool shared by every agent).

WHERE THE VALUES COME FROM
==========================
`os.environ` first, then the QA defaults below. Reading the environment is the
whole point of this migration -- it is a one-time transfer of env -> database,
so a deployment that had its own `MITRA_BASE_URL` keeps ITS endpoint rather
than inheriting QA's. `migrations/env.py` imports `app.core.settings`, which
calls `load_dotenv`, so `backend/.env` is already loaded by the time this runs.

The fallbacks matter because a deployment may equally well clean the dead keys
out of its `.env` BEFORE running `make migrate`. They are the same QA values
0007 hardcodes for `company` / `bot_route`, and for the same reason.

IN PLACE, NOT A NEW VERSION
===========================
0007 superseded legacy rows with a new version so the old one stayed one
`activate` call away. That argument does not apply here: a row without
`remote.connection` no longer validates at all, so it is not a rollback target
-- activating it would just make the agent vanish from routing with a log line
(the same trap 0008 cleaned up). There is also nothing to roll back TO: the
block written here is exactly what the environment was already supplying, so
behaviour is unchanged.

EVERY remote_flow row is patched, not just the active ones, so that an older
version can still be activated afterwards.

The checksum is recomputed the same way `app.domain.agent_spec.canonical_json`
does -- standard library only, so this keeps working when that code moves on.
`tests/integration/test_seed_migration.py` asserts the two agree.
"""
from typing import Any, Dict, Sequence, Union
import hashlib
import json
import os

from alembic import op

revision: str = "0009"
down_revision: Union[str, Sequence[str], None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Fallbacks for a deployment whose .env has already been cleaned. The paths
#: are Mitra's API contract (identical to MitraPaths' own defaults); the hosts
#: and ip_* are the QA values, matching 0007's company / bot_route.
_DEFAULTS = {
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
    """The deployment's own value for `key`, or the QA fallback."""
    value = os.getenv(name)
    return value if value else _DEFAULTS[key]


def _connection_block() -> Dict[str, Any]:
    """The `remote.connection` this deployment's environment describes."""
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
        "ws_connect_timeout_s": float(
            _env("MITRA_WS_CONNECT_TIMEOUT_S", "ws_connect_timeout_s")
        ),
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


def canonical(spec: Dict[str, Any]) -> tuple[str, str]:
    """Mirrors app.domain.agent_spec.canonical_json, standard library only."""
    payload = json.dumps(spec, separators=(",", ":"), sort_keys=True)
    return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _remote_flow_rows(bind):
    """Every stored remote_flow config, whatever its scope or version."""
    from sqlalchemy import text

    return bind.execute(
        text("""
            SELECT id, config::text AS config_text
            FROM agent_configs
            WHERE config->>'agent_type' = 'remote_flow'
        """)
    ).fetchall()


def _rewrite(bind, row_id, config: Dict[str, Any]) -> None:
    from sqlalchemy import text

    payload, checksum = canonical(config)
    bind.execute(
        text("""
            UPDATE agent_configs
            SET config = CAST(:config AS jsonb), checksum = CAST(:checksum AS varchar)
            WHERE id = :id
        """),
        {"id": row_id, "config": payload, "checksum": checksum},
    )


def upgrade() -> None:
    bind = op.get_bind()
    connection_block = _connection_block()

    for row in _remote_flow_rows(bind):
        try:
            config = json.loads(row.config_text or "")
        except ValueError:
            # Not JSON we can reason about; 0008 already removed the legacy
            # shapes, and rewriting something unparseable is worse than skipping.
            continue

        remote = config.get("remote")
        if not isinstance(remote, dict) or "connection" in remote:
            # Already carries its own connection -- an operator wrote it
            # deliberately, and it is exactly the value this migration would
            # otherwise be guessing at. Leave it alone.
            continue

        remote["connection"] = connection_block
        _rewrite(bind, row.id, config)


def downgrade() -> None:
    """Strip `remote.connection` back off, restoring the env-floor shape.

    Only blocks identical to the one `upgrade` wrote are removed: anything an
    operator has since edited is theirs, and dropping it would silently
    repoint that agent at whatever the environment happens to say.
    """
    bind = op.get_bind()
    connection_block = _connection_block()

    for row in _remote_flow_rows(bind):
        try:
            config = json.loads(row.config_text or "")
        except ValueError:
            continue

        remote = config.get("remote")
        if not isinstance(remote, dict):
            continue
        if remote.get("connection") != connection_block:
            continue

        del remote["connection"]
        _rewrite(bind, row.id, config)
