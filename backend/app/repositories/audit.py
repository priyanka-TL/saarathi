import uuid
from typing import Optional, Dict, Any

from sqlalchemy.orm import Session

from app.models.orm import AuditLog, AuditActionEnum
from app.domain.audit import AuditLogDTO


class AuditLogRepository:
    def __init__(self, session: Session):
        self._session = session

    def insert(
        self,
        action: str,
        entity_type: str,
        entity_id: Optional[uuid.UUID] = None,
        actor: str = "system",
        request_id: Optional[str] = None,
        before: Optional[Dict[str, Any]] = None,
        after: Optional[Dict[str, Any]] = None,
        note: Optional[str] = None,
    ) -> AuditLogDTO:
        row = AuditLog(
            action=AuditActionEnum(action),
            entity_type=entity_type,
            entity_id=entity_id,
            actor=actor,
            request_id=request_id,
            before=before,
            after=after,
            note=note,
        )
        self._session.add(row)
        self._session.flush()
        self._session.refresh(row)
        return AuditLogDTO.model_validate(row)
