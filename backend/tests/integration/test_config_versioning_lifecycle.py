"""The config versioning lifecycle, end to end through the real admin API.

  seed v1 -> API override v2 -> v3 -> roll back to v1 -> still works.

THIS FILE USED TO BE ABOUT YAML. It tested `ConfigSyncService` reconciling
`app/config/agents/*.yaml` against the database: drift detection, `force` mode
reclaiming a live override, `off` mode skipping reconciliation, and
orphan-disabling agents whose file had been deleted. None of that exists any
more -- the database is the only source of agent configuration, seeded by
migration 0007 and edited here.

What survives is the part that was always the point: **a config change is a new
VERSION, the previous one stays readable, and activating an older version is a
complete rollback.** That is what makes editing production configuration at
runtime a safe operation rather than a destructive one, and it is now the only
way configuration changes at all.

Real Postgres, a throwaway `agent_type: llm` agent per test, and the real
`POST /config` / `POST /{version}/activate` endpoints rather than hand-written
SQL.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from starlette.testclient import TestClient

from app.database.engine import SessionLocal
from app.domain.core import OrgMembership, UserContext


@pytest.fixture(autouse=True)
def _cleanup_created_agents(flask_app):
    """Delete any agent row THIS test created.

    A repeated `pytest` run must not accumulate a new enabled agent row every
    time, which would eventually break test_agents_endpoint.py's "exactly N
    agents" golden assertion -- exactly as this file's own uncleaned fixtures
    were once found to do.

    Also forces one final registry reload after cleanup: the admin calls here
    force a LIVE reload of the in-memory registry mid-test, so without this it
    would keep serving a snapshot that still includes the deleted agent for the
    rest of the pytest process.
    """
    session = SessionLocal()
    try:
        before = {row[0] for row in session.execute(text("SELECT id FROM agents")).fetchall()}
    finally:
        session.close()

    yield

    session = SessionLocal()
    try:
        after = {row[0] for row in session.execute(text("SELECT id FROM agents")).fetchall()}
        for agent_id in after - before:
            session.execute(text("DELETE FROM agent_configs WHERE agent_id = :id"), {"id": agent_id})
            session.execute(text("DELETE FROM audit_logs WHERE entity_id = :id"), {"id": agent_id})
            session.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})
        session.commit()
        flask_app.state.container.agent_registry.reload(session)
    finally:
        session.close()


@pytest.fixture()
def admin_client(flask_app, monkeypatch):
    """A test client authenticated as an admin -- mirrors
    tests/integration/test_admin_routes.py's own established pattern."""
    flask_app.state.container.settings.saarthi_admin_enabled = 1
    admin_user = UserContext(
        user_id="versioning_admin", email="admin@example.com", display_name="Admin",
        tenant_code="t", orgs=(OrgMembership(org_id="o", org_code="o", roles=("admin",)),),
        active_org_id="o",
    )
    # Identity is resolved once from configuration, not per request -- there is
    # no login flow upstream of this API -- so patching `authenticate()` is
    # sufficient.
    monkeypatch.setattr(
        "app.services.identity.Authenticator.authenticate", lambda self: admin_user,
    )
    return TestClient(
        flask_app,
        raise_server_exceptions=False,
        headers={"Authorization": "Bearer test-admin-token"},
    )


@pytest.fixture()
def seeded_agent():
    """One agent with an active v1, standing in for what migration 0007 does
    for the shipped catalogue."""
    session = SessionLocal()
    key = f"vertest_{uuid.uuid4().hex[:8]}"
    agent_id = session.execute(
        text("""
            INSERT INTO agents (key, name, description, agent_type, status)
            VALUES (:key, :name, 'versioning fixture', 'llm', 'enabled')
            RETURNING id
        """),
        {"key": key, "name": f"Versioning {key}"},
    ).scalar()

    from app.domain.agent_spec import AgentSpec, canonical_json
    from pydantic import TypeAdapter

    spec = TypeAdapter(AgentSpec).validate_python(_body(key, "v1 -- the seeded config"))
    config, checksum = canonical_json(spec)
    session.execute(
        text("""
            INSERT INTO agent_configs (agent_id, tenant_id, organization_id, version,
                                       source, checksum, config, is_active, activated_at)
            VALUES (:agent_id, 'default', 'default', 1, 'db', :checksum,
                    CAST(:config AS jsonb), TRUE, now())
        """),
        {"agent_id": agent_id, "checksum": checksum, "config": config},
    )
    session.commit()
    session.close()
    return key, agent_id


def _body(key: str, prompt: str) -> dict:
    return {
        "key": key,
        "name": f"Versioning {key}",
        "description": "posted through the admin API",
        "agent_type": "llm",
        "prompt": prompt,
        "model": {"name": "test-model"},
        "tools": [],
    }


def _configs(session, agent_id):
    return session.execute(
        text("""
            SELECT version, source, is_active, checksum FROM agent_configs
            WHERE agent_id = :id ORDER BY version
        """),
        {"id": agent_id},
    ).fetchall()


def _active(session, agent_id):
    return session.execute(
        text("SELECT version, source FROM agent_configs WHERE agent_id = :id AND is_active"),
        {"id": agent_id},
    ).fetchone()


def _prompt_in_registry(flask_app, key: str) -> str:
    session = SessionLocal()
    try:
        flask_app.state.container.agent_registry.reload(session)
    finally:
        session.close()
    return flask_app.state.container.agent_registry.get(key).spec.prompt


# ---------------------------------------------------------------------------


def test_full_versioning_lifecycle(admin_client, seeded_agent, flask_app):
    key, agent_id = seeded_agent

    res = admin_client.post(f"/api/agents/{key}/config", json=_body(key, "v2 -- the override"))
    assert res.status_code == 200, res.json()
    assert res.json()["version"] == 2

    res = admin_client.post(f"/api/agents/{key}/config", json=_body(key, "v3 -- another edit"))
    assert res.status_code == 200
    assert res.json()["version"] == 3

    session = SessionLocal()
    try:
        assert _active(session, agent_id)[0] == 3
        # EXACTLY ONE active row per scope -- uq_agent_cfg_one_active. A second
        # would make which config serves a turn a matter of row order.
        assert sum(1 for c in _configs(session, agent_id) if c.is_active) == 1
    finally:
        session.close()

    assert _prompt_in_registry(flask_app, key) == "v3 -- another edit"

    # ROLL BACK to the seeded config. This is the whole reason versions exist:
    # a bad production edit is undone by one call, not by re-deriving what the
    # config used to say.
    res = admin_client.post(f"/api/agents/{key}/config/1/activate")
    assert res.status_code == 200, res.json()
    assert res.json()["version"] == 1

    session = SessionLocal()
    try:
        assert _active(session, agent_id)[0] == 1
        # Nothing is deleted by a rollback -- v2 and v3 are still there to roll
        # forward to.
        assert [c.version for c in _configs(session, agent_id)] == [1, 2, 3]
    finally:
        session.close()

    assert _prompt_in_registry(flask_app, key) == "v1 -- the seeded config"


def test_version_list_is_newest_first_and_marks_the_active_one(admin_client, seeded_agent):
    key, _agent_id = seeded_agent
    admin_client.post(f"/api/agents/{key}/config", json=_body(key, "v2"))

    res = admin_client.get(f"/api/agents/{key}/config/versions")
    assert res.status_code == 200
    versions = res.json()

    assert [v["version"] for v in versions] == [2, 1]
    assert [v["is_active"] for v in versions] == [True, False]


def test_each_version_carries_its_own_checksum(admin_client, seeded_agent):
    """The checksum is HandlerFactory's cache key alongside the agent key, so
    two different configs sharing one would serve the wrong handler -- and,
    across tenants, the wrong system prompt. See tests/guards/test_tenant_isolation.py."""
    key, agent_id = seeded_agent
    admin_client.post(f"/api/agents/{key}/config", json=_body(key, "v2 -- different content"))

    session = SessionLocal()
    try:
        checksums = [c.checksum for c in _configs(session, agent_id)]
    finally:
        session.close()

    assert len(set(checksums)) == len(checksums)


def test_activating_an_unknown_version_is_a_400_and_changes_nothing(admin_client, seeded_agent):
    key, agent_id = seeded_agent

    res = admin_client.post(f"/api/agents/{key}/config/99/activate")
    assert res.status_code == 400
    assert res.json()["error"] == "INVALID_REQUEST"

    session = SessionLocal()
    try:
        # Still exactly one active row: the failed activate must not have run
        # its blanket deactivate and left the agent with none.
        assert _active(session, agent_id)[0] == 1
    finally:
        session.close()


def test_an_invalid_config_is_rejected_and_writes_no_version(admin_client, seeded_agent):
    key, agent_id = seeded_agent
    bad = _body(key, "v2")
    bad["model"] = {"name": "test-model", "temperature": 99}  # ModelSpec caps at 2.0

    res = admin_client.post(f"/api/agents/{key}/config", json=bad)
    assert res.status_code == 422
    assert res.json()["error"] == "CONFIG_INVALID"

    session = SessionLocal()
    try:
        assert [c.version for c in _configs(session, agent_id)] == [1]
        assert _active(session, agent_id)[0] == 1
    finally:
        session.close()


def test_config_for_an_unknown_agent_is_a_404(admin_client):
    res = admin_client.post("/api/agents/nosuchagent/config", json=_body("nosuchagent", "x"))
    assert res.status_code == 404
    assert res.json()["error"] == "AGENT_NOT_FOUND"
