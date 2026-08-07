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
    mitra_sessions: Optional[Any] = None   # MitraSessionManager | None (mitra_enabled gated)
    bhashini: Optional[Any] = None         # BhashiniClient | None (voice_enabled gated)
    object_store: Optional[Any] = None     # ObjectStore | None (voice_enabled gated)


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
    # None and raises a clear error if an operator enables a remote_flow agent
    # without Mitra wired up.
    #
    # WHY A REGISTRY RATHER THAN A CLIENT. A MitraRestClient carries a base
    # URL, timeouts and the Origin credential -- all of which resolve per agent
    # and per tenant (app/integrations/mitra/connection.py). One shared client
    # would serve every scope the FIRST scope's endpoint. The registry hands
    # out one client per distinct connection, cached by checksum, and is shared
    # with OrchestrationService so a turn and its finalisation use the identical
    # client rather than two independent pools.
    #
    # THERE IS NO DEFAULT-SCOPE CLIENT ANY MORE. There used to be one, built
    # from the MITRA_* settings at boot. Those settings are gone -- the endpoint
    # now lives in `remote.connection` on the agent config -- so there is no
    # connection to build before an agent spec is in hand, and nothing to build
    # it from. Every caller goes through the registry with a resolved spec.
    mitra_clients = None
    mitra_sessions = None
    if settings.mitra_enabled:
        from app.integrations.mitra.connection import MitraClientRegistry
        from app.integrations.mitra.session_manager import MitraSessionManager
        mitra_clients = MitraClientRegistry()
        mitra_sessions = MitraSessionManager(settings)

    # bhashini / object_store: built when voice_enabled is set. Both None when
    # disabled, and app/routers/voice.py turns that into 503 VOICE_DISABLED --
    # the same gating shape mitra_enabled uses above.
    #
    # WHY BOTH OR NEITHER. Voice needs a place to put the recording AND a
    # service to transcribe it; either one alone serves no request. Building
    # them together means a half-configured deployment fails at boot with a
    # message, rather than answering upload-url and then failing at transcribe.
    #
    # A missing ffmpeg only WARNS. It is a system binary rather than a Python
    # dependency, so it is the one piece that a working `make install` cannot
    # guarantee -- and refusing to boot the whole app over a feature flag that
    # may not even be exercised would be the wrong trade.
    bhashini = None
    object_store = None
    if settings.voice_enabled:
        import logging

        from app.integrations.bhashini import BhashiniClient
        from app.integrations.bhashini.audio import ffmpeg_available
        from app.integrations.storage import get_object_store

        if not settings.bhashini_authorization:
            raise RuntimeError(
                "VOICE_ENABLED=1 but BHASHINI_AUTHORIZATION is not set. "
                "Set the Bhashini credentials in .env, or set VOICE_ENABLED=0."
            )
        if not ffmpeg_available():
            logging.getLogger(__name__).warning(
                "VOICE_ENABLED=1 but ffmpeg is not on PATH. Speech-to-text will "
                "fail at request time. Install it: `brew install ffmpeg` or "
                "`apt-get install -y ffmpeg`."
            )

        # Raises StorageConfigError at BOOT on a bad provider or a missing
        # bucket -- a failed deploy rather than an outage mid-turn.
        object_store = get_object_store(settings)

        # Supported, because a deployment may genuinely want it, but never
        # silent: voice recordings are user speech, and on a public bucket
        # anyone who guesses a key can listen to them. A warning in the startup
        # log is the difference between a decision and an accident.
        if object_store.bucket_type == "public":
            logging.getLogger(__name__).warning(
                "VOICE_ENABLED=1 with CLOUD_STORAGE_BUCKET_TYPE=public: voice "
                "recordings in bucket %r will be WORLD-READABLE. Recordings are "
                "user speech (PII) -- set CLOUD_STORAGE_BUCKET_TYPE=private "
                "unless this is deliberate.",
                getattr(object_store, "bucket", "?"),
            )
        bhashini = BhashiniClient(
            base_url=settings.bhashini_base_url,
            authorization=settings.bhashini_authorization,
            api_key=settings.bhashini_api_key,
            user_id=settings.bhashini_user_id,
            connect_timeout=settings.bhashini_connect_timeout,
            read_timeout=settings.bhashini_read_timeout,
            chunk_duration_s=settings.voice_chunk_duration_s,
            tts_byte_limit=settings.voice_tts_byte_limit,
            asr_max_workers=settings.voice_asr_max_workers,
            ffmpeg_timeout_s=settings.voice_ffmpeg_timeout_s,
        )

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
        mitra_sessions=mitra_sessions,
        bhashini=bhashini,
        object_store=object_store,
    )
