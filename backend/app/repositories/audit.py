"""Persistence for `audit_log`.

Responsible for: recording who changed what, with before/after snapshots.
Used by: every service that mutates configuration or session state.
"""
import uuid
from typing import Optional, Dict, Any

from sqlalchemy.orm import Session

from app.models.orm import SYSTEM_ACTOR, AuditLog, AuditActionEnum
from app.domain.audit import AuditLogDTO


class AuditLogRepository:
    def __init__(self, session: Session):
        self._session = session

    def insert(
        self,
        action: str,
        entity_type: str,
        entity_id: Optional[uuid.UUID] = None,
        actor: str = SYSTEM_ACTOR,
        request_id: Optional[str] = None,
        before: Optional[Dict[str, Any]] = None,
        after: Optional[Dict[str, Any]] = None,
        note: Optional[str] = None,
    ) -> AuditLogDTO:
        """`actor` is the caller-facing name and stays -- it is what an audit
        row is ABOUT. It lands in the standard `created_by` column rather than a
        bespoke `actor` one, so the table that exists to record provenance spells
        provenance the same way the other eight do."""
        row = AuditLog(
            action=AuditActionEnum(action),
            entity_type=entity_type,
            entity_id=entity_id,
            request_id=request_id,
            before=before,
            after=after,
            note=note,
            created_by=actor,
            updated_by=actor,
        )
        self._session.add(row)
        self._session.flush()
        self._session.refresh(row)
        return AuditLogDTO.model_validate(row)
