"""POST /api/agents/{key}/config must reject a remote_flow config that would
fail SILENTLY at interview time.

THIS ROUTE IS THE ONLY GATE. There is no YAML and no startup config sync any
more, so a config reaches Mitra exactly as it was written here. The failures
below all produce a valid HTTP 200 from Mitra and a broken artefact -- without
this gate they are discovered by a user holding a blank PDF.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from starlette.testclient import TestClient

from app.database.engine import SessionLocal
from app.domain.core import OrgMembership, UserContext


@pytest.fixture()
def admin_client(flask_app, monkeypatch):
    flask_app.state.container.settings.saarthi_admin_enabled = 1
    admin_user = UserContext(
        user_id="remote_cfg_admin", email="admin@example.com", display_name="Admin",
        tenant_code="t", orgs=(OrgMembership(org_id="o", org_code="o", roles=("admin",)),),
        active_org_id="o",
    )
    monkeypatch.setattr(
        "app.services.identity.Authenticator.authenticate",
        lambda self, token=None: admin_user,
    )
    return TestClient(
        flask_app,
        raise_server_exceptions=False,
        headers={"Authorization": "Bearer test-admin-token"},
    )


@pytest.fixture()
def remote_agent():
    """A remote_flow agent row to hang config versions off."""
    session = SessionLocal()
    key = f"remotecfg_{uuid.uuid4().hex[:8]}"
    agent_id = session.execute(
        text("""
            INSERT INTO agents (key, name, description, agent_type, status)
            VALUES (:key, :name, 'remote config fixture', 'remote_flow', 'enabled')
            RETURNING id
        """),
        {"key": key, "name": f"Remote Config {key}"},
    ).scalar()
    session.commit()

    yield key, agent_id

    session.execute(text("DELETE FROM agent_configs WHERE agent_id = :id"), {"id": agent_id})
    session.execute(text("DELETE FROM audit_logs WHERE entity_id = :id"), {"id": agent_id})
    session.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})
    session.commit()
    session.close()


#: Fields that live in the PROVIDER's own options block rather than in the
#: envelope. A test names them at the top level for readability; this routes
#: them, which also documents where each one ended up.
_OPTION_FIELDS = frozenset({
    "bot_route", "company", "finalize_path", "finalize_as_guest", "paths",
})


def _body(key: str, **remote_overrides) -> dict:
    from tests.provider_factories import remote_dict

    options = dict(remote_overrides.pop("options", None) or {})
    for field in list(remote_overrides):
        if field in _OPTION_FIELDS:
            options[field] = remote_overrides.pop(field)

    remote = remote_dict(
        "mitra",
        options={"company": "some-company", "finalize_path": "/api/end-story/", **options},
        flow_name="guest-mi-story",
        **remote_overrides,
    )
    return {
        "key": key,
        "name": f"Remote Config {key}",
        "description": "posted through the admin API",
        "agent_type": "remote_flow",
        "remote": remote,
    }


def test_a_valid_remote_config_is_accepted(admin_client, remote_agent):
    key, _agent_id = remote_agent
    res = admin_client.post(f"/api/agents/{key}/config", json=_body(key))
    assert res.status_code == 200, res.json()
    assert res.json()["version"] == 1


def test_an_unrecognised_finalize_path_is_rejected(admin_client, remote_agent):
    """Anything not matching a configured endpoint falls through to the v1
    branch and finalises with the wrong body shape -- which Mitra accepts and
    renders as an empty report."""
    key, _agent_id = remote_agent
    res = admin_client.post(
        f"/api/agents/{key}/config", json=_body(key, finalize_path="/api/end-storyy/"),
    )

    assert res.status_code == 422
    body = res.json()
    assert body["error"] == "CONFIG_INVALID"
    assert body["path"] == ["remote", "options", "finalize_path"]


def test_a_finalize_path_is_checked_against_this_specs_own_endpoints(
    admin_client, remote_agent
):
    """A scope may carry its own endpoint paths. Validating against the shipped
    pair would reject this correct config."""
    key, _agent_id = remote_agent
    res = admin_client.post(
        f"/api/agents/{key}/config",
        json=_body(
            key,
            finalize_path="/api/end-story/v3/",
            paths={"finalize_v2": "/api/end-story/v3/"},
        ),
    )

    assert res.status_code == 200, res.json()


def test_an_empty_bot_route_is_rejected_by_the_schema(admin_client, remote_agent):
    """Not deferred to Mitra: an empty bot route resolves the wrong CompanyBot
    rather than erroring, so the interview asks the wrong questions."""
    key, _agent_id = remote_agent
    res = admin_client.post(f"/api/agents/{key}/config", json=_body(key, bot_route=""))

    assert res.status_code == 422
    assert res.json()["error"] == "CONFIG_INVALID"
    assert "bot_route" in str(res.json())


def test_an_empty_company_is_rejected_by_the_schema(admin_client, remote_agent):
    """Mitra identifies a profile by (email, company); an empty company splits
    or merges story histories rather than erroring."""
    key, _agent_id = remote_agent
    res = admin_client.post(f"/api/agents/{key}/config", json=_body(key, company=""))

    assert res.status_code == 422
    assert "company" in str(res.json())


def test_a_missing_bot_route_is_rejected(admin_client, remote_agent):
    """Absent entirely, not just empty -- there is no environment fallback to
    fill it in any more."""
    key, _agent_id = remote_agent
    body = _body(key)
    del body["remote"]["options"]["bot_route"]

    res = admin_client.post(f"/api/agents/{key}/config", json=body)

    assert res.status_code == 422
    assert "bot_route" in str(res.json())


def test_the_old_env_indirection_is_rejected_rather_than_silently_ignored(
    admin_client, remote_agent, monkeypatch
):
    """`bot_route_env` / `company_env` are gone. BaseAgentSpec is extra="forbid",
    so a config still written in the old shape fails loudly instead of coming up
    with an empty bot route."""
    key, _agent_id = remote_agent
    monkeypatch.setenv("MITRA_SOMEWHERE_BOT_ROUTE", "/guided_guest")
    body = _body(key)
    del body["remote"]["options"]["bot_route"]
    body["remote"]["options"]["bot_route_env"] = "MITRA_SOMEWHERE_BOT_ROUTE"

    res = admin_client.post(f"/api/agents/{key}/config", json=body)

    assert res.status_code == 422


def test_a_rejected_config_writes_no_version(admin_client, remote_agent):
    """422 must mean nothing happened -- a partially applied config would
    deactivate the working version and leave the agent unservable."""
    key, agent_id = remote_agent
    admin_client.post(f"/api/agents/{key}/config", json=_body(key))

    admin_client.post(
        f"/api/agents/{key}/config", json=_body(key, finalize_path="/nope/"),
    )

    session = SessionLocal()
    try:
        rows = session.execute(
            text("SELECT version, is_active FROM agent_configs WHERE agent_id = :id"),
            {"id": agent_id},
        ).fetchall()
    finally:
        session.close()

    assert [(r.version, r.is_active) for r in rows] == [(1, True)]
