"""Migrations 0007 and 0009 are the agent catalogue. These are their guards.

0007 replaced `app/config/agents/*.yaml` -- there is no file to sync from any
more, so if it is wrong the application boots with no agents at all or, worse,
with a config Mitra silently mishandles. 0009 completes it: the MITRA_*
connection settings moved out of the environment into `remote.connection`, so a
remote_flow spec is only valid once 0009 has added that block. THE TWO ARE
TESTED TOGETHER because together is how a database ever sees them.

Two properties matter enough to pin:

  1. **The seeded specs are VALID.** A typo in a migration is not caught by
     anything else -- it is a plain dict, and the failure surfaces as a registry
     that loads zero agents at startup.
  2. **The migrations compute the SAME checksum the application does.** They use
     the standard library rather than importing `canonical_json`, deliberately,
     so that they keep working when the application moves on. That independence
     is only safe while the two agree, which is what the test below asserts.
"""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest
from pydantic import TypeAdapter
from sqlalchemy import text

from app.database.engine import SessionLocal
from app.domain.agent_spec import AgentSpec, canonical_json

_VERSIONS = Path(__file__).parents[2] / "migrations" / "versions"
_MIGRATION = _VERSIONS / "0007_seed_agents.py"
_CONNECTION_MIGRATION = _VERSIONS / "0009_mitra_connection_to_config.py"

_adapter = TypeAdapter(AgentSpec)


def _module(path: Path, name: str):
    """Loaded by path: `migrations/versions` is not a package, and alembic
    revision filenames are not importable identifiers."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def seed():
    return _module(_MIGRATION, "_seed_0007")


@pytest.fixture(scope="module")
def connection_migration():
    return _module(_CONNECTION_MIGRATION, "_conn_0009")


def _as_stored(raw: dict, connection_migration) -> dict:
    """The spec as it lands in the database: 0007's dict with 0009 applied."""
    spec = copy.deepcopy(raw)
    if spec.get("agent_type") == "remote_flow":
        spec["remote"]["connection"] = connection_migration._connection_block()
    return spec


def test_every_seeded_spec_validates(seed, connection_migration):
    for raw in seed.SEED_AGENTS:
        # Raises on a typo -- that is the assertion.
        _adapter.validate_python(_as_stored(raw, connection_migration))


def test_a_seeded_remote_spec_is_incomplete_until_0009_runs(seed):
    """0007 is applied history and is deliberately NOT edited to carry a
    connection block. Pinning that here so a future reader does not "fix" 0007
    and quietly change the checksum of every row it ever wrote."""
    from pydantic import ValidationError

    remote = [s for s in seed.SEED_AGENTS if s.get("agent_type") == "remote_flow"]
    assert remote, "0007 should still seed the remote_flow agents"
    for raw in remote:
        assert "connection" not in raw["remote"]
        with pytest.raises(ValidationError):
            _adapter.validate_python(raw)


def test_the_migration_computes_the_same_checksum_as_the_application(
    seed, connection_migration
):
    """The migrations reimplement canonical_json with hashlib/json so they do
    not depend on application code they may outlive. This is the tripwire for
    the two drifting apart -- if it fires, the seeded rows carry checksums that
    no longer match what `POST /config` would produce for identical content, and
    HandlerFactory's cache key stops meaning what it says."""
    for raw in seed.SEED_AGENTS:
        stored = _as_stored(raw, connection_migration)

        migration_json, migration_sha = seed.canonical(stored)
        app_json, app_sha = canonical_json(_adapter.validate_python(stored))
        assert migration_json == app_json, raw["key"]
        assert migration_sha == app_sha, raw["key"]

        # 0009 recomputes it too, and must agree with both.
        assert connection_migration.canonical(stored) == (migration_json, migration_sha)


def test_no_seeded_value_still_carries_an_env_reference(seed):
    """${VAR} expansion was removed with the YAML. A leftover reference is no
    longer a placeholder -- it is a literal string that would be sent to Mitra
    verbatim as a company slug or a bot route."""
    import json

    for raw in seed.SEED_AGENTS:
        assert "${" not in json.dumps(raw), raw["key"]


def test_the_shipped_catalogue_is_present_and_active_in_the_database():
    """What the migration is actually for: a migrated database has a working
    catalogue with no YAML, no config sync and no first-run flag."""
    session = SessionLocal()
    try:
        rows = session.execute(
            text("""
                SELECT a.key, a.agent_type, a.status, c.is_active
                FROM agents a
                JOIN agent_configs c ON c.agent_id = a.id
                WHERE a.key IN ('record_stories', 'capture_discussion', 'general_support')
                  AND c.tenant_id = 'default' AND c.organization_id = 'default'
                  AND c.is_active
            """)
        ).fetchall()
    finally:
        session.close()

    assert {r.key for r in rows} == {
        "record_stories", "capture_discussion", "general_support",
    }


def test_exactly_one_default_agent():
    """RouterService falls back to `registry.default()`, which returns the first
    is_default row it finds. Two would make that fallback non-deterministic."""
    session = SessionLocal()
    try:
        count = session.execute(
            text("SELECT count(*) FROM agents WHERE is_default AND status = 'enabled'")
        ).scalar()
    finally:
        session.close()

    assert count == 1


def test_the_seeded_configs_in_the_database_still_validate():
    """The rows as they actually are, not as the migration source says they
    should be -- this also covers a database upgraded from the YAML era, where
    0007 had to supersede configs still holding ${VAR} references."""
    session = SessionLocal()
    try:
        rows = session.execute(
            text("""
                SELECT a.key, c.config FROM agents a
                JOIN agent_configs c ON c.agent_id = a.id
                WHERE c.is_active
                  AND c.tenant_id = 'default' AND c.organization_id = 'default'
                  AND a.status = 'enabled'
            """)
        ).fetchall()
    finally:
        session.close()

    assert rows, "a migrated database must have an active default-scope config"
    for row in rows:
        _adapter.validate_python(row.config)


def test_capability_membership_survived_the_fresh_database_ordering():
    """Migration 0006 seeds the capabilities but can only link membership for
    agents that already exist -- and on a fresh database it runs BEFORE 0007,
    when none do. 0007 fills in what 0006 had to skip; without that the sidebar
    renders a capability card with no buttons under it."""
    session = SessionLocal()
    try:
        members = session.execute(
            text("""
                SELECT a.key FROM capability_agents ca
                JOIN capabilities c ON c.id = ca.capability_id
                JOIN agents a ON a.id = ca.agent_id
                WHERE c.key = 'listening_at_scale'
                  AND c.tenant_id = 'default' AND c.organization_id = 'default'
            """)
        ).fetchall()
    finally:
        session.close()

    assert {m.key for m in members} >= {"record_stories", "capture_discussion"}


# ---------------------------------------------------------------------------
# The upgrade path: databases that already held YAML-era configs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("config_text", [
    # ${VAR} expansion is gone, so this is now a literal string where a number
    # or a company slug is expected.
    '{"key":"a","name":"A","agent_type":"llm","model":{"timeout_s":"${LLM_TIMEOUT:-30}"}}',
    # The environment indirection `bot_route` / `company` replaced.
    '{"key":"a","name":"A","agent_type":"remote_flow","remote":{"bot_route_env":"X"}}',
    '{"key":"a","name":"A","agent_type":"remote_flow","remote":{"company_env":"X"}}',
    # Neither field present at all -- both are required by RemoteSpec now.
    '{"key":"a","name":"A","agent_type":"remote_flow","remote":{"flow_name":"guest-mi-story"}}',
])
def test_legacy_configs_are_detected_for_supersession(seed, config_text):
    assert seed.is_legacy(config_text) is True


@pytest.mark.parametrize("config_text", [
    '{"key":"a","name":"A","agent_type":"llm","prompt":"p","model":{"name":"m"}}',
    '{"key":"a","name":"A","agent_type":"remote_flow",'
    '"remote":{"bot_route":"/r","company":"c"}}',
])
def test_a_current_config_is_left_alone(seed, config_text):
    """An operator's own configuration must survive the upgrade untouched --
    superseding it would silently revert a deliberate production change."""
    assert seed.is_legacy(config_text) is False


def test_unparseable_json_is_not_treated_as_legacy(seed):
    """Better to leave a row alone than to overwrite something unreadable: the
    registry skips it and says so, which is recoverable, where a blind
    supersede would destroy whatever it actually was."""
    assert seed.is_legacy("not json at all") is False
