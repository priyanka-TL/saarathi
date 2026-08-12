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
from app.providers.registry import stateful_enabled_names
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
    # ONE SLOT FOR EVERY REMOTE PROVIDER. This was four named vendor slots
    # (clients and channel pools, twice), which meant onboarding a platform
    # edited the composition root, the DI struct and every construction site.
    providers: Any = None                  # app.providers.registry.ProviderRegistry
    bhashini: Optional[Any] = None         # BhashiniClient | None (voice_enabled gated)
    object_store: Optional[Any] = None     # ObjectStore | None (voice_enabled gated)
    elevate: Optional[Any] = None          # ElevateUserClient | None (ELEVATE_BASE_URL gated)


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

    # A REGISTRY, not a client: base URL, timeouts, endpoint paths and the
    # credentials all resolve per agent and per tenant, so one shared client
    # would serve every scope the FIRST scope's endpoint. Nothing can be built
    # before an agent spec is in hand, which is why there is no default-scope
    # client and no per-provider block here.
    #
    # ONE LINE, AND IT DOES NOT GROW. Providers self-register from their own
    # packages and PROVIDERS_ENABLED decides which of them this deployment
    # hands out, so onboarding a platform never edits the composition root.
    from app.providers.registry import ProviderRegistry

    providers = ProviderRegistry(settings)
    logger.info(
        "remote providers ready",
        extra={
            "enabled": sorted(providers.enabled) or None,
            "stateful": stateful_enabled_names(settings) or None,
        },
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

    # UNSET IS NOT AN ERROR, unlike the voice block above. Voice fails the boot
    # when half-configured because answering upload-url and then failing at
    # transcribe is worse than not starting; profile has no such pairing -- a
    # deployment with no ELEVATE_BASE_URL simply has no Profile section, and the
    # router turns None into 503 PROFILE_UNAVAILABLE. Raising here would take
    # down every existing deployment on upgrade.
    elevate = None
    if settings.elevate_base_url:
        from app.integrations.elevate import ElevateUserClient

        elevate = ElevateUserClient(
            base_url=settings.elevate_base_url,
            connect_timeout=settings.elevate_connect_timeout,
            read_timeout=settings.elevate_read_timeout,
        )
    else:
        logger.info(
            "ELEVATE_BASE_URL is not set: /api/profile will answer 503 "
            "PROFILE_UNAVAILABLE and the Profile section stays hidden."
        )

    deps = HandlerDeps(
        llm_factory=llm_factory,
        tool_registry=tool_registry,
        providers=providers,
        settings=settings,
    )
    handler_factory = HandlerFactory(deps)
    agent_registry = AgentRegistry(
        ttl_s=settings.registry_ttl_s,
        # Hides a disabled provider's agents. A runtime filter, not the status
        # write ConfigSyncService used to perform -- see AgentRegistry.
        enabled_providers=providers.enabled,
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
        providers=providers,
        bhashini=bhashini,
        object_store=object_store,
        elevate=elevate,
    )
