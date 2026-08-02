"""The full config versioning lifecycle (design doc §5.5, §10.2):

  YAML v1 -> API override v2 -> restart -> YAML v3 stored inactive, v2 still
  active, drift reported -> rollback to v1 -> works.

Real Postgres, an isolated tmp_path YAML directory (never touches the real
src/config/agents/*.yaml files) with one throwaway `agent_type: llm` test
agent. Config overrides are posted through the real admin API (not simulated
by hand), exercising the actual POST /config and /activate endpoints.

IMPORTANT: sync()'s orphan-detection (§5.5 case 3) disables every agent whose
active config is yaml-sourced and whose key isn't present in the yaml_dir
being synced. Since these tests deliberately point sync() at an ISOLATED
tmp_path containing only one throwaway agent, every real shipped agent
(general_support, research, ...) would otherwise get disabled as a side
effect -- confirmed the hard way: an earlier run of this file left the real
`agents` table with general_support/health_wellness/research/technical_support
all 'disabled', which cascaded into unrelated test failures across the whole
suite. The autouse fixture below snapshots and restores every agent's status
around each test so this file can never do that again.
"""
from __future__ import annotations
from starlette.testclient import TestClient

import json
import uuid

import pytest
from sqlalchemy import text

from app.database.engine import SessionLocal
from app.domain.core import OrgMembership, UserContext
from app.services.config_sync import ConfigSyncService


@pytest.fixture(autouse=True)
def _protect_real_agent_statuses(flask_app):
    """See the module docstring: sync() against an isolated tmp_path
    orphan-disables every agent it doesn't know about. Snapshot every
    agent's status before the test and restore it after, so this file's
    isolated syncs can never leak damage into the shared dev database. Also
    deletes any agent rows THIS test created (its own vertest_* throwaway
    agent) -- a repeated `pytest` run must not accumulate a new enabled
    agent row every time, which would eventually break test_agents_endpoint.py's
    "exactly 5 agents" golden assertion exactly as test_admin_routes.py's own
    uncleaned test fixtures were separately found to do.

    Also forces one final container.agent_registry.reload() after cleanup:
    the admin API calls this test makes (POST /config, /activate) force a
    LIVE reload of the session-scoped, in-memory registry mid-test, so
    without this it would keep serving a stale snapshot that still includes
    the just-deleted vertest agent for the rest of the pytest process.
    """
    session = SessionLocal()
    try:
        snapshot_ids = {
            row[0] for row in session.execute(text("SELECT id FROM agents")).fetchall()
        }
        status_snapshot = {
            row[0]: row[1]
            for row in session.execute(text("SELECT id, status FROM agents")).fetchall()
        }
    finally:
        session.close()

    yield

    session = SessionLocal()
    try:
        for agent_id, status in status_snapshot.items():
            session.execute(
                text("UPDATE agents SET status = :status WHERE id = :id"),
                {"status": status, "id": agent_id},
            )

        new_ids = {
            row[0] for row in session.execute(text("SELECT id FROM agents")).fetchall()
        } - snapshot_ids
        for agent_id in new_ids:
            session.execute(text("DELETE FROM agent_configs WHERE agent_id = :id"), {"id": agent_id})
            session.execute(text("DELETE FROM audit_logs WHERE entity_id = :id"), {"id": agent_id})
            session.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})

        session.commit()
        flask_app.state.container.agent_registry.reload(session)
    finally:
        session.close()


def _write_yaml(yaml_dir, key: str, name: str, description: str) -> None:
    (yaml_dir / "agent.yaml").write_text(f"""
schema_version: 1
key: {key}
name: "{name}"
description: "{description}"
agent_type: llm
prompt: "You are a versioning test agent."
model: {{ provider: openrouter, name: test-model }}
tools: []
""")


def _agent_id(session, key: str) -> uuid.UUID:
    row = session.execute(text("SELECT id FROM agents WHERE key = :key"), {"key": key}).fetchone()
    assert row is not None
    return row[0]


def _configs(session, agent_id: uuid.UUID):
    """[(version, source, is_active, checksum), ...] ordered by version."""
    rows = session.execute(
        text("""
            SELECT version, source, is_active, checksum FROM agent_configs
            WHERE agent_id = :agent_id ORDER BY version
        """),
        {"agent_id": agent_id},
    ).fetchall()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


def _active(session, agent_id: uuid.UUID):
    return next(c for c in _configs(session, agent_id) if c[2])


def _drift_audit_rows(session, agent_id: uuid.UUID):
    return session.execute(
        text("""
            SELECT action, note FROM audit_logs
            WHERE entity_id = :agent_id AND note LIKE 'drift:%'
        """),
        {"agent_id": agent_id},
    ).fetchall()


@pytest.fixture()
def admin_client(flask_app, monkeypatch):
    """A Flask test client authenticated as an admin -- mirrors
    tests/integration/test_admin_routes.py's own established pattern."""
    flask_app.state.container.settings.saarthi_admin_enabled = 1
    admin_user = UserContext(
        user_id="versioning_admin", email="admin@example.com", display_name="Admin",
        tenant_code="t", orgs=(OrgMembership(org_id="o", org_code="o", roles=("admin",)),),
        active_org_id="o",
    )
    # Identity is resolved once from configuration, not per request -- there
    # is no login flow upstream of this API -- so patching `authenticate()` is
    # sufficient.
    monkeypatch.setattr(
        "app.services.identity.Authenticator.authenticate",
        lambda self: admin_user,
    )
    return TestClient(
        flask_app,
        raise_server_exceptions=False,
        headers={"Authorization": "Bearer test-admin-token"},
    )


def _v2_body(key: str) -> dict:
    return {
        "key": key,
        "name": "Versioning Test Agent",
        "description": "v2 -- posted through the admin API",
        "agent_type": "llm",
        "prompt": "You are the v2 override.",
        "model": {"name": "test-model"},
        "tools": [],
    }


def test_full_versioning_lifecycle(tmp_path, admin_client):
    key = f"vertest_{uuid.uuid4().hex[:8]}"
    name = f"Versioning Test {uuid.uuid4().hex[:8]}"
    _write_yaml(tmp_path, key, name, "v1 -- original yaml")

    svc = ConfigSyncService()
    session = SessionLocal()
    try:
        report1 = svc.sync(session, tmp_path, mode="safe")
        assert key in report1.created
        agent_id = _agent_id(session, key)
        v1 = _active(session, agent_id)
        assert v1 == (1, "yaml", True, v1[3])
    finally:
        session.close()

    # API override -> v2, source='db', activated.
    res = admin_client.post(f"/api/agents/{key}/config", json=_v2_body(key))
    assert res.status_code == 200, res.json()
    assert res.json()["version"] == 2

    verify = SessionLocal()
    try:
        active = _active(verify, agent_id)
        assert active[0] == 2
        assert active[1] == "db"
    finally:
        verify.close()

    # "Restart": the YAML changes (v3) while the db override is still active.
    _write_yaml(tmp_path, key, name, "v3 -- yaml changed after the override")
    session2 = SessionLocal()
    try:
        report2 = svc.sync(session2, tmp_path, mode="safe")
        assert key in report2.drifted
        assert key not in report2.updated

        configs = _configs(session2, agent_id)
        v3 = next(c for c in configs if c[0] == 3)
        assert v3[1] == "yaml"
        assert v3[2] is False  # stored inactive

        active = _active(session2, agent_id)
        assert active[0] == 2
        assert active[1] == "db"

        drift_rows = _drift_audit_rows(session2, agent_id)
        assert len(drift_rows) == 1
        assert drift_rows[0][0] == "config_sync"
    finally:
        session2.close()

    # Rollback to v1 via the real admin API.
    res = admin_client.post(f"/api/agents/{key}/config/1/activate")
    assert res.status_code == 200, res.json()

    final = SessionLocal()
    try:
        active = _active(final, agent_id)
        assert active[0] == 1
        assert active[1] == "yaml"

        rollback_audit = final.execute(
            text("""
                SELECT action FROM audit_logs
                WHERE entity_id = :agent_id AND action = 'config_activate'
                AND note LIKE 'Activated version 1%'
            """),
            {"agent_id": agent_id},
        ).fetchone()
        assert rollback_audit is not None
    finally:
        final.close()


def test_drift_reported_on_every_subsequent_boot(tmp_path, admin_client):
    key = f"vertest_{uuid.uuid4().hex[:8]}"
    name = f"Versioning Test {uuid.uuid4().hex[:8]}"
    _write_yaml(tmp_path, key, name, "v1")

    svc = ConfigSyncService()
    session = SessionLocal()
    svc.sync(session, tmp_path, mode="safe")
    agent_id = _agent_id(session, key)
    session.close()

    res = admin_client.post(f"/api/agents/{key}/config", json=_v2_body(key))
    assert res.status_code == 200

    _write_yaml(tmp_path, key, name, "v3 -- yaml changed")
    session2 = SessionLocal()
    report_boot2 = svc.sync(session2, tmp_path, mode="safe")
    session2.close()
    assert key in report_boot2.drifted

    # "boot 3" -- no further yaml edits, but the db override is still active.
    session3 = SessionLocal()
    try:
        report_boot3 = svc.sync(session3, tmp_path, mode="safe")
        assert key in report_boot3.drifted, "drift must be reported again, not silently 'unchanged'"
        assert key not in report_boot3.unchanged

        drift_rows = _drift_audit_rows(session3, agent_id)
        assert len(drift_rows) == 2, "each boot with a live override must write its own drift audit row"
    finally:
        session3.close()


def test_force_mode_discards_override_even_when_yaml_unchanged(tmp_path, admin_client):
    key = f"vertest_{uuid.uuid4().hex[:8]}"
    name = f"Versioning Test {uuid.uuid4().hex[:8]}"
    _write_yaml(tmp_path, key, name, "v1")

    svc = ConfigSyncService()
    session = SessionLocal()
    svc.sync(session, tmp_path, mode="safe")
    agent_id = _agent_id(session, key)
    session.close()

    res = admin_client.post(f"/api/agents/{key}/config", json=_v2_body(key))
    assert res.status_code == 200

    _write_yaml(tmp_path, key, name, "v3 -- yaml changed")
    session2 = SessionLocal()
    svc.sync(session2, tmp_path, mode="safe")
    session2.close()

    # Force, with NO further yaml edits since the last sync (still v3 content).
    session3 = SessionLocal()
    try:
        report = svc.sync(session3, tmp_path, mode="force")
        assert key in report.updated
        assert key not in report.drifted

        active = _active(session3, agent_id)
        assert active[1] == "yaml"

        configs = _configs(session3, agent_id)
        db_version = next(c for c in configs if c[1] == "db")
        assert db_version[2] is False, "the db override must be deactivated"
    finally:
        session3.close()


def test_mode_off_skips_reconciliation_entirely(tmp_path, admin_client):
    key = f"vertest_{uuid.uuid4().hex[:8]}"
    name = f"Versioning Test {uuid.uuid4().hex[:8]}"
    _write_yaml(tmp_path, key, name, "v1")

    svc = ConfigSyncService()
    session = SessionLocal()
    svc.sync(session, tmp_path, mode="safe")
    agent_id = _agent_id(session, key)
    session.close()

    res = admin_client.post(f"/api/agents/{key}/config", json=_v2_body(key))
    assert res.status_code == 200

    _write_yaml(tmp_path, key, name, "v3 -- yaml changed, but mode=off must ignore this entirely")
    before = SessionLocal()
    configs_before = _configs(before, agent_id)
    before.close()

    session2 = SessionLocal()
    try:
        report = svc.sync(session2, tmp_path, mode="off")
        assert report.as_dict() == {
            "created": [], "updated": [], "unchanged": [], "drifted": [], "orphaned": [],
        }
        configs_after = _configs(session2, agent_id)
        assert configs_after == configs_before, "mode=off must not write anything at all"
    finally:
        session2.close()
