import os
import re
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

def _resolve_env_string(value: str) -> str:
    def replacer(match):
        var_name = match.group(1)
        default = match.group(3)
        if var_name in os.environ:
            return os.environ[var_name]
        elif default is not None:
            return default
        else:
            return "" # Bash-like behavior: substitute empty string if missing and no default
            
    pattern = re.compile(r'\$\{([A-Za-z0-9_]+)(:-(.*?))?\}')
    return pattern.sub(replacer, value)

def _resolve_env_in_dict(d: Any) -> Any:
    if isinstance(d, dict):
        return {k: _resolve_env_in_dict(v) for k, v in d.items()}
    elif isinstance(d, list):
        return [_resolve_env_in_dict(v) for v in d]
    elif isinstance(d, str):
        return _resolve_env_string(d)
    return d

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

    _unresolved_dict: dict = PrivateAttr(default_factory=dict)

    @model_validator(mode='wrap')
    @classmethod
    def resolve_and_store(cls, v: Any, handler: Any) -> Any:
        if isinstance(v, dict):
            unresolved = copy.deepcopy(v)
            resolved = _resolve_env_in_dict(v)
            model = handler(resolved)
            model._unresolved_dict = unresolved
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

class RemoteSpec(BaseModel):
    provider:  Literal["mitra"]
    flow_name: Literal["guest-mi-story", "guest-discussion"]
    bot_route_env: str
    company_env:   str = "MITRA_COMPANY"
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
    # moved and widened: ConfigSyncService asserts at startup that this value
    # equals one of the CONFIGURED finalize endpoints, which also catches a
    # YAML/settings mismatch the Literal could not see. A typo still fails
    # before any interview runs, with the agent key in the message.
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
    """
    Returns a deterministic JSON serialization of the unresolved spec,
    along with its SHA256 checksum.
    The spec can be an AgentSpec instance.
    """
    if hasattr(spec, "_unresolved_dict") and spec._unresolved_dict:
        # Pydantic v2 discriminator might be present in the instance but missing from raw dict if it was inferred?
        # Actually agent_type is required in the input.
        raw_dict = spec._unresolved_dict
    else:
        # Fallback if someone serialized to dict manually
        raw_dict = spec.model_dump(mode="json")
        
    json_str = json.dumps(raw_dict, separators=(',', ':'), sort_keys=True)
    sha = hashlib.sha256(json_str.encode('utf-8')).hexdigest()
    return json_str, sha
