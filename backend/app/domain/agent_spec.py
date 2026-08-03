import json
import hashlib
import copy
from typing import Any, Literal, Optional, Union, List

from pydantic import BaseModel, Field, ConfigDict, model_validator, PrivateAttr

from app.domain.core import UserContext

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

class MitraPathsSpec(BaseModel):
    """Per-scope overrides for Mitra's own endpoint paths.

    EVERY FIELD IS OPTIONAL AND None MEANS "use the env value". That is what
    keeps this additive: a deployment with no overrides resolves to exactly the
    MITRA_*_PATH settings it did before these fields existed.
    """
    profile:          Optional[str] = None
    generate_session: Optional[str] = None
    chat:             Optional[str] = None
    get_story:        Optional[str] = None
    finalize_v1:      Optional[str] = None
    finalize_v2:      Optional[str] = None

class MitraConnectionSpec(BaseModel):
    """Which Mitra deployment this agent talks to, at this scope.

    Set nothing and the agent uses the deployment's own MITRA_* settings, which
    is the case for every agent until a tenant is deliberately pointed
    elsewhere. Merged onto the env floor by
    app/integrations/mitra/connection.resolve_connection -- this module cannot
    do the merging itself, because the domain layer is import-pure by contract
    and cannot reach Settings.

    THE ORIGIN URL IS ABSENT ON PURPOSE. Mitra gates admission on it, so it is
    a credential and must never be stored in a config row; a scope needing its
    own uses `RemoteSpec.origin_env` to name an environment variable instead.
    """
    base_url:             Optional[str] = None
    ws_url:               Optional[str] = None
    user_agent:           Optional[str] = None
    # Extra hosts whose presigned report URLs may be fetched. An SSRF control
    # (MitraRestClient._validate_url), so it is intersected with
    # MITRA_HOST_CEILING when the operator has set one.
    allowed_hosts:        Optional[List[str]] = None
    paths:                Optional[MitraPathsSpec] = None
    connect_timeout_s:    Optional[float] = Field(None, gt=0, le=300)
    read_timeout_s:       Optional[float] = Field(None, gt=0, le=300)
    ws_connect_timeout_s: Optional[float] = Field(None, gt=0, le=300)
    ip_city:              Optional[str] = None
    ip_state:             Optional[str] = None
    ip_zip:               Optional[str] = None

class RemoteSpec(BaseModel):
    provider:  Literal["mitra"]
    flow_name: Literal["guest-mi-story", "guest-discussion"]
    # REQUIRED, and stored literally.
    #
    # These were read from os.environ (via `bot_route_env` / `company_env`),
    # which made them process-global -- and since Mitra identifies a profile by
    # (email, company), that meant one Saarthi process could serve exactly one
    # Mitra company with one set of bot routes. As plain spec fields they live
    # in agent_configs, so a tenant-scoped row carries its own values and two
    # tenants get genuinely separate Mitra profiles and story histories.
    #
    # min_length=1 rather than Optional: an empty company or bot route does not
    # fail loudly at Mitra, it silently resolves the wrong CompanyBot or splits
    # a user's profile. Rejecting it at validation is the only cheap place.
    bot_route: str = Field(min_length=1)
    company:   str = Field(min_length=1)
    # Names the variable holding this scope's Origin credential -- never the
    # value. THE ONE REMAINING ENVIRONMENT INDIRECTION, and it exists precisely
    # because Mitra gates admission on the Origin header, which makes it a
    # credential that must not be stored in a config row. Unset means the
    # deployment-wide MITRA_ORIGIN_URL.
    origin_env: Optional[str] = None
    connection: Optional[MitraConnectionSpec] = None
    default_language:    Literal["en","hi","kn","te"] = "en"
    supported_languages: List[str] = Field(default_factory=lambda: ["en","hi","kn","te"])
    handshake: MitraHandshakeSpec = Field(default_factory=MitraHandshakeSpec)
    turn:      MitraTurnSpec      = Field(default_factory=MitraTurnSpec)
    completion_poll_every_turn: bool = True
    # v1 vs v2 is a real behavioural choice (they resolve the story bot from
    # different Mitra tables), not a version preference -- see
    # MitraRestClient's module docstring.
    #
    # This WAS a Literal pinning the two paths. It cannot be any more: the
    # endpoints are configurable now (MITRA_FINALIZE_V1_PATH /
    # MITRA_FINALIZE_V2_PATH) and this module is import-pure by contract, so it
    # cannot reach Settings to build the Literal. The guard is not lost, only
    # moved: POST /api/agents/{key}/config asserts that this value equals one
    # of the endpoints THIS spec resolves to, which also catches a mismatch
    # between a scoped connection override and the path. A typo is rejected at
    # write time rather than discovered as a blank PDF.
    finalize_path:     str = "/api/end-story/v2/"
    # Finalize WITHOUT a user token -- v1 sends `access_token: null` in the
    # body, v2 sends no Authorization header.
    #
    # Not a security knob: Mitra derives `auth = access_token is not None`
    # (shikshalokam_story_utils.get_html_from_template) and uses it to pick the
    # PDF template's user_type (AUTH vs GUEST). A guest flow finalised WITH a
    # token therefore looks up a template that was never registered, and
    # get_html_from_template returns "" -- which save_project_story hands to
    # Gotenberg, producing a VALID BUT BLANK PDF with no error anywhere. That
    # is exactly how Capture Discussion shipped empty reports.
    #
    # It must match what MitraChannel._authenticate sends on the WebSocket
    # (`access_token: None` -- ws_channel.py). Interviewing as a guest and
    # finalising as an authenticated user is the mismatch, not either half.
    finalize_as_guest: bool = False
    # NOT report_path. MitraRestClient.get_report hardcodes /api/get-story/ and
    # never consulted this field, so setting it did nothing while looking like
    # it did. Silently-ignored config is worse than absent config -- if the
    # report endpoint ever needs to vary per agent, add it back together with
    # the code that reads it.
    report_media_type: str = "application/pdf"

class RemoteFlowAgentSpec(BaseAgentSpec):
    agent_type: Literal["remote_flow"]
    remote: RemoteSpec

# Use Annotated and Union for the discriminated union
from typing_extensions import Annotated
AgentSpec = Annotated[Union[LlmAgentSpec, RemoteFlowAgentSpec], Field(discriminator="agent_type")]

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
