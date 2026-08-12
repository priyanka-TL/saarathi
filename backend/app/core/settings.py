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
# thing still does: a remote provider's credentials are named by its config row
# (`remote.auth.credential_env` and friends) and resolved with os.getenv, so
# every variable a row can name has to be in the process environment whether or
# not it is a Settings field. That indirection exists because a config row is
# readable through the admin API and must never hold a credential.
# override=False keeps real environment variables winning.
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
    # MUST stay 1 while any enabled provider declares stateful_transport --
    # those pool live WebSockets in process memory. assert_single_worker
    # enforces it at startup, reading the providers rather than a named flag.
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
    # The ONLY auth switch. True requires a verified per-request bearer token
    # on every call (see app/services/identity.py); False serves the
    # hardcoded dev identity instead, regardless of any token sent -- NEVER
    # in prod. Defaults True so a missing flag fails safe.
    auth_check: bool = True
    # 0 => the whole /api/agents admin surface 404s.
    saarthi_admin_enabled: int = 0

    # ---- remote providers ----
    #
    # ONE KEY FOR EVERY PROVIDER, replacing the per-platform MITRA_ENABLED /
    # SAATHI_ENABLED pair. Comma-separated registry keys:
    #
    #     PROVIDERS_ENABLED=mitra,saathi
    #
    # An agent whose `remote.provider` is not in this list is hidden from
    # /api/agents and from the sidebar -- a runtime filter, never a DB write: a
    # deployment switch has no business mutating tenant configuration.
    #
    # WHY THIS IS STILL ENV, when the rest of a provider's configuration moved
    # into agent_configs. It is read at container-build time, before any agent
    # config is loadable, and it decides whether the database-reading client is
    # built at all. A database write must not be able to switch a provider on.
    #
    # Empty (the default) means no remote provider is enabled, so a deployment
    # that has not opted in cannot reach an external platform by accident.
    providers_enabled: str = ""

    # SSRF backstop: a config-supplied allowed_hosts is intersected with this.
    # Empty means no ceiling. An operator backstop ON configuration, so it stays
    # env -- a control that the thing it constrains can widen is not a control.
    provider_host_ceiling: str = ""

    # Bounds on ONE channel pool per provider, shared by every agent using it,
    # so these cannot be per-agent. Each open channel is a socket plus a thread.
    provider_max_open_channels: int = 200
    provider_idle_close_s: float = 1200.0

    # ---- auth / jwt ----
    # ELEVATE's own ACCESS_TOKEN_SECRET. Required to trust ANY per-request
    # bearer token (the login flow's output) -- it comes from the request
    # itself, so it must be signature- and expiry-verified or any caller
    # could forge an identity/tenant/role. Without this set, a per-request
    # token is refused rather than trusted unverified -- and since AUTH_CHECK
    # no longer has a no-token fallback (removed: it was an authentication
    # bypass -- see app/services/identity.py), an unset secret means nothing
    # can authenticate at all while AUTH_CHECK=true.
    elevate_jwt_secret: Optional[str] = None
    # MUST match Mitra's SSO derivation, email = data[field] + suffix. Wrong
    # values create a SECOND Mitra profile and split a user's stories.
    jwt_identifier_field: str = "id"
    jwt_email_suffix: str = "@shikshalokam.org"

    # ELEVATE's user service, for reading and updating the caller's PROFILE
    # (/api/profile). Distinct from elevate_jwt_secret above, which only
    # verifies a token ELEVATE already minted: this is an outbound call, made
    # with the caller's OWN token, so no new credential is introduced.
    #
    # THIS NAME IS BACK, AND NOT BY MISTAKE. `.env.example` once retired
    # ELEVATE_BASE_URL into `remote.auth.token_endpoint` (migration 0013) --
    # that was Saathi's per-agent, per-tenant login endpoint, which genuinely
    # is not configuration of this process. This is a different thing wearing
    # the same name: the first-party user service every tenant shares.
    #
    # None is the only safe default -- there is no sensible fallback host for
    # a user-data write. Unset degrades to 503 at request time rather than
    # failing the boot, so every existing deployment keeps working untouched.
    #
    # There is deliberately NO `profile_enabled` flag beside it. Whether the
    # completion popup appears is a frontend decision
    # (APPLICATION_PROFILE_POPUP_ENABLED); a second switch here would let the
    # two disagree.
    elevate_base_url: Optional[str] = None
    # Split, not one `timeout=30` as the Django original uses. A request holds
    # a worker thread and therefore a DB connection for its whole life
    # (THREADPOOL_SIZE <= DB_POOL_SIZE), so a dead DNS must not park one for
    # the full read budget. Same reasoning as bhashini_*_timeout below.
    elevate_connect_timeout: float = 10.0
    elevate_read_timeout: float = 30.0

    # ---- provider credentials ----
    #
    # THERE ARE NO FIELDS HERE, DELIBERATELY, AND THAT IS THE WHOLE DESIGN.
    #
    # A provider's credentials still live in .env -- a config row is readable
    # through the admin API, so it must never hold one -- but they are reached
    # by NAME, not by field. `remote.auth` on the agent config names the
    # variable (`credential_env`) and app/providers/connection.py resolves it
    # with os.getenv against the environment load_dotenv populated above.
    #
    # The variables a stock deployment sets are documented in .env.example:
    #
    #     MITRA_ORIGIN_URL     the Origin header Mitra gates admission on
    #     SAATHI_ORIGIN_URL    the same, for Saathi
    #
    # Saathi used to also name SAATHI_EMAIL/SAATHI_PASSWORD/SAATHI_ACCESS_TOKEN
    # here, to mint its own connection-level ELEVATE credential. That mechanism
    # (app/providers/saathi/auth.py) is gone: Saathi now authenticates every
    # call with the CALLING UserContext's own `.token`, from that user's real
    # ELEVATE login -- see ELEVATE_JWT_SECRET above, and
    # app/providers/saathi/provider.py.
    #
    # Adding a platform adds variables to .env and names them from its config
    # row. It does NOT add fields here, which is what stopped every new platform
    # from being a Settings change.
    #
    # Everything that is NOT a credential -- base URL, stream URL, timeouts,
    # endpoint paths, the company, the bot route -- moved into the agent config
    # row, where it is per tenant instead of per process. Note extra="ignore"
    # above: a stale MITRA_BASE_URL or SAATHI_EMAIL left in a .env is accepted
    # and does nothing.

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
    # container build time, for the same reason PROVIDERS_ENABLED is.
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
