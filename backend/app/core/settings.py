"""Application settings.

Responsible for: every configurable value, and the one .env file behind them.
Used by: build_container and anything that needs a tunable; imported once, at
startup, where a validation error exits the process.

PRECEDENCE (highest first): real environment variables, then backend/.env, then
the field defaults here.
"""
import sys
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import ValidationError, field_validator

# The backend package root. The env file resolves against THIS, not the CWD, so
# `python -m app.main` finds the same config whatever directory launched it.
BASE_DIR = Path(__file__).resolve().parents[2]

# Settings does not need this -- pydantic-settings reads the file itself. One
# thing still does: RemoteSpec.origin_env names the variable holding a scope's
# Mitra Origin credential and is resolved with os.getenv. That indirection
# exists because the Origin header is a credential that must not sit in a
# config row. override=False keeps real environment variables winning.
load_dotenv(BASE_DIR / ".env", override=False)

class Settings(BaseSettings):
    # ONE env file. Deployments differ by the values in it (or by real
    # environment variables), not by which file is loaded.
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- environment ----
    # A LABEL, not a selector -- it chooses no file. Echoed in the startup log
    # so a running process can say which deployment it believes it is.
    app_env: Literal["development", "qa", "production"] = "development"

    # ---- server ----
    # Where uvicorn binds. 0.0.0.0 in a container, 127.0.0.1 locally.
    host: str = "127.0.0.1"
    port: int = 8000
    # MUST stay 1 while MITRA_ENABLED=1 -- MitraSessionManager pools live
    # WebSockets in process memory. assert_single_worker enforces it at startup.
    workers: int = 1
    # Mounted in FRONT of every route: "/saarathi-service" makes the chat
    # endpoint POST /saarathi-service/api/chat. Normalised by the validator below.
    api_prefix: str = ""

    # ---- core ----
    OPENROUTER_API_KEY: str
    OPENROUTER_MODEL: str = "qwen/qwen3.7-flash"
    LLM_TIMEOUT: float = 30.0
    LLM_MAX_RETRIES: int = 3
    LOG_LEVEL: str = "INFO"

    # ---- database ----
    database_url: Optional[str] = None
    db_pool_size: int = 16
    db_max_overflow: int = 8

    # ---- registry ----
    # How stale the in-process agent snapshot may be, and so the upper bound on
    # how long a config change takes to go live. CONFIG_SYNC_MODE is gone.
    registry_ttl_s: int = 30

    # ---- web tier ----
    # Comma-separated browser origins allowed to call this API. The SPA is a
    # separate origin, so one that is missing here is blocked by CORS.
    frontend_origins: str = "http://localhost:5173"
    # Starlette's anyio threadpool. One request == one thread == one DB
    # connection, so this MUST stay <= db_pool_size. None => db_pool_size.
    threadpool_size: Optional[int] = None

    # ---- feature flags ----
    # The ONLY auth switch. True decodes SAARTHI_STATIC_TOKEN for the identity;
    # False serves the hardcoded one. Defaults True so a missing flag fails safe.
    auth_check: bool = True
    # 0 disables every remote_flow agent. Read at container build time, before
    # any agent config is loaded, which is why it cannot live in one.
    mitra_enabled: int = 0
    # 0 => the whole /api/agents admin surface 404s.
    saarthi_admin_enabled: int = 0

    # ---- auth / jwt ----
    # Saarthi is the sole validator: this app decodes the token and never
    # verifies signature or expiry, so there is deliberately no secret setting.
    saarthi_static_token: Optional[str] = None
    # MUST match Mitra's SSO derivation, email = data[field] + suffix. Wrong
    # values create a SECOND Mitra profile and split a user's stories.
    jwt_identifier_field: str = "id"
    jwt_email_suffix: str = "@shikshalokam.org"

    # ---- mitra ----
    # ONLY FOUR KEYS LIVE HERE. Which Mitra deployment an agent reaches (base
    # URL, timeouts, endpoint paths) is per agent and per tenant, so it lives in
    # `remote.connection` on the agent config row -- not here. Note extra=ignore
    # above: a stale MITRA_BASE_URL in a .env is accepted and does nothing.

    # A CREDENTIAL -- Mitra gates admission on the Origin header. Never log it,
    # never store it in a config row, never echo it in an error response.
    mitra_origin_url: str = "https://mitra.example.com"
    # SSRF backstop: a config-supplied allowed_hosts is intersected with this.
    # Empty means no ceiling. See docs/agent-configuration.md.
    mitra_host_ceiling: str = ""
    # Bounds on ONE process-wide channel pool shared by every agent, so these
    # cannot be per-agent. Each open channel is a socket plus a thread.
    mitra_max_open_channels: int = 200
    mitra_idle_close_s: float = 1200.0

    # ---- saathi ----
    # Saathi runs the same Django application as Mitra, so its endpoint and
    # timeouts live in `remote.connection` on the agent config exactly as
    # Mitra's do. What lives here is only what cannot: credentials, and the
    # deployment-wide enable switch.
    #
    # 0 disables every saathi_flow agent, the same shape mitra_enabled uses.
    saathi_enabled: int = 0
    # A CREDENTIAL -- Saathi gates the WebSocket on Origin via Django Channels'
    # AllowedHostsOriginValidator, exactly as Mitra does.
    saathi_origin_url: str = "https://qa.saathi.shikshalokam.org"

    # HOW THE ACCESS TOKEN IS OBTAINED. Unlike Mitra, Saathi authenticates
    # per-user: the WebSocket carries a real ELEVATE JWT and the server derives
    # the profile from it, so there is always a token to source from somewhere.
    #
    #   "password" -- log in with saathi_email/saathi_password and re-mint on a
    #                 401. The only option that survives a session ending.
    #   "token"    -- use saathi_access_token verbatim. Simple, but cannot
    #                 recover: ELEVATE tracks the JWT's session_id server-side
    #                 and can invalidate it long before the token's own `exp`.
    saathi_login_mechanism: Literal["password", "token"] = "password"
    # A CREDENTIAL. Read when saathi_login_mechanism="token".
    saathi_access_token: Optional[str] = None
    # CREDENTIALS. Read when saathi_login_mechanism="password".
    saathi_email: Optional[str] = None
    saathi_password: Optional[str] = None
    # ELEVATE resolves the tenant from this header; without it login answers
    # 406 "Tenant domain not found" rather than a credential error.
    saathi_tenant_code: str = "saathi"
    # The ELEVATE identity service login/read lives here, NOT on the Saathi
    # host. Only the "password" mechanism needs it.
    elevate_base_url: str = "https://qa.elevate-apis.shikshalokam.org"

    # ---- cloud storage (provider-agnostic) ----
    # Switching provider is a .env change, not a code change. Names follow the
    # ELEVATE convention shared with the Node services, so ACCOUNTNAME/SECRET
    # mean different things per provider:
    #   aws / s3 / oci -> access key id / secret access key
    #   gcp            -> service-account email / the full service-account JSON
    #   local          -> both unused
    cloud_storage_provider: str = "local"
    # The ELEVATE Node services also accept a bare CLOUD_STORAGE key and some of
    # their .env files set only that. Read as a fallback by the factory.
    cloud_storage: Optional[str] = None
    cloud_storage_accountname: Optional[str] = None
    # A CREDENTIAL. Never log it, never echo it in an error response. For gcp
    # this is an entire service-account JSON document, which is why it is read
    # as a string and parsed by the driver rather than pointing at a file.
    cloud_storage_secret: Optional[str] = None
    # aws / oci: the region. GCP: THE PROJECT ID -- the convention has no
    # project slot, so this carries it, the same one-name-two-meanings trade
    # ACCOUNTNAME and SECRET already make. GCS takes its actual location from
    # the bucket. See app/integrations/storage/gcp.py::_credentials_info.
    cloud_storage_region: Optional[str] = None
    # s3 / oci only. Set it and the aws driver talks to OCI Object Storage,
    # MinIO, or any other S3-compatible endpoint -- which is most of the reason
    # a separate driver per vendor is not needed.
    cloud_endpoint: Optional[str] = None
    cloud_storage_bucketname: Optional[str] = None
    # Carried for convention-compatibility with the Node services. Voice
    # recordings are private and short-lived, so nothing here reads it.
    public_asset_bucketname: Optional[str] = None
    # "private" | "public". Changes how objects are READ and nothing else:
    # private issues signed, expiring URLs; public returns the stable unsigned
    # address. Uploads stay signed on both -- a bucket being publicly readable
    # is never a reason to accept anonymous writes.
    cloud_storage_bucket_type: str = "private"
    # Where LocalObjectStore keeps its files. Relative paths resolve against
    # BASE_DIR, not the CWD, for the same reason .env does.
    local_storage_dir: str = "var/storage"

    # ---- voice (bhashini) ----
    #
    # Speech-to-text and text-to-speech via Bhashini (AI4Bharat / ULCA Dhruva),
    # the same provider Mitra uses. Secrets plus a third-party endpoint, so per
    # docs/configuration.md they stay in .env: "env holds what the app needs
    # before it can read a database, plus the secrets."
    #
    # 0 => the whole /api/voice surface answers 503 VOICE_DISABLED and the
    # container builds neither the Bhashini client nor the object store. Read at
    # container build time, exactly like mitra_enabled.
    voice_enabled: int = 0
    # Three separate credentials, and Dhruva wants different subsets per task:
    # ASR sends all three, TTS and translation send Authorization alone.
    bhashini_api_key: Optional[str] = None
    bhashini_user_id: Optional[str] = None
    bhashini_authorization: Optional[str] = None
    bhashini_pipeline_id: Optional[str] = None
    bhashini_base_url: str = (
        "https://dhruva-api.bhashini.gov.in/services/inference/pipeline"
    )
    bhashini_connect_timeout: float = 10.0
    bhashini_read_timeout: float = 30.0
    # ~10 minutes of Opus. Checked against object METADATA before the body is
    # buffered, so an oversized upload costs a HEAD, not a download.
    voice_max_audio_bytes: int = 10_485_760
    # Seconds of audio per ASR request. Dhruva degrades on long clips, so a
    # recording is split and the chunks are transcribed in parallel.
    voice_chunk_duration_s: int = 10
    # Bhashini's per-request text ceiling, in UTF-8 BYTES not characters --
    # Indic scripts run 3 bytes/char, so this is ~1600 Devanagari characters.
    voice_tts_byte_limit: int = 4800
    # Bounds the ASR fan-out. Mitra uses an unbounded ThreadPoolExecutor, which
    # fires ~30 concurrent calls at Dhruva for a 5-minute recording.
    voice_asr_max_workers: int = 4
    # How long ffmpeg may take to transcode one recording to WAV. A wedged
    # subprocess otherwise holds a request thread -- and a thread is also a DB
    # connection for the life of the turn (THREADPOOL_SIZE <= DB_POOL_SIZE).
    voice_ffmpeg_timeout_s: float = 30.0
    # Lifetime of a presigned upload URL. Long enough for a slow mobile upload,
    # short enough that a leaked URL stops being a write primitive quickly.
    voice_upload_url_expiry_s: int = 300

    # Ceiling on GET /api/conversations?limit=. Caps how much history one
    # request can pull, independently of what a client asks for.
    conversations_page_limit_max: int = 20

    # ---- object storage tuning --------------------------------------------
    # boto3's retry policy. "standard" retries throttling and transient 5xx
    # with exponential backoff; the attempt count is the total, not additional.
    cloud_storage_max_attempts: int = 3
    cloud_storage_retry_mode: str = "standard"

    @field_validator("cloud_storage_bucket_type")
    @classmethod
    def _normalise_bucket_type(cls, v: str) -> str:
        """Accept only "private" or "public", case-insensitively.

        Rejected rather than defaulted, because the two differ in whether
        recordings end up world-readable. A typo silently falling back to
        "private" would be the safe direction, but a typo falling back the
        other way would not -- and a validator that only catches one direction
        is worse than one that catches both.
        """
        normalised = (v or "").strip().lower()
        if normalised not in ("private", "public"):
            raise ValueError(
                f"CLOUD_STORAGE_BUCKET_TYPE must be 'private' or 'public', got {v!r}"
            )
        return normalised

    @field_validator("api_prefix")
    @classmethod
    def _normalise_api_prefix(cls, v: str) -> str:
        """Accept any spelling, store exactly one: "" or "/segment".

        Routes are mounted as f"{api_prefix}/api/...", so a missing leading
        slash or a stray trailing one would produce "saarathi-service/api/chat"
        or "//api/chat" -- neither of which FastAPI matches the way anyone
        expects.
        """
        v = v.strip().strip("/")
        return f"/{v}" if v else ""

try:
    settings = Settings()
except ValidationError as e:
    # DELIBERATELY stderr and not app.core.logger. Logging is configured from
    # Settings (LOG_LEVEL), so at this point there is no configured logger to
    # write to -- reaching for one here would either emit nothing or raise a
    # second error on top of the one the operator actually needs to read.
    # Do not "fix" this to logger.error().
    print(f"Configuration validation error:\n{e}", file=sys.stderr)
    sys.exit(1)
