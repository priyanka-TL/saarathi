from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.core.settings import Settings
from app.tools.registry import ToolRegistry
from app.llm.factory import LlmFactory
from app.agents.factory import HandlerFactory, HandlerDeps
from app.services.agent_registry import AgentRegistry


@dataclass(frozen=True)
class Container:
    """The composition root. Plain constructor injection, no DI framework.

    Singletons (engine, tool registry, llm factory, handler factory, agent
    registry, authenticator) are built once, here. Per-request services are
    built fresh around the request's own db session elsewhere (src/api/deps.py,
    src/api/chat_routes.py) -- this dataclass only holds the long-lived pieces.
    """

    settings: Settings
    engine: Optional[Engine]
    session_factory: sessionmaker
    tool_registry: ToolRegistry
    llm_factory: LlmFactory
    handler_factory: HandlerFactory
    agent_registry: AgentRegistry
    authenticator: Any  # app.services.identity.Authenticator
    mitra_clients: Optional[Any] = None    # MitraClientRegistry | None (mitra_enabled gated)
    mitra_rest: Optional[Any] = None       # MitraRestClient | None -- the DEFAULT-scope client
    mitra_sessions: Optional[Any] = None   # MitraSessionManager | None (mitra_enabled gated)


def build_container(settings: Settings) -> Container:
    # Reuse the EXISTING module-level engine/session-factory singletons
    # (src/db/engine.py) rather than constructing a second engine here --
    # a fresh engine would open a second connection pool against the same
    # database that src/api/deps.py's SessionLocal() also uses.
    from app.database.engine import engine as shared_engine, SessionLocal as shared_session_factory

    # Tools self-register onto a MODULE-LEVEL singleton (app.tools.registry.registry)
    # via @registry.register(...) decorators, fired only when their defining
    # module is imported. `import app.tools` walks every submodule and triggers
    # that registration. It must happen here, explicitly and before anything
    # resolves a spec's `tools:` references -- otherwise POST /api/agents/{key}/config
    # would reject every tool as unknown, and a built handler would find none.
    import app.tools  # noqa: F401  (import for registration side effect only)
    from app.tools.registry import registry as tool_registry

    llm_factory = LlmFactory()

    # mitra_clients / mitra_sessions: built when mitra_enabled is set. When
    # disabled (the default) both are None and LlmAgentHandler is unaffected
    # -- it never touches these fields. RemoteFlowAgentHandler checks for
    # None and raises a clear error if an operator enables a remote_flow
    # agent without setting MITRA_BASE_URL etc.
    #
    # WHY A REGISTRY RATHER THAN A CLIENT. A MitraRestClient carries a base
    # URL, timeouts and the Origin credential -- all of which now resolve per
    # agent and per tenant (app/integrations/mitra/connection.py). One shared
    # client would serve every scope the FIRST scope's endpoint. The registry
    # hands out one client per distinct connection, cached by checksum, and is
    # shared with OrchestrationService so a turn and its finalisation use the
    # identical client rather than two independent pools.
    #
    # mitra_rest is the DEFAULT-scope client, kept for the finalisation paths
    # that still resolve the default snapshot. Those move onto the registry in
    # the next step, and this field goes with them.
    mitra_clients = None
    mitra_rest = None
    mitra_sessions = None
    if settings.mitra_enabled:
        from app.integrations.mitra.connection import (
            MitraClientRegistry,
            from_settings as build_mitra_connection,
        )
        from app.integrations.mitra.session_manager import MitraSessionManager
        mitra_clients = MitraClientRegistry()
        mitra_rest = mitra_clients.get(build_mitra_connection(settings))
        mitra_sessions = MitraSessionManager(settings)

    deps = HandlerDeps(
        llm_factory=llm_factory,
        tool_registry=tool_registry,
        mitra_clients=mitra_clients,
        mitra_sessions=mitra_sessions,
        settings=settings,
    )
    handler_factory = HandlerFactory(deps)
    agent_registry = AgentRegistry(
        ttl_s=settings.registry_ttl_s,
        # Hides remote_flow agents when Mitra is off. A runtime filter, not the
        # status write ConfigSyncService used to perform -- see AgentRegistry.
        mitra_enabled=bool(settings.mitra_enabled),
    )

    from app.services.identity import Authenticator
    authenticator = Authenticator(settings)

    return Container(
        settings=settings,
        engine=shared_engine,
        session_factory=shared_session_factory,
        tool_registry=tool_registry,
        llm_factory=llm_factory,
        handler_factory=handler_factory,
        agent_registry=agent_registry,
        authenticator=authenticator,
        mitra_clients=mitra_clients,
        mitra_rest=mitra_rest,
        mitra_sessions=mitra_sessions,
    )
