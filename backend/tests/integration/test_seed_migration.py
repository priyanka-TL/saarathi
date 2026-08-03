"""Migration 0007 is the agent catalogue. These are its guards.

It replaced `app/config/agents/*.yaml` -- there is no file to sync from any
more, so if this migration is wrong the application boots with no agents at all
(`sync_and_reload` raises) or, worse, with a config Mitra silently mishandles.

Two properties matter enough to pin:

  1. **The seeded specs are VALID.** A typo in the migration is not caught by
     anything else -- it is a plain dict, and the failure surfaces as a registry
     that loads zero agents at startup.
  2. **The migration computes the SAME checksum the application does.** It uses
     the standard library rather than importing `canonical_json`, deliberately,
     so that it keeps working when the application moves on. That independence
     is only safe while the two agree, which is what the test below asserts.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from pydantic import TypeAdapter
from sqlalchemy import text

from app.database.engine import SessionLocal
from app.domain.agent_spec import AgentSpec, canonical_json

_MIGRATION = Path(__file__).parents[2] / "migrations" / "versions" / "0007_seed_agents.py"

_adapter = TypeAdapter(AgentSpec)


def _module():
    """Loaded by path: `migrations/versions` is not a package, and alembic
    revision filenames are not importable identifiers."""
    spec = importlib.util.spec_from_file_location("_seed_0007", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def seed():
    return _module()


def test_every_seeded_spec_validates(seed):
    for raw in seed.SEED_AGENTS:
        _adapter.validate_python(raw)  # raises on a typo -- that is the assertion


def test_the_migration_computes_the_same_checksum_as_the_application(seed):
    """The migration reimplements canonical_json with hashlib/json so it does
    not depend on application code it may outlive. This is the tripwire for the
    two drifting apart -- if it fires, the seeded rows carry checksums that no
    longer match what `POST /config` would produce for identical content, and
    HandlerFactory's cache key stops meaning what it says."""
    for raw in seed.SEED_AGENTS:
        migration_json, migration_sha = seed.canonical(raw)
        app_json, app_sha = canonical_json(_adapter.validate_python(raw))

        assert migration_json == app_json, raw["key"]
        assert migration_sha == app_sha, raw["key"]


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
                SELECT a.key, a.agent_type, a.status, c.is_active, c.source
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
