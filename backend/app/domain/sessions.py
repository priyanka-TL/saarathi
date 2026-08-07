"""Session DTOs.

Responsible for: the persisted-row shape of an agent session.
Used by: the session repository and SessionService.
"""
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

    state_data: Dict[str, Any]
    # `created_at` is the session's start. There is no separate `started_at`
    # column any more -- it always held exactly this value. serialize_session
    # still emits a "started_at" key, sourced from here, so the API is unchanged.
    last_activity_at: datetime
    finalized_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None

    created_by: str
    updated_by: str
    created_at: datetime
    updated_at: datetime
