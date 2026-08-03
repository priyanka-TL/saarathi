import enum
from uuid import UUID
from typing import Literal, List, Dict, Any, Optional, Protocol, ClassVar
from dataclasses import dataclass, field

from app.domain.core import UserContext
from app.domain.agent_spec import AgentSpec

class SessionState(enum.Enum):
    pending = "pending"
    authenticating = "authenticating"
    in_progress = "in_progress"
    awaiting_user = "awaiting_user"
    finalizing = "finalizing"
    completed = "completed"
    failed = "failed"
    abandoned = "abandoned"

@dataclass(frozen=True)
class AgentSessionView:
    id: UUID
    conversation_id: UUID
    agent_id: UUID
    state: SessionState
    remote_session_id: Optional[str]
    remote_profile_id: Optional[str]
    remote_flow: Optional[str]
    remote_bot_route: Optional[str]
    language: str
    step: int
    turn_count: int
    result_ref: Optional[str]
    report_url: Optional[str]
    error: Optional[str]
    state_data: dict

@dataclass(frozen=True)
class HistoryTurn:
    role: Literal["user", "assistant"]
    content: str
    agent_key: Optional[str]

@dataclass(frozen=True)
class TurnContext:
    request_id: str
    conversation_id: UUID
    user: UserContext
    text: str
    option_id: Optional[str]
    history: List[HistoryTurn]
    session: Optional[AgentSessionView]
    locale: str

@dataclass(frozen=True)
class Option:
    id: str
    label: str
    value: str

@dataclass(frozen=True)
class ToolTrace:
    tool_name: str
    iteration: int
    arguments: dict
    result_excerpt: str
    status: Literal["success", "error", "timeout"]
    error: Optional[str]
    duration_ms: int

@dataclass(frozen=True)
class SessionDelta:
    state: SessionState
    remote_session_id: Optional[str] = None
    remote_profile_id: Optional[str] = None
    remote_flow: Optional[str] = None
    remote_bot_route: Optional[str] = None
    step: Optional[int] = None
    result_ref: Optional[str] = None
    report_url: Optional[str] = None
    error: Optional[str] = None
    state_data: Optional[dict] = None

@dataclass(frozen=True)
class AgentTurn:
    text: str
    options: List[Option] = field(default_factory=list)
    session_delta: Optional[SessionDelta] = None
    tool_traces: List[ToolTrace] = field(default_factory=list)
    model: Optional[str] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    latency_ms: int = 0
    error: Optional[str] = None
    terminal: bool = False

class AgentHandler(Protocol):
    agent_type: ClassVar[str]
    def __init__(self, spec: AgentSpec, deps: Any) -> None: ...
    def handle(self, ctx: TurnContext) -> AgentTurn: ...
