"""The Mitra half of the tenant-isolation guard.

tests/guards/test_tenant_isolation.py pins that two tenants get different
HANDLERS. These pin the paths that do NOT go through a handler.

WHAT THIS EXISTS TO CATCH
=========================
`handle_turn` has always corrected the default-scope snapshot with
`resolve_for_scope`. `resume_turn`, `finalize_now` and the report route did
not -- they read the agent with a bare `get_by_id`, which answers from the
process-wide ('default','default') cache. So a tenant's own `finalize_path`,
`report_media_type` and (once `remote.company` / `remote.bot_route` became spec
fields) its Mitra COMPANY were silently ignored on exactly the two operations
that submit the story and fetch the PDF.

That failure is silent in the worst way: v1 vs v2 finalize choose different PDF
renderers, and the wrong one produces a valid, downloadable, completely BLANK
report with nothing logged anywhere.

Every test below fails if `agent_for_session` reverts to `get_by_id`.
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from pydantic import TypeAdapter
from sqlalchemy import text

from app.database.engine import SessionLocal
from app.domain.agent_spec import AgentSpec, canonical_json
from app.integrations.mitra.connection import MitraClientRegistry
from app.services.orchestration import OrchestrationService

TENANT = "tenant_with_own_mitra"
DEFAULT_SCOPE = "default"

_adapter = TypeAdapter(AgentSpec)


def _remote_spec(key: str, *, company: str, finalize_path: str,
                 media_type: str = "application/pdf", base_url: str = None):
    remote = {
        "provider": "mitra",
        "flow_name": "guest-mi-story",
        "bot_route": "/guided_guest",
        "company": company,
        "finalize_path": finalize_path,
        "report_media_type": media_type,
    }
    if base_url:
        remote["connection"] = {"base_url": base_url}
    return _adapter.validate_python({
        "schema_version": 1,
        "key": key,
        "name": f"Mitra scope {key}",
        "description": "mitra scope fixture",
        "agent_type": "remote_flow",
        "remote": remote,
    })


@pytest.fixture()
def agent_with_scoped_mitra():
    """One remote_flow agent with two active configs pointing at DIFFERENT
    Mitra companies, endpoints and report types."""
    session = SessionLocal()
    key = f"mitrascope_{uuid.uuid4().hex[:8]}"

    agent_id = session.execute(
        text("""
            INSERT INTO agents (key, name, description, agent_type, status,
                                tenant_id, organization_id)
            VALUES (:key, :name, 'mitra scope fixture', 'remote_flow', 'enabled',
                    'default', 'default')
            RETURNING id
        """),
        {"key": key, "name": f"Mitra scope {key}"},
    ).scalar()

    specs = {
        DEFAULT_SCOPE: _remote_spec(
            key, company="default-company", finalize_path="/api/end-story/v2/",
        ),
        TENANT: _remote_spec(
            key, company="tenant-company", finalize_path="/api/end-story/",
            media_type="application/vnd.tenant+pdf",
            base_url="https://tenant-mitra.example.com",
        ),
    }
    for version, (tenant_id, spec) in enumerate(specs.items(), start=1):
        canonical, checksum = canonical_json(spec)
        session.execute(
            text("""
                INSERT INTO agent_configs (agent_id, tenant_id, organization_id, version,
                                           source, checksum, config, is_active, activated_at)
                VALUES (:agent_id, :tenant_id, 'default', :version, 'db', :checksum,
                        CAST(:config AS jsonb), TRUE, now())
            """),
            {"agent_id": agent_id, "tenant_id": tenant_id, "version": version,
             "checksum": checksum, "config": canonical},
        )
    session.commit()

    yield key, agent_id

    session.execute(text("DELETE FROM agent_configs WHERE agent_id = :id"), {"id": agent_id})
    session.execute(text("DELETE FROM audit_logs WHERE entity_id = :id"), {"id": agent_id})
    session.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})
    session.commit()
    session.close()


def _user(tenant_code: str):
    user = MagicMock()
    user.tenant_code = tenant_code
    user.active_org_id = None
    user.token = "user-token"
    return user


def _orch(api_app, session, **overrides):
    container = api_app.state.container
    kwargs = dict(
        session=session,
        registry=container.agent_registry,
        handler_factory=container.handler_factory,
        llm_factory=container.llm_factory,
        mitra_rest=MagicMock(),
        mitra_sessions=MagicMock(),
        mitra_clients=None,
        settings=container.settings,
    )
    kwargs.update(overrides)
    return OrchestrationService(**kwargs)


def _session_view(agent_id):
    view = MagicMock()
    view.agent_id = agent_id
    return view


# ---------------------------------------------------------------------------
# agent_for_session resolves the caller's scope
# ---------------------------------------------------------------------------


def test_a_tenants_finalize_path_is_honoured_not_the_default_scopes(
    api_app, agent_with_scoped_mitra
):
    """THE regression test. Before the fix this returned the default scope's
    '/api/end-story/v2/' for the tenant too -- and for a discussion flow, v2 is
    the endpoint that renders a blank PDF."""
    _key, agent_id = agent_with_scoped_mitra
    session = SessionLocal()
    try:
        orch = _orch(api_app, session)
        orch._registry.reload(session)

        agent = orch.agent_for_session(_session_view(agent_id), _user(TENANT))

        assert agent.spec.remote.finalize_path == "/api/end-story/"
        assert agent.spec.remote.company == "tenant-company"
        assert agent.spec.remote.report_media_type == "application/vnd.tenant+pdf"
    finally:
        session.close()


def test_a_tenant_without_its_own_config_still_gets_the_default(
    api_app, agent_with_scoped_mitra
):
    _key, agent_id = agent_with_scoped_mitra
    session = SessionLocal()
    try:
        orch = _orch(api_app, session)
        orch._registry.reload(session)

        agent = orch.agent_for_session(_session_view(agent_id), _user("some_other_tenant"))

        assert agent.spec.remote.finalize_path == "/api/end-story/v2/"
        assert agent.spec.remote.company == "default-company"
    finally:
        session.close()


def test_an_unknown_session_agent_resolves_to_none_rather_than_raising():
    orch = OrchestrationService.__new__(OrchestrationService)
    orch._registry = MagicMock()
    orch._registry.get_by_id.return_value = None
    orch._db = MagicMock()

    assert orch.agent_for_session(_session_view(uuid.uuid4()), _user(TENANT)) is None


# ---------------------------------------------------------------------------
# rest_for hands each scope its OWN client
# ---------------------------------------------------------------------------


def test_two_scopes_get_two_different_mitra_clients(api_app, agent_with_scoped_mitra):
    """A client carries the base URL, the timeouts and the Origin credential.
    Sharing one across scopes would send a tenant's story to another tenant's
    Mitra -- the REST analogue of the shared-handler leak."""
    _key, agent_id = agent_with_scoped_mitra
    session = SessionLocal()
    try:
        orch = _orch(api_app, session, mitra_clients=MitraClientRegistry())
        orch._registry.reload(session)

        tenant_agent = orch.agent_for_session(_session_view(agent_id), _user(TENANT))
        default_agent = orch.agent_for_session(_session_view(agent_id), _user(DEFAULT_SCOPE))

        tenant_rest = orch.rest_for(tenant_agent)
        default_rest = orch.rest_for(default_agent)

        assert tenant_rest is not default_rest
        assert tenant_rest._base_url == "https://tenant-mitra.example.com"
        assert default_rest._base_url == api_app.state.container.settings.mitra_base_url.rstrip("/")
    finally:
        session.close()


def test_the_same_scope_twice_reuses_one_client(api_app, agent_with_scoped_mitra):
    """One connection pool per configuration, not per call."""
    _key, agent_id = agent_with_scoped_mitra
    session = SessionLocal()
    try:
        orch = _orch(api_app, session, mitra_clients=MitraClientRegistry())
        orch._registry.reload(session)

        first = orch.rest_for(orch.agent_for_session(_session_view(agent_id), _user(TENANT)))
        second = orch.rest_for(orch.agent_for_session(_session_view(agent_id), _user(TENANT)))

        assert first is second
    finally:
        session.close()


# ---------------------------------------------------------------------------
# One unparseable config must not take down the catalogue
# ---------------------------------------------------------------------------


def test_one_broken_config_costs_one_agent_not_all_of_them(api_app, agent_with_scoped_mitra):
    """The blast radius of a bad config row.

    Config is written through the API now, with no YAML validated at startup,
    so a row can predate a schema change. reload() used to validate inside one
    try/except around the whole loop, so a single bad row emptied the snapshot
    -- and `sync_and_reload` turns an empty snapshot into a refusal to boot.
    One editable row could take down every agent, including unrelated ones.
    """
    _key, _agent_id = agent_with_scoped_mitra
    session = SessionLocal()
    broken_id = None
    try:
        broken_key = f"broken_{uuid.uuid4().hex[:8]}"
        broken_id = session.execute(
            text("""
                INSERT INTO agents (key, name, description, agent_type, status)
                VALUES (:key, :name, 'broken fixture', 'llm', 'enabled')
                RETURNING id
            """),
            {"key": broken_key, "name": f"Broken {broken_key}"},
        ).scalar()
        session.execute(
            text("""
                INSERT INTO agent_configs (agent_id, tenant_id, organization_id, version,
                                           source, checksum, config, is_active, activated_at)
                VALUES (:id, 'default', 'default', 1, 'db', 'brokensum',
                        CAST(:config AS jsonb), TRUE, now())
            """),
            # Passes the DDL's key/name/agent_type CHECK, fails the schema:
            # `prompt` and `model` are required for an llm agent.
            {"id": broken_id,
             "config": '{"key":"%s","name":"x","agent_type":"llm"}' % broken_key},
        )
        session.commit()

        registry = api_app.state.container.agent_registry
        registry.reload(session)

        assert registry.get_by_key_exact(broken_key) is None, "the bad agent is excluded"
        assert registry.get("general_support") is not None, "healthy agents survive"
        assert len(registry._snapshot) > 0, "the snapshot must not be emptied"
    finally:
        if broken_id is not None:
            session.execute(text("DELETE FROM agent_configs WHERE agent_id = :id"), {"id": broken_id})
            session.execute(text("DELETE FROM agents WHERE id = :id"), {"id": broken_id})
            session.commit()
            api_app.state.container.agent_registry.reload(session)
        session.close()
