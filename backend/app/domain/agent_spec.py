"""The AgentSpec model -- what an agent IS, as validated configuration.

Responsible for: the discriminated union of agent types and their nested specs,
plus the canonical JSON + checksum used for versioning.
Used by: the admin route on write, the registry on load, handlers at runtime.

PURE: imports nothing outside app.domain, so it cannot reach Settings. That is
why `finalize_path` is a plain str rather than a Literal -- the endpoints are
configurable, and the admin route validates it instead.
"""
import json
import hashlib
import copy
from typing import Any, Literal, Optional, Union, List

from pydantic import BaseModel, Field, ConfigDict, model_validator, PrivateAttr

from app.domain.core import UserContext

#: What a finalised interview report is fetched as, unless a spec overrides it.
#: Declared here (rather than defaulted separately in RemoteSpec, in
#: MitraRestClient.get_report and in the sessions router) because the three have
#: to agree: the media type requested from Mitra is what the browser is then
#: told it is downloading.
DEFAULT_REPORT_MEDIA_TYPE = "application/pdf"


class ModelSpec(BaseModel):
    provider:    Literal["openrouter"] = "openrouter"
    name:        str
    temperature: float      = Field(0.0, ge=0.0, le=2.0)
    max_tokens:  Optional[int] = Field(None, ge=1, le=32768)
    timeout_s:   float      = Field(30.0, gt=0, le=300)

class RoutingSpec(BaseModel):
    keywords:             List[str] = Field(default_factory=list)
    priority:             int   = Field(50, ge=0, le=100)
    pin_session:          bool  = False
    exit_keywords:        List[str] = Field(default_factory=lambda: ["/exit", "cancel", "stop"])
    confidence_threshold: float = Field(0.5, ge=0.0, le=1.0)
    router_selectable:    bool  = True
    direct_selectable:    bool  = True

class MemorySpec(BaseModel):
    strategy:      Literal["recent", "none"] = "recent"
    history_turns: int  = Field(10, ge=0, le=100)
    include_other_agents: bool = True

class LimitsSpec(BaseModel):
    max_tool_iterations: int        = Field(3, ge=0, le=10)
    max_turns:           Optional[int] = None
    rate_limit_per_conversation_per_min: int = Field(20, ge=1)
    rate_limit_per_user_per_min:         int = Field(60, ge=1)

class RetrySpec(BaseModel):
    max_attempts:     int = Field(3, ge=1, le=5)
    backoff:          Literal["none", "linear", "exponential"] = "exponential"
    initial_delay_ms: int = Field(500, ge=0)
    retry_on: List[Literal["timeout","rate_limit","server_error","connection_error"]] = \
              Field(default_factory=lambda: ["timeout", "rate_limit", "server_error"])

class AccessSpec(BaseModel):
    tenant_codes:       List[str] = Field(default_factory=list)
    organization_codes: List[str] = Field(default_factory=list)
    required_roles:     List[str] = Field(default_factory=list)
    allow_anonymous:    bool = True

    def matches(self, user_context: Optional[UserContext]) -> bool:
        if user_context is None:
            return self.allow_anonymous
            
        if self.tenant_codes and user_context.tenant_code not in self.tenant_codes:
            return False
            
        if self.organization_codes:
            if not user_context.active_org_id:
                return False
            active_org_code = next((o.org_code for o in user_context.orgs if o.org_id == user_context.active_org_id), None)
            if active_org_code not in self.organization_codes:
                return False
                
        if self.required_roles:
            # required_roles matches against the ACTIVE org only. 
            # We enforce that the user has AT LEAST ONE of the required roles.
            # "Empty list == no restriction on that dimension. All three are ANDed."
            user_roles = set(user_context.roles)
            if not user_roles.intersection(set(self.required_roles)):
                return False

        return True

class FeaturesSpec(BaseModel):
    emit_options:           bool = False
    streaming:              bool = False
    record_tool_executions: bool = True

class BaseAgentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    
    schema_version: Literal[1] = 1
    key:         str = Field(pattern=r"^[a-z][a-z0-9_]{1,62}$")
    name:        str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1000)
    status:      Literal["enabled", "disabled"] = "enabled"
    default:     bool = False
    sort_order:  int  = 100
    capabilities: List[str] = Field(default_factory=list)
    routing:  RoutingSpec  = Field(default_factory=RoutingSpec)
    memory:   MemorySpec   = Field(default_factory=MemorySpec)
    limits:   LimitsSpec   = Field(default_factory=LimitsSpec)
    retry:    RetrySpec    = Field(default_factory=RetrySpec)
    access:   AccessSpec   = Field(default_factory=AccessSpec)
    features: FeaturesSpec = Field(default_factory=FeaturesSpec)

    #: The dict this spec was validated FROM, kept verbatim.
    #:
    #: `canonical_json` checksums this rather than `model_dump()`, and that is
    #: deliberate: model_dump emits every default the schema fills in, so adding
    #: an optional field with a default would change the checksum of every
    #: stored config -- invalidating every HandlerFactory cache entry and making
    #: a schema change look like a configuration change in the audit log.
    _source_dict: dict = PrivateAttr(default_factory=dict)

    @model_validator(mode='wrap')
    @classmethod
    def store_source(cls, v: Any, handler: Any) -> Any:
        """Capture the input dict before validation fills in defaults.

        This used to also expand ${VAR} references against os.environ, which is
        how the YAML seed carried per-deployment values. There is no YAML any
        more and no environment indirection: a config row holds literal values,
        and a deployment or a tenant differs by having its own row. The one
        exception is `RemoteSpec.origin_env`, which names a variable rather than
        holding a value, because the Origin header is a credential.
        """
        if isinstance(v, dict):
            source = copy.deepcopy(v)
            model = handler(v)
            model._source_dict = source
            return model
        return handler(v)

class LlmAgentSpec(BaseAgentSpec):
    agent_type: Literal["llm"]
    prompt: str = Field(min_length=1)
    tools:  List[str] = Field(default_factory=list)
    model:  ModelSpec

class MitraHandshakeSpec(BaseModel):
    settle_ms:  int = 1500
    ack_types:  List[str] = Field(default_factory=lambda: ["authenticated", "auth_success"])
    timeout_ms: int = 5000

class MitraTurnSpec(BaseModel):
    first_turn_timeout_ms: int = 60000
    turn_timeout_ms:       int = 45000
    idle_gap_ms:           int = 8000

#: Mitra's own endpoint paths and the browser UA it expects. Duplicated from
#: app.integrations.mitra.rest_client because the domain layer is import-pure
#: by contract (.importlinter) and cannot import it; a test asserts they agree.
DEFAULT_PROFILE_PATH          = "/api/profile/"
DEFAULT_GENERATE_SESSION_PATH = "/api/generate-session/"
DEFAULT_CHAT_PATH             = "/api/companychat/"
DEFAULT_GET_STORY_PATH        = "/api/get-story/"
DEFAULT_FINALIZE_V1_PATH      = "/api/end-story/"
DEFAULT_FINALIZE_V2_PATH      = "/api/end-story/v2/"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

class MitraPathsSpec(BaseModel):
    """Mitra's own endpoint paths -- a third-party API contract, not a
    preference. Override only when Mitra moves an endpoint.
    """
    profile:          str = DEFAULT_PROFILE_PATH
    generate_session: str = DEFAULT_GENERATE_SESSION_PATH
    chat:             str = DEFAULT_CHAT_PATH
    get_story:        str = DEFAULT_GET_STORY_PATH
    # The v1/v2 pair is load-bearing: they read the user token from different
    # places and pick different PDF renderers. See docs/agent-configuration.md.
    finalize_v1:      str = DEFAULT_FINALIZE_V1_PATH
    finalize_v2:      str = DEFAULT_FINALIZE_V2_PATH

class MitraConnectionSpec(BaseModel):
    """Which Mitra deployment this agent talks to, at this scope.

    This is the SOLE source for these values -- there is no MITRA_* environment
    floor behind it any more, so a tenant-scoped config row fully determines
    which Mitra deployment that agent reaches.

    THE ORIGIN URL IS ABSENT ON PURPOSE. Mitra gates admission on it, so it is
    a credential and must never be stored in a config row; a scope needing its
    own uses `RemoteSpec.origin_env` to name an environment variable instead.
    """
    # No default: there is no env fallback, and an empty host is not a failure
    # anyone can read off a stack trace.
    base_url:             str = Field(min_length=1)
    ws_url:               str = Field(min_length=1)
    user_agent:           str = DEFAULT_USER_AGENT
    # Extra hosts whose presigned report URLs may be fetched. An SSRF control,
    # intersected with MITRA_HOST_CEILING when the operator has set one.
    allowed_hosts:        List[str] = Field(default_factory=list)
    paths:                MitraPathsSpec = Field(default_factory=MitraPathsSpec)
    connect_timeout_s:    float = Field(10.0, gt=0, le=300)
    read_timeout_s:       float = Field(30.0, gt=0, le=300)
    ws_connect_timeout_s: float = Field(10.0, gt=0, le=300)
    # Address fields sent in the authenticate frame. Mitra does not validate
    # them against anything real for the guest flows this integration uses.
    ip_city:              str = ""
    ip_state:             str = ""
    ip_zip:               str = ""

class RemoteSpec(BaseModel):
    """One remote_flow agent's Mitra binding.

    WHY THESE ARE FIELDS AND NOT ENVIRONMENT VARIABLES
    ==================================================
    `company` / `bot_route` were once read from os.environ (via `company_env` /
    `bot_route_env`), and the whole connection block was once a set of MITRA_*
    settings. Both made the binding process-global: one Saarthi process could
    serve exactly one Mitra company, on one Mitra deployment, with one set of
    bot routes. Since Mitra identifies a profile by (email, company), that is
    the difference between every tenant sharing one Mitra profile and each
    having its own.

    As plain spec fields they live in `agent_configs`, which is scoped
    (tenant_id, organization_id) -- so a scoped row carries its own values and
    two tenants get genuinely separate Mitra profiles and story histories.

    `origin_env` is the ONE remaining environment indirection, and only because
    the Origin header is a credential. See docs/agent-configuration.md.
    """
    # Which adapter serves this agent. Saathi runs the SAME Django application as
    # Mitra (project `shikshalokam_mohini`, the same ws/common/ consumer and the
    # same REST surface) but authenticates per-user and produces no story, which
    # is why it is a separate provider rather than another flow_name.
    provider:  Literal["mitra", "saathi"]
    # Not a Literal any more: the set of flows is Mitra-side configuration
    # (`/api/flow-connection-info/`), so enumerating it here meant a new flow
    # could not be configured without a code change. The provider validates it.
    flow_name: str = Field(min_length=1)
    # min_length=1, not Optional: an empty value does not fail loudly at Mitra,
    # it silently resolves the wrong CompanyBot or splits a user's profile.
    bot_route: str = Field(min_length=1)
    company:   str = Field(min_length=1)
    # Names the env var holding this scope's Origin credential, never the value.
    # Unset means the deployment-wide MITRA_ORIGIN_URL.
    origin_env: Optional[str] = None
    # Required: this is the only source for the Mitra endpoint, with no
    # environment floor behind it.
    connection: MitraConnectionSpec
    default_language:    Literal["en","hi","kn","te"] = "en"
    supported_languages: List[str] = Field(default_factory=lambda: ["en","hi","kn","te"])
    handshake: MitraHandshakeSpec = Field(default_factory=MitraHandshakeSpec)
    turn:      MitraTurnSpec      = Field(default_factory=MitraTurnSpec)
    completion_poll_every_turn: bool = True

    # Whether this flow ends by producing a story/report at all.
    #
    # FALSE IS NOT A DEGRADED MODE, it is a different kind of flow. Mitra's
    # interviews finalise into a PDF; Saathi's assistant is a conversation and
    # its own `/api/flow-connection-info/` reports `create_story: "none"`.
    # Calling finalize() on such a flow raises (no story id in the response) and
    # leaves the session `failed`, so this flag is what keeps a terminal turn
    # from being reported as an error. See app/services/turn_finalization.py.
    produces_artifact: bool = True

    # v1 vs v2 is a behavioural choice, not a version preference; a value
    # matching neither resolved endpoint is rejected at config-write time.
    # Optional because a provider with produces_artifact=False never finalises
    # and so has no endpoint to name.
    finalize_path:     Optional[str] = DEFAULT_FINALIZE_V2_PATH
    # Finalize without a user token. MUST match what the WebSocket authenticated
    # as -- a mismatch yields a valid but BLANK PDF, silently.
    finalize_as_guest: bool = False
    # NOT report_path: get_report hardcodes its endpoint and never read one.
    report_media_type: str = DEFAULT_REPORT_MEDIA_TYPE

class RemoteFlowAgentSpec(BaseAgentSpec):
    agent_type: Literal["remote_flow"]
    remote: RemoteSpec


class SaathiFlowAgentSpec(BaseAgentSpec):
    """A Saathi-backed agent.

    A SEPARATE agent_type rather than another `remote_flow` provider, because
    the two are gated independently: `SAATHI_ENABLED` and `MITRA_ENABLED` are
    different deployment switches, and the registry hides an agent by its type.
    Folding them together would mean enabling Mitra to get Saathi.

    The `remote` block is the same model -- Saathi runs the same Django
    application -- with `provider: "saathi"` and `produces_artifact: False`.
    """
    agent_type: Literal["saathi_flow"]
    remote: RemoteSpec

# Use Annotated and Union for the discriminated union
from typing_extensions import Annotated
AgentSpec = Annotated[
    Union[LlmAgentSpec, RemoteFlowAgentSpec, SaathiFlowAgentSpec],
    Field(discriminator="agent_type"),
]

def canonical_json(spec: Any) -> tuple[str, str]:
    """Deterministic JSON for a spec, plus its SHA256.

    The checksum is what separates one tenant's cached handler from another's
    (HandlerFactory keys on `(spec.key, checksum)`), so it must be a function of
    the CONTENT that was written, not of the schema that read it.

    That is why the source dict is preferred over `model_dump()`: model_dump
    emits every default the schema supplies, so adding one optional field would
    change the checksum of every stored config at once. Migration 0007 relies on
    this too -- it computes the same digest with the standard library, and
    tests/integration/test_seed_migration.py asserts the two agree.
    """
    raw_dict = getattr(spec, "_source_dict", None) or spec.model_dump(mode="json")

    json_str = json.dumps(raw_dict, separators=(',', ':'), sort_keys=True)
    sha = hashlib.sha256(json_str.encode('utf-8')).hexdigest()
    return json_str, sha
