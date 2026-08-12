"""The AgentSpec model -- what an agent IS, as validated configuration.

Responsible for: the discriminated union of agent types and their nested specs,
plus the canonical JSON + checksum used for versioning.
Used by: the admin route on write, the registry on load, handlers at runtime.

PURE: imports nothing outside app.domain, so it cannot reach Settings or a
provider registry. That purity is what stops onboarding a new external platform
from ever editing this file.

NOTHING PER-PROVIDER LIVES HERE, and that is the point. This module used to
carry five Mitra-shaped models, six of Mitra's URL paths and a browser
User-Agent string -- duplicated from the REST client, because the import rule
above forbade importing it, with a test to keep the two copies in step. All of
that now lives in the provider package that uses it. What is left is the
ENVELOPE, and the rule that decides its contents is:

    A field belongs in RemoteSpec if and only if Saarathi's own core reads it.
    Everything else is the provider's business and goes in `options`.

`options` is therefore opaque HERE and typed THERE, in two stages:

    stage 1  RemoteSpec (this file)          the envelope, extra="forbid"
    stage 2  the provider's options_model    the block, also extra="forbid"

Stage 2 runs at config-write time (the admin route), at registry load, and again
when the provider is built. Opacity costs nothing for multi-tenancy: the
checksum that separates one tenant's cached handler from another's is computed
from `_source_dict`, the raw input, so it never depended on the domain
understanding the shape.
"""
import json
import hashlib
import copy
from typing import Any, Dict, Literal, Optional, Union, List

from pydantic import BaseModel, Field, ConfigDict, model_validator, PrivateAttr

from app.domain.core import UserContext

#: What a finalised interview report is fetched as, unless a spec overrides it.
#: Declared here (rather than defaulted separately in RemoteSpec, in
#: the provider's artifact fetch and in the sessions router) because the three
#: have to agree: the media type requested upstream is what the browser is then
#: told it is downloading.
DEFAULT_REPORT_MEDIA_TYPE = "application/pdf"


#: EVERY MODEL IN THIS FILE SETS THIS. pydantic does NOT propagate a parent's
#: `extra` setting into a nested FIELD model, so declaring it on BaseAgentSpec
#: alone left every sub-model accepting unknown keys in silence. The failure that
#: reaches is not a validation error but a wrong default: renaming a field by one
#: doubled letter validated cleanly, the field took its default, and the
#: downstream call returned HTTP 200 with blank output and nothing logged.
STRICT = ConfigDict(extra="forbid")


class ModelSpec(BaseModel):
    model_config = STRICT

    provider:    Literal["openrouter"] = "openrouter"
    name:        str
    temperature: float      = Field(0.0, ge=0.0, le=2.0)
    max_tokens:  Optional[int] = Field(None, ge=1, le=32768)
    timeout_s:   float      = Field(30.0, gt=0, le=300)

class RoutingSpec(BaseModel):
    model_config = STRICT

    keywords:             List[str] = Field(default_factory=list)
    priority:             int   = Field(50, ge=0, le=100)
    pin_session:          bool  = False
    exit_keywords:        List[str] = Field(default_factory=lambda: ["/exit", "cancel", "stop"])
    confidence_threshold: float = Field(0.5, ge=0.0, le=1.0)

    #: May this agent's OPEN SESSION be displaced mid-conversation when the user
    #: plainly wants a different agent?
    #:
    #: FALSE IS THE SAFE DEFAULT AND MUST STAY SO. A pinned session is what
    #: stops a half-finished interview being hijacked: a user answering "I want
    #: to tell my story about attendance" is talking TO the interview, not
    #: asking to leave it, and re-routing them would destroy the run.
    #:
    #: TRUE is for an agent whose conversation has no end. An open-ended
    #: assistant never reports completion, so its session never becomes
    #: terminal and Gate 2 would otherwise pin the conversation to it forever --
    #: which is exactly what happened to `saathi`. Such an agent yields instead:
    #: it keeps the turn unless the router has positive evidence for a different
    #: agent, and the FLOOR is staying pinned, never the default agent.
    #:
    #: See RouterService.select Gate 2.
    yields_to_keyword:    bool = False

    #: The bar a YIELD decision must clear, set on the PINNED agent -- not on
    #: the candidate being yielded to. `None` (the default) falls back to the
    #: candidate's own `confidence_threshold`, i.e. today's behaviour.
    #:
    #: A candidate's `confidence_threshold` is tuned for cheap first-message
    #: routing (Gate 4), where being wrong just means one extra turn. Reusing
    #: that same low bar to interrupt an ALREADY-IN-PROGRESS pinned
    #: conversation is a different, higher-stakes decision and deserves its
    #: own, stricter threshold.
    yield_confidence_threshold: Optional[float] = Field(None, ge=0.0, le=1.0)
    router_selectable:    bool  = True
    direct_selectable:    bool  = True

class MemorySpec(BaseModel):
    model_config = STRICT

    strategy:      Literal["recent", "none"] = "recent"
    history_turns: int  = Field(10, ge=0, le=100)
    include_other_agents: bool = True

class LimitsSpec(BaseModel):
    model_config = STRICT

    max_tool_iterations: int        = Field(3, ge=0, le=10)
    max_turns:           Optional[int] = None
    rate_limit_per_conversation_per_min: int = Field(20, ge=1)
    rate_limit_per_user_per_min:         int = Field(60, ge=1)

class RetrySpec(BaseModel):
    model_config = STRICT

    max_attempts:     int = Field(3, ge=1, le=5)
    backoff:          Literal["none", "linear", "exponential"] = "exponential"
    initial_delay_ms: int = Field(500, ge=0)
    retry_on: List[Literal["timeout","rate_limit","server_error","connection_error"]] = \
              Field(default_factory=lambda: ["timeout", "rate_limit", "server_error"])

class AccessSpec(BaseModel):
    model_config = STRICT

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
    model_config = STRICT

    emit_options:           bool = False
    streaming:              bool = False
    record_tool_executions: bool = True

class BaseAgentSpec(BaseModel):
    model_config = STRICT

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
        more and no expansion: a config row holds literal values, and a
        deployment or a tenant differs by having its own row. The one exception
        is `RemoteSpec.auth`, whose `*_env` fields name variables rather than
        holding values, because credentials must not sit in a row the admin API
        can read back.
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

class RemoteAuthSpec(BaseModel):
    """How this agent authenticates to its remote platform.

    NAMES ENVIRONMENT VARIABLES, NEVER HOLDS VALUES. A config row is readable
    through the admin API, so every credential is referenced by the NAME of the
    variable that holds it and resolved with os.getenv at connection-build time.
    That indirection is not a convenience -- it is what lets endpoint and
    company be per-tenant configuration while the secrets stay deployment-level.

    `credential_env` is required and names the variable holding the Origin
    header. These platforms gate admission on it (Django Channels'
    AllowedHostsOriginValidator), so it is a credential rather than a
    preference, and an absent one is a 403 that reads like an outage.

    `scheme` is a plain str, validated by the PROVIDER rather than here: which
    schemes exist is provider knowledge, and enumerating them in the pure domain
    layer would mean a new platform could not authenticate without editing this
    file. `origin_header` is the only scheme any provider needs today -- Saathi
    used to mint its own per-user token from a `token_endpoint`/`identifier_env`/
    `secret_env`/`tenant_code` block (schemes "elevate_login"/"static_token"),
    but that credential now comes from the logged-in caller's own UserContext,
    never from connection config, so those fields were removed rather than left
    unused.
    """
    model_config = STRICT

    scheme: str = "origin_header"
    credential_env: str = Field(min_length=1)


class RemoteTimeoutsSpec(BaseModel):
    model_config = STRICT

    connect_s:        float = Field(10.0, gt=0, le=300)
    read_s:           float = Field(30.0, gt=0, le=300)
    stream_connect_s: float = Field(10.0, gt=0, le=300)


class RemoteTurnSpec(BaseModel):
    """Per-turn timing.

    IN THE ENVELOPE, not in `options`, because the core reads it: these numbers
    bound how long the orchestrator's advisory lock is held and decide when a
    turn becomes a 504.
    """
    model_config = STRICT

    first_turn_timeout_ms: int = 60000
    turn_timeout_ms:       int = 45000
    idle_gap_ms:           int = 8000


class RemoteSpec(BaseModel):
    """One remote_flow agent's binding to an external platform.

    WHY THESE ARE FIELDS AND NOT ENVIRONMENT VARIABLES. The whole connection
    block was once a set of per-platform `*_URL` / `*_ENABLED` settings, which
    made the binding process-global: one Saarthi process could serve exactly one
    deployment of one platform as one company. Since these platforms identify a
    profile by (email, company), that was the difference between every tenant
    sharing one remote profile and each having its own.

    As plain spec fields they live in `agent_configs`, which is scoped
    (tenant_id, organization_id) -- so a scoped row carries its own values and
    two tenants get genuinely separate remote profiles and histories.

    EVERY FIELD BELOW IS READ BY SAARATHI'S OWN CORE. That is the entry test.
    Anything only the remote platform cares about -- its URL paths, its bot
    route, its company, its finalisation switches -- goes in `options`, where
    the provider's own model types it.
    """
    model_config = STRICT

    #: Registry key, resolved by ProviderRegistry. A plain str and not a
    #: Literal: enumerating providers here would mean the pure domain layer had
    #: to be edited to onboard a platform, which is exactly what this design
    #: removes. An unregistered name is rejected at config-write time and skips
    #: that one agent at load time.
    provider:  str = Field(pattern=r"^[a-z][a-z0-9_]{1,31}$")
    transport: Literal["websocket", "rest"] = "websocket"

    # No default: there is no env fallback, and an empty host is not a failure
    # anyone can read off a stack trace.
    base_url:   str = Field(min_length=1)
    #: The streaming endpoint. Required when transport is "websocket".
    stream_url: str = ""

    #: Extra hosts whose artifact URLs may be fetched. An SSRF control,
    #: intersected with PROVIDER_HOST_CEILING when the operator has set one.
    allowed_hosts: List[str] = Field(default_factory=list)
    timeouts: RemoteTimeoutsSpec = Field(default_factory=RemoteTimeoutsSpec)
    #: Sent on every request and on the socket upgrade. "User-Agent" is read out
    #: of here rather than given its own field, so a platform expecting other
    #: fixed headers needs no schema change.
    headers: Dict[str, str] = Field(default_factory=dict)
    auth: RemoteAuthSpec

    #: Persisted to agent_sessions.remote_flow and returned in the API response,
    #: which is what makes it core rather than provider business. Not a Literal:
    #: the set of flows is the remote platform's own configuration, so
    #: enumerating it meant a new flow could not be configured without a code
    #: change.
    flow_name: str = Field(min_length=1)

    default_language:    Literal["en","hi","kn","te"] = "en"
    supported_languages: List[str] = Field(default_factory=lambda: ["en","hi","kn","te"])
    turn: RemoteTurnSpec = Field(default_factory=RemoteTurnSpec)

    #: Whether this flow ends by producing an artifact at all.
    #:
    #: FALSE IS NOT A DEGRADED MODE, it is a different kind of flow. An
    #: interview finalises into a PDF; an assistant conversation does not, and a
    #: platform reporting `create_story: "none"` raises if finalize() is called
    #: on it, leaving the session `failed`. This flag is what keeps a terminal
    #: turn from being reported as an outage. See app/services/turn_finalization.py.
    produces_artifact: bool = True
    report_media_type: str = DEFAULT_REPORT_MEDIA_TYPE

    #: OPAQUE HERE, TYPED BY THE PROVIDER. Validated against
    #: `provider.options_model` (which must set extra="forbid") at config-write
    #: time, at registry load, and when the provider is built. See the module
    #: docstring for why leaving it opaque is safe for multi-tenancy.
    options: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _stream_url_required_for_websocket(self) -> "RemoteSpec":
        """A websocket transport with no stream_url fails at the first turn with
        a connection error naming an empty host, which reads like an outage
        rather than a missing field."""
        if self.transport == "websocket" and not self.stream_url:
            raise ValueError("stream_url is required when transport is 'websocket'")
        return self


class RemoteFlowAgentSpec(BaseAgentSpec):
    """An agent whose whole turn is delegated to an external platform.

    THE ONLY DELEGATED AGENT TYPE. There was briefly a second one
    (`saathi_flow`), created solely because two platforms had independent
    `*_ENABLED` switches and the registry hid agents BY TYPE. Enablement is now
    per provider, so the second type had nothing left to express -- which is the
    shape to keep: a new platform is a new `remote.provider` value, never a new
    agent_type, and therefore never a Postgres enum migration.
    """
    agent_type: Literal["remote_flow"]
    remote: RemoteSpec


# Use Annotated and Union for the discriminated union
from typing_extensions import Annotated
AgentSpec = Annotated[
    Union[LlmAgentSpec, RemoteFlowAgentSpec],
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
