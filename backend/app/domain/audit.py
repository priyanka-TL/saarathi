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
    request_id: Optional[str] = None
    before: Optional[Dict[str, Any]] = None
    after: Optional[Dict[str, Any]] = None
    note: Optional[str] = None

    #: The actor. Named created_by like the audit block on every other table.
    created_by: str
    updated_by: str
    created_at: datetime
    updated_at: datetime
