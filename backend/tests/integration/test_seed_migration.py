"""Migration 0010 is the agent catalogue. These are its guards.

It replaced `app/config/agents/*.yaml` -- there is no file to sync from any
more, so if it is wrong the application boots with no agents at all or, worse,
with a config Mitra silently mishandles.

THREE PROPERTIES MATTER ENOUGH TO PIN
  1. **The seeded specs are VALID, on their own.** A typo in a migration is not
     caught by anything else -- it is a plain dict, and the failure surfaces as
     a registry that loads zero agents at startup. "On their own" is the part
     worth stating: the previous schema seeded remote_flow specs that were
     deliberately INCOMPLETE until a later migration patched a
     `remote.connection` block into them, so neither migration could be
     validated alone. 0010 writes a complete spec.
  2. **The migration computes the SAME checksum the application does.** It uses
     the standard library rather than importing `canonical_json`, deliberately,
     so that it keeps working when the application moves on. That independence
     is only safe while the two agree, which is what the test below asserts.
  3. **A migrated database actually has a working catalogue** -- three enabled
     agents, exactly one default, and capability membership present on the
     FIRST run.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from pydantic import TypeAdapter
from sqlalchemy import text

from app.database.engine import SessionLocal
from app.domain.agent_spec import AgentSpec, canonical_json

_VERSIONS = Path(__file__).parents[2] / "migrations" / "versions"
_MIGRATION = _VERSIONS / "0010_seed_default_data.py"

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
    return _module(_MIGRATION, "_seed_0010")


def test_every_seeded_spec_validates(seed):
    for raw in seed.seed_agents():
        # Raises on a typo -- that is the assertion.
        _adapter.validate_python(raw)


def test_a_seeded_remote_spec_is_complete_as_written(seed):
    """No second migration patches these rows afterwards.

    The old two-step seed (write the spec, then backfill `remote.connection`)
    meant a freshly-inserted row was invalid for the length of one migration and
    its checksum was computed twice. Pinning the single-step property here so
    nobody reintroduces the split.
    """
    remote = [s for s in seed.seed_agents() if s.get("agent_type") == "remote_flow"]
    assert remote, "0010 should seed the remote_flow agents"
    for raw in remote:
        assert "connection" in raw["remote"], raw["key"]
        _adapter.validate_python(raw)


def test_the_migration_computes_the_same_checksum_as_the_application(seed):
    """The migration reimplements canonical_json with hashlib/json so it does
    not depend on application code it may outlive. This is the tripwire for the
    two drifting apart -- if it fires, the seeded rows carry checksums that no
    longer match what `POST /config` would produce for identical content, and
    HandlerFactory's cache key stops meaning what it says."""
    for raw in seed.seed_agents():
        migration_json, migration_sha = seed.canonical(raw)
        app_json, app_sha = canonical_json(_adapter.validate_python(raw))
        assert migration_json == app_json, raw["key"]
        assert migration_sha == app_sha, raw["key"]


def test_no_seeded_value_still_carries_an_env_reference(seed):
    """${VAR} expansion was removed with the YAML. A leftover reference is no
    longer a placeholder -- it is a literal string that would be sent to Mitra
    verbatim as a company slug or a bot route."""
    import json

    for raw in seed.seed_agents():
        assert "${" not in json.dumps(raw), raw["key"]


def test_exactly_one_agent_is_the_routing_default(seed):
    """RouterService Gate 5 falls back to `registry.default()`, which returns
    the first is_default row it finds, and uq_agents_single_default allows only
    one. Asserted on the source dicts as well as the database (below) because
    this is the property that makes routing total."""
    defaults = [s["key"] for s in seed.seed_agents() if s.get("default")]
    assert defaults == ["general_support"]


# ---------------------------------------------------------------------------
# The database as it actually is, not as the migration source says it should be
# ---------------------------------------------------------------------------


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
    """The DB half of test_exactly_one_agent_is_the_routing_default."""
    session = SessionLocal()
    try:
        count = session.execute(
            text("SELECT count(*) FROM agents WHERE is_default AND status = 'enabled'")
        ).scalar()
    finally:
        session.close()

    assert count == 1


def test_the_seeded_configs_in_the_database_still_validate():
    """The rows as they actually are, read back out of agent_configs."""
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


def test_capability_membership_is_seeded_on_the_first_run():
    """THE ORDERING BUG THIS MIGRATION EXISTS TO PREVENT.

    Membership used to be inserted by an earlier migration than the one creating
    the agents. capability_agents has a real FK to agents, so on a fresh
    database that insert matched nothing and the sidebar card rendered with no
    buttons under it until a later migration re-ran the same statement. Seeding
    agents and membership in one migration, in order, is what fixes it.
    """
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

    assert {m.key for m in members} == {"record_stories", "capture_discussion"}


def test_every_seeded_row_carries_the_system_actor():
    """The audit block is on all nine tables, and the seed must fill it.

    'system' -- the same value as the column's server default -- because a
    seeded row was created by the system and not by any user, which is the whole
    of what created_by records. Rows an ADMIN creates carry that admin's user id
    instead, and that is the distinction the column exists to make."""
    session = SessionLocal()
    try:
        rows = session.execute(
            text("""
                SELECT 'agents' AS t, a.created_by, a.updated_by
                  FROM agents a
                 WHERE a.key IN ('record_stories','capture_discussion','general_support')
                UNION ALL
                SELECT 'agent_configs', c.created_by, c.updated_by
                  FROM agent_configs c JOIN agents a ON a.id = c.agent_id
                 WHERE a.key IN ('record_stories','capture_discussion','general_support')
                   AND c.version = 1
                UNION ALL
                SELECT 'capabilities', created_by, updated_by FROM capabilities
                 WHERE key IN ('listening_at_scale','sg_commons')
                   AND tenant_id = 'default' AND organization_id = 'default'
            """)
        ).fetchall()
    finally:
        session.close()

    assert rows
    for row in rows:
        assert row.created_by == "system", row.t
        assert row.updated_by == "system", row.t
