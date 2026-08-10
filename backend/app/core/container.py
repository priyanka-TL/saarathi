"""The composition root.

Responsible for: building every long-lived singleton, once, at startup.
Used by: create_app(), which stashes the result on app.state.

Per-request services are built fresh around the request's own session in
app/dependencies/; this module only holds what outlives a request.
"""
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.core.logger import get_logger
from app.core.settings import Settings
from app.tools.registry import ToolRegistry
from app.llm.factory import LlmFactory
from app.agents.factory import HandlerFactory, HandlerDeps
from app.services.agent_registry import AgentRegistry

logger = get_logger(__name__)


@dataclass(frozen=True)
class Container:
    """Plain constructor injection, no DI framework. Frozen on purpose."""

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
    saathi_tokens: Optional[Any] = None    # TokenProvider | None (saathi_enabled gated)
    saathi_sessions: Optional[Any] = None  # SaathiSessionManager | None (saathi_enabled gated)
    bhashini: Optional[Any] = None         # BhashiniClient | None (voice_enabled gated)
    object_store: Optional[Any] = None     # ObjectStore | None (voice_enabled gated)


def build_container(settings: Settings) -> Container:
    # Reuse the module-level singletons rather than building a second engine,
    # which would open a second pool against the same database.
    from app.database.engine import engine as shared_engine, SessionLocal as shared_session_factory

    # Tools self-register via decorators fired on import. This walks every
    # submodule to trigger that, and must run before anything resolves a spec's
    # `tools:` -- otherwise every tool is rejected as unknown.
    import app.tools  # noqa: F401  (import for registration side effect only)
    from app.tools.registry import registry as tool_registry

    llm_factory = LlmFactory()

    # A REGISTRY, not a client: base URL, timeouts and the Origin credential
    # all resolve per agent and per tenant, so one shared client would serve
    # every scope the FIRST scope's endpoint. Nothing can be built before an
    # agent spec is in hand, which is why there is no default-scope client.
    mitra_clients = None
    mitra_sessions = None
    if settings.mitra_enabled:
        from app.integrations.mitra.connection import MitraClientRegistry
        from app.integrations.mitra.session_manager import MitraSessionManager
        mitra_clients = MitraClientRegistry()
        mitra_sessions = MitraSessionManager(settings)

    # Saathi: same shape as Mitra above, its own switch. The token provider is
    # built HERE rather than lazily, so a deployment whose credentials are wrong
    # fails at boot with a clear message instead of rendering a sidebar button
    # that errors on click. Under the "password" mechanism nothing is minted
    # yet -- the first request does that -- so a boot does not depend on ELEVATE
    # being reachable.
    saathi_tokens = None
    saathi_sessions = None
    if settings.saathi_enabled:
        from app.integrations.saathi.auth import build_token_provider
        from app.integrations.saathi.session_manager import SaathiSessionManager

        saathi_tokens = build_token_provider(settings)
        saathi_sessions = SaathiSessionManager(settings, saathi_tokens)
        logger.info(
            "saathi enabled",
            extra={"mechanism": saathi_tokens.mechanism, "origin": settings.saathi_origin_url},
        )

    # BOTH OR NEITHER: voice needs somewhere to put the recording AND something
    # to transcribe it, so a half-configured deployment must fail at boot rather
    # than answer upload-url and then fail at transcribe. Both None when
    # disabled, which the voice router turns into 503 VOICE_DISABLED.
    #
    # A missing ffmpeg only warns: it is a system binary `make install` cannot
    # guarantee, and refusing to boot over an unexercised feature flag is worse.
    bhashini = None
    object_store = None
    if settings.voice_enabled:
        from app.integrations.bhashini import BhashiniClient
        from app.integrations.bhashini.audio import ffmpeg_available
        from app.integrations.storage import get_object_store

        if not settings.bhashini_authorization:
            raise RuntimeError(
                "VOICE_ENABLED=1 but BHASHINI_AUTHORIZATION is not set. "
                "Set the Bhashini credentials in .env, or set VOICE_ENABLED=0."
            )
        if not ffmpeg_available():
            logger.warning(
                "VOICE_ENABLED=1 but ffmpeg is not on PATH. Speech-to-text will "
                "fail at request time. Install it: `brew install ffmpeg` or "
                "`apt-get install -y ffmpeg`."
            )

        # Raises StorageConfigError at BOOT on a bad provider or a missing
        # bucket -- a failed deploy rather than an outage mid-turn.
        object_store = get_object_store(settings)

        # Supported but never silent: recordings are user speech, and on a
        # public bucket anyone who guesses a key can listen to them.
        if object_store.bucket_type == "public":
            logger.warning(
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
        saathi_tokens=saathi_tokens,
        saathi_sessions=saathi_sessions,
    )
    handler_factory = HandlerFactory(deps)
    agent_registry = AgentRegistry(
        ttl_s=settings.registry_ttl_s,
        # Hides a disabled provider's agents. A runtime filter, not the status
        # write ConfigSyncService used to perform -- see AgentRegistry.
        mitra_enabled=bool(settings.mitra_enabled),
        saathi_enabled=bool(settings.saathi_enabled),
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
        saathi_tokens=saathi_tokens,
        saathi_sessions=saathi_sessions,
        bhashini=bhashini,
        object_store=object_store,
    )
