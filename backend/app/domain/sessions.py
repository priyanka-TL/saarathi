import uuid
from datetime import datetime
from typing import Optional, Dict, Any
from pydantic import BaseModel, ConfigDict


class AgentSessionDTO(BaseModel):
    """
    Data Transfer Object for an AgentSession -- the full persisted row.

    Distinct from app.agents.protocol.AgentSessionView, which is a narrower,
    handler-facing read model (no timestamp columns) carried inside TurnContext.
    """
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    conversation_id: uuid.UUID
    agent_id: uuid.UUID
    state: str

    remote_provider: Optional[str] = None
    remote_session_id: Optional[str] = None
    remote_profile_id: Optional[str] = None
    remote_flow: Optional[str] = None
    remote_bot_route: Optional[str] = None
    language: str
    step: int
    turn_count: int

    result_ref: Optional[str] = None
    report_url: Optional[str] = None
    error: Optional[str] = None
    error_code: Optional[str] = None

    state_data: Dict[str, Any]
    started_at: datetime
    last_activity_at: datetime
    finalized_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
