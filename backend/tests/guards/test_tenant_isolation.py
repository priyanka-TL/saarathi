"""THE cross-tenant leak guard.

HandlerFactory caches built handlers by `(spec.key, checksum)`
(app/agents/factory.py). An agent handler holds its spec -- INCLUDING ITS
SYSTEM PROMPT. So if two tenants' configurations differed while sharing a
checksum, the first tenant's handler would be served to the second, and one
tenant's prompt would answer another tenant's user.

That is the worst failure this design can produce, and it is silent: nothing
errors, the wrong prompt simply answers. These tests are what stand between the
schema and that outcome.

The property that prevents it: a tenant-scoped configuration is a WHOLE ROW in
agent_configs carrying its OWN checksum, computed from its own content -- not a
patch merged over a base which someone must remember to re-checksum. So
different content implies a different checksum implies a different cache key,
by construction.

The converse is asserted too: byte-identical configs SHOULD share a handler.
A "fix" that keyed the cache on tenant_id would pass the leak tests while
quietly multiplying handler instances per tenant, so both directions are pinned.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from pydantic import TypeAdapter

from app.agents.factory import HandlerDeps, HandlerFactory
from app.database.engine import SessionLocal
from app.domain.agent_spec import AgentSpec, canonical_json

TENANT_A = "tenant_alpha"
TENANT_B = "tenant_beta"
DEFAULT_SCOPE = "default"

_adapter = TypeAdapter(AgentSpec)


def _spec(key: str, prompt: str):
    """A validated AgentSpec.

    Validated rather than hand-built as a dict because `canonical_json` takes a
    spec instance -- and going through the real adapter is also what makes the
    checksums below the same ones production would compute.
    """
    return _adapter.validate_python({
        "schema_version": 1,
        "key": key,
        "name": f"Isolation {key}",
        "description": "tenant isolation fixture",
        "agent_type": "llm",
        "prompt": prompt,
        "model": {"provider": "openrouter", "name": "test-model"},
        "tools": [],
    })


@pytest.fixture()
def agent_with_scoped_configs():
    """One agent with three active configs: default, tenant A, tenant B.

    Three ACTIVE rows for one agent is legal precisely because
    uq_agent_cfg_one_active is unique per (agent_id, tenant_id,
    organization_id) rather than per agent -- that index is what makes
    per-tenant configuration representable at all.
    """
    session = SessionLocal()
    key = f"isolation_{uuid.uuid4().hex[:8]}"

    agent_id = session.execute(
        text("""
            INSERT INTO agents (key, name, description, agent_type, status,
                                tenant_id, organization_id)
            VALUES (:key, :name, 'isolation fixture', 'llm', 'enabled', 'default', 'default')
            RETURNING id
        """),
        {"key": key, "name": f"Isolation {key}"},
    ).scalar()

    prompts = {
        DEFAULT_SCOPE: "You are the DEFAULT prompt.",
        TENANT_A: "You are tenant ALPHA's prompt. Alpha secret: 111.",
        TENANT_B: "You are tenant BETA's prompt. Beta secret: 222.",
    }
    for version, (tenant_id, prompt) in enumerate(prompts.items(), start=1):
        canonical, checksum = canonical_json(_spec(key, prompt))
        session.execute(
            text("""
                INSERT INTO agent_configs (agent_id, tenant_id, organization_id, version,
                                           checksum, config, is_active, activated_at)
                VALUES (:agent_id, :tenant_id, 'default', :version, :checksum,
                        CAST(:config AS jsonb), TRUE, now())
            """),
            {"agent_id": agent_id, "tenant_id": tenant_id, "version": version,
             "checksum": checksum, "config": canonical},
        )
    session.commit()

    yield key, agent_id, prompts

    session.execute(text("DELETE FROM agent_configs WHERE agent_id = :id"), {"id": agent_id})
    session.execute(text("DELETE FROM audit_logs WHERE entity_id = :id"), {"id": agent_id})
    session.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})
    session.commit()
    session.close()


def _resolve(registry, session, key, tenant_id):
    registry.reload(session)
    base = registry.get_by_key_exact(key)
    assert base is not None, "the default-scope config must load into the snapshot"
    return registry.resolve_for_scope(session, base, tenant_id, DEFAULT_SCOPE)


def test_each_tenant_resolves_to_its_own_prompt(api_app, agent_with_scoped_configs):
    key, _agent_id, prompts = agent_with_scoped_configs
    registry = api_app.state.container.agent_registry
    session = SessionLocal()
    try:
        alpha = _resolve(registry, session, key, TENANT_A)
        beta = _resolve(registry, session, key, TENANT_B)
        default = _resolve(registry, session, key, DEFAULT_SCOPE)

        assert alpha.spec.prompt == prompts[TENANT_A]
        assert beta.spec.prompt == prompts[TENANT_B]
        assert default.spec.prompt == prompts[DEFAULT_SCOPE]
    finally:
        session.close()


def test_different_tenants_get_different_checksums(api_app, agent_with_scoped_configs):
    """The cache KEY must differ. This is the assertion that actually prevents
    the leak -- the prompt test above would still pass if resolution were right
    but the checksum were shared."""
    key, _agent_id, _prompts = agent_with_scoped_configs
    registry = api_app.state.container.agent_registry
    session = SessionLocal()
    try:
        alpha = _resolve(registry, session, key, TENANT_A)
        beta = _resolve(registry, session, key, TENANT_B)

        assert alpha.checksum != beta.checksum
        assert alpha.key == beta.key  # same agent, so the key alone is NOT enough
    finally:
        session.close()


def test_handler_factory_hands_the_two_tenants_different_handlers(
    api_app, agent_with_scoped_configs
):
    """End to end, through the real cache. If this fails, tenant B's user is
    being answered by tenant A's prompt."""
    key, _agent_id, prompts = agent_with_scoped_configs
    registry = api_app.state.container.agent_registry
    container = api_app.state.container
    session = SessionLocal()
    try:
        alpha = _resolve(registry, session, key, TENANT_A)
        beta = _resolve(registry, session, key, TENANT_B)

        factory = HandlerFactory(HandlerDeps(
            llm_factory=container.llm_factory,
            tool_registry=container.tool_registry,
            settings=container.settings,
        ))

        handler_a = factory.build(alpha.spec, alpha.checksum)
        handler_b = factory.build(beta.spec, beta.checksum)

        assert handler_a is not handler_b
        # `_spec` is the handler's private attribute; reaching for it is the
        # point of this assertion -- it is the prompt the handler will actually
        # send, not the one we asked for.
        assert handler_a._spec.prompt == prompts[TENANT_A]
        assert handler_b._spec.prompt == prompts[TENANT_B]
        # The cache holds two entries for one agent key -- the whole point.
        assert len(factory._cache) == 2
    finally:
        session.close()


def test_identical_configs_still_share_one_handler(api_app, agent_with_scoped_configs):
    """The converse. Keying the cache on tenant_id would pass every test above
    while quietly building one handler per tenant for identical specs; this is
    what catches that over-correction."""
    key, agent_id, _prompts = agent_with_scoped_configs
    registry = api_app.state.container.agent_registry
    container = api_app.state.container
    session = SessionLocal()
    try:
        # Give tenant B byte-identical content to tenant A.
        canonical, checksum = canonical_json(
            _spec(key, "You are tenant ALPHA's prompt. Alpha secret: 111.")
        )
        session.execute(
            text("""
                UPDATE agent_configs SET config = CAST(:config AS jsonb), checksum = :checksum
                WHERE agent_id = :agent_id AND tenant_id = :tenant_id
            """),
            {"config": canonical, "checksum": checksum,
             "agent_id": agent_id, "tenant_id": TENANT_B},
        )
        session.commit()
        registry._scope_cache.clear()

        alpha = _resolve(registry, session, key, TENANT_A)
        beta = _resolve(registry, session, key, TENANT_B)
        assert alpha.checksum == beta.checksum

        factory = HandlerFactory(HandlerDeps(
            llm_factory=container.llm_factory,
            tool_registry=container.tool_registry,
            settings=container.settings,
        ))
        assert factory.build(alpha.spec, alpha.checksum) is factory.build(beta.spec, beta.checksum)
    finally:
        session.close()


def test_a_tenant_without_its_own_config_gets_the_default(api_app, agent_with_scoped_configs):
    """The common path, and the one that must stay cheap: an uncustomised
    tenant is served the default snapshot entry unchanged."""
    key, _agent_id, prompts = agent_with_scoped_configs
    registry = api_app.state.container.agent_registry
    session = SessionLocal()
    try:
        stranger = _resolve(registry, session, key, f"tenant_{uuid.uuid4().hex[:8]}")
        assert stranger.spec.prompt == prompts[DEFAULT_SCOPE]
    finally:
        session.close()


def test_the_global_snapshot_is_the_default_scope_not_a_tenants(
    api_app, agent_with_scoped_configs
):
    """reload()'s scope filter. Without it the join returns one row per scope
    and the snapshot keeps whichever arrived last -- so a tenant's config could
    become what every OTHER tenant is served."""
    key, _agent_id, prompts = agent_with_scoped_configs
    registry = api_app.state.container.agent_registry
    session = SessionLocal()
    try:
        registry.reload(session)
        assert registry.get_by_key_exact(key).spec.prompt == prompts[DEFAULT_SCOPE]
    finally:
        session.close()


def test_the_scope_cache_does_not_confuse_two_tenants(api_app, agent_with_scoped_configs):
    """A cache keyed on the agent alone -- forgetting the tenant -- would serve
    whichever tenant asked first to everyone after it."""
    key, _agent_id, prompts = agent_with_scoped_configs
    registry = api_app.state.container.agent_registry
    session = SessionLocal()
    try:
        first = _resolve(registry, session, key, TENANT_A)
        second = _resolve(registry, session, key, TENANT_B)
        # Ask again in the opposite order; the cache is warm for both now.
        assert _resolve(registry, session, key, TENANT_B).spec.prompt == prompts[TENANT_B]
        assert _resolve(registry, session, key, TENANT_A).spec.prompt == prompts[TENANT_A]
        assert first.checksum != second.checksum
    finally:
        session.close()
