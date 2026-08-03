import sys
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import ValidationError, field_validator

# The backend package root (.../backend). The env file is resolved against
# THIS, not the CWD -- so `python -m app.main` finds the same configuration
# whatever directory it was launched from.
BASE_DIR = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Populate os.environ from .env, explicitly and first.
#
# Settings itself does NOT need this -- pydantic-settings reads the file
# directly. ONE thing still does: `RemoteSpec.origin_env`, which names the
# variable holding a scope's Mitra Origin credential and is resolved with
# os.getenv (app/integrations/mitra/connection.py). It is the only remaining
# environment indirection in agent configuration, and it exists because the
# Origin header is a credential that must not be stored in a config row.
#
# The agent YAML's `${VAR}` substitution and the `bot_route_env` / `company_env`
# fields used to depend on this too. Both are gone -- agent config lives in the
# database and carries literal values.
#
# Under Flask this worked by accident twice over -- `flask run` loads .env, and
# so does litellm on import. Neither applies under uvicorn as a guarantee, and
# depending on a transitive import side effect is not worth keeping.
#
# PRECEDENCE (highest first):
#
#   1. real environment variables   (container / CI injection, one-off overrides)
#   2. backend/.env                 (the single config file)
#   3. the field defaults in this file
#
# override=False, so a real environment variable still beats the file -- which
# is what makes `PORT=9000 make run` and container-injected config work without
# editing anything.
# ---------------------------------------------------------------------------
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
    # A LABEL, not a selector -- it chooses no file. It exists so a running
    # process can say in its logs which deployment it believes it is, which is
    # the first thing you check when a config looks wrong.
    app_env: Literal["development", "qa", "production"] = "development"

    # ---- server ----
    # Where uvicorn binds. 0.0.0.0 in a container, 127.0.0.1 locally.
    host: str = "127.0.0.1"
    port: int = 8000
    # MUST stay 1 while MITRA_ENABLED=1: MitraSessionManager pools live
    # WebSockets in process memory, so a second worker opens a second Mitra
    # channel for the same interview. app/core/runtime.assert_single_worker
    # enforces this at startup rather than trusting the config.
    workers: int = 1
    # Mounted in FRONT of every route, e.g. "/saarathi-service" makes the chat
    # endpoint POST /saarathi-service/api/chat. Empty (the default) keeps the
    # bare /api/... paths. Normalised by the validator below, so
    # "saarathi-service/", "/saarathi-service" and "/saarathi-service/" are
    # all the same thing.
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
    # How long the in-process agent snapshot may be stale before the next
    # /api/ request re-checks MAX(updated_at). This is also the upper bound on
    # how long a config change takes to go live, since there is no restart and
    # no file to redeploy.
    #
    # CONFIG_SYNC_MODE is gone. There is no YAML to reconcile against: the
    # catalogue is seeded by migration 0007 and edited through the config API,
    # so there is nothing for the database to drift from.
    registry_ttl_s: int = 30

    # ---- web tier (new in the FastAPI port) ----
    # Comma-separated browser origins allowed to call this API. The React dev
    # server is a separate origin now, so CORS is mandatory where it was not
    # under Flask (which served the SPA itself).
    frontend_origins: str = "http://localhost:5173"
    # Size of Starlette's anyio worker threadpool. Every endpoint is a plain
    # `def`, so one request == one worker thread == one DB connection held for
    # the whole turn. anyio's default is 40, which exceeds the 24-connection
    # pool and turns overload into QueuePool timeouts surfaced as 500s.
    # None => fall back to db_pool_size. MUST stay <= db_pool_size.
    threadpool_size: Optional[int] = None

    # ---- feature flags ----
    # The ONLY auth switch. True: decode SAARTHI_STATIC_TOKEN and derive the
    # identity from its claims. False: skip tokens entirely and serve the
    # Authenticator's hardcoded identity. Defaults to True so a missing flag
    # fails safe (auth on), never silently open.
    auth_check: bool = True
    mitra_enabled: int = 0
    saarthi_admin_enabled: int = 0

    # ---- auth / jwt ----
    # Saarthi is the sole validator: this app decodes the token, it never
    # verifies the signature or the expiry. There is deliberately no secret
    # setting -- see app/services/identity.py.
    saarthi_static_token: Optional[str] = None
    jwt_identifier_field: str = "id"
    jwt_email_suffix: str = "@shikshalokam.org"

    # ---- mitra REST ----
    # MITRA_ORIGIN_URL is a credential: Mitra gates admission on the Origin
    # header. Never log it, never include it in error responses, never put
    # it in YAML. See design doc §13.2 and the comment in MitraRestClient.
    mitra_base_url: str = "https://mitra.example.com"
    mitra_origin_url: str = "https://mitra.example.com"
    mitra_user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    # Comma-separated EXTRA hostnames allowed in returned URLs (the
    # mitra_base_url hostname is always included automatically).
    mitra_allowed_hosts: str = ""
    # The operator's BACKSTOP on the above, once an agent config can override
    # it. `mitra_allowed_hosts` is an SSRF control (MitraRestClient._validate_url),
    # so a config write that widens it must not be able to widen it without
    # bound; a spec-supplied allowlist is intersected with this.
    #
    # EMPTY MEANS NO CEILING, deliberately -- every existing deployment keeps
    # today's behaviour until it opts in. Set it to the full set of hosts any
    # tenant may ever be pointed at.
    mitra_host_ceiling: str = ""
    mitra_connect_timeout_s: float = 10.0
    mitra_read_timeout_s: float = 30.0

    # ---- mitra REST paths ----
    # Mitra's OWN endpoint paths, appended to mitra_base_url. These are a
    # third-party API contract, not a preference: change them only when Mitra
    # moves an endpoint. The v1/v2 finalize pair is load-bearing -- the two
    # endpoints read the user token from different places (v2 from the
    # Authorization header, v1 from the request body) and select different PDF
    # renderers, so MitraRestClient decides token placement by comparing the
    # per-agent finalize_path against mitra_finalize_v2_path. Renaming one of
    # the pair without the other silently unauthenticates every finalize.
    mitra_profile_path: str = "/api/profile/"
    mitra_generate_session_path: str = "/api/generate-session/"
    mitra_chat_path: str = "/api/companychat/"
    mitra_get_story_path: str = "/api/get-story/"
    mitra_finalize_v1_path: str = "/api/end-story/"
    mitra_finalize_v2_path: str = "/api/end-story/v2/"

    # ---- mitra WebSocket (MitraChannel) ----
    mitra_ws_url: str = "wss://mitra.example.com/ws/common/"
    mitra_ws_connect_timeout_s: float = 10.0
    # Address fields sent in the authenticate frame. Mitra doesn't validate
    # these against anything real for the guest flows this integration uses.
    mitra_ip_city: str = ""
    mitra_ip_state: str = ""
    mitra_ip_zip: str = ""

    # ---- mitra channel pool (MitraSessionManager) ----
    # Each open channel is a socket plus a thread -- bound the LRU. 200 is
    # fine; 5,000 is not (design doc §7.6/§13.1).
    mitra_max_open_channels: int = 200
    mitra_idle_close_s: float = 1200.0

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
    print(f"Configuration validation error:\n{e}")
    sys.exit(1)
