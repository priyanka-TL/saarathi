import uuid
from datetime import datetime
from typing import Optional, Dict, Any
from pydantic import BaseModel, ConfigDict


class AuditLogDTO(BaseModel):
    """Data Transfer Object for an AuditLog row."""
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: int
    action: str
    entity_type: str
    entity_id: Optional[uuid.UUID] = None
    actor: str
    request_id: Optional[str] = None
    before: Optional[Dict[str, Any]] = None
    after: Optional[Dict[str, Any]] = None
    note: Optional[str] = None
    created_at: datetime
