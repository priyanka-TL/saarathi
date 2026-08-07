"""Conversation DTOs.

Responsible for: the shapes repositories return and services pass around.
Used by: the conversation repository and service.
"""
import uuid
from datetime import datetime
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, ConfigDict

class ConversationDTO(BaseModel):
    """
    Data Transfer Object for a Conversation.

    There is no `pinned_agent_id`: a conversation is not bound to one agent.
    Which agent is currently driving is the conversation's one non-terminal
    `agent_sessions` row -- see AgentSessionRepository.get_open_for_conversation.
    """
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    tenant_code: str
    organization_id: Optional[str] = None
    user_id: str
    title: Optional[str] = None
    status: str
    locale: str
    message_count: int
    metadata_: Dict[str, Any]
    last_message_at: Optional[datetime] = None

    created_by: str
    updated_by: str
    created_at: datetime
    updated_at: datetime

class MessageDTO(BaseModel):
    """
    Data Transfer Object for a ConversationMessage.
    """
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    conversation_id: uuid.UUID
    seq: int
    role: str
    content: str
    
    agent_id: Optional[uuid.UUID] = None
    agent_session_id: Optional[uuid.UUID] = None
    route_reason: Optional[str] = None
    route_confidence: Optional[float] = None
    
    options: Optional[List[Dict[str, Any]]] = None
    selected_option_id: Optional[str] = None
    
    model: Optional[str] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    latency_ms: Optional[int] = None
    error: Optional[str] = None
    request_id: Optional[str] = None

    created_by: str
    updated_by: str
    created_at: datetime
    updated_at: datetime

class ConversationPageDTO(BaseModel):
    conversations: List[ConversationDTO]
    next_cursor: Optional[datetime] = None

class MessagePageDTO(BaseModel):
    messages: List[MessageDTO]
    next_seq: Optional[int] = None
