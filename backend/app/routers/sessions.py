"""Session polling/control endpoints (design doc §10.2): a client drives and
observes a pinned (remote_flow) interview through these once /api/chat itself
has returned a `session` payload.

Every route scopes the session to the caller's tenant/identity via the
owning conversation -- a wrong id OR someone else's session both come back
as a plain 404, never 403, so a client can't probe for the existence of a
session it doesn't own.

Port of src/api/session_routes.py.
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.container import Container
from app.dependencies.container import get_container
from app.dependencies.db import get_db
from app.dependencies.identity import get_current_user
from app.domain.core import UserContext
from app.exceptions.envelope import error_response, mitra_error_response
from app.integrations.mitra.exceptions import MitraError
from app.integrations.mitra.turn_recovery import TurnOutcome
from app.repositories.conversations import ConversationRepository
from app.services.orchestration import OrchestrationService
from app.services.session_service import SessionService
from app.utils.responses import json_response, parse_uuid
from app.utils.serializers import agent_key_for, serialize_session

router = APIRouter(tags=["sessions"])


def _not_found() -> JSONResponse:
    return error_response("Session not found", "SESSION_NOT_FOUND", 404)


def _scoped_session(db: Session, session_id: uuid.UUID, user: UserContext):
    """Returns the session's DTO only if the owning conversation belongs to
    the caller's tenant/identity. Returns None on any miss -- caller maps
    that straight to a 404."""
    dto = SessionService(db).get(session_id)
    if dto is None:
        return None
    conv = ConversationRepository(db).get_scoped(dto.conversation_id, user)
    if conv is None:
        return None
    return dto


def _orchestrator(db: Session, container: Container) -> OrchestrationService:
    return OrchestrationService(
        session=db,
        registry=container.agent_registry,
        handler_factory=container.handler_factory,
        llm_factory=container.llm_factory,
        mitra_sessions=container.mitra_sessions,
        mitra_clients=container.mitra_clients,
        settings=container.settings,
    )


def _resolve(session_id: str, db: Session, user: UserContext):
    """Parse + scope in one step. Returns (dto, None) or (None, 404 response)."""
    sid = parse_uuid(session_id)
    if sid is None:
        return None, None, _not_found()
    dto = _scoped_session(db, sid, user)
    if dto is None:
        return None, None, _not_found()
    return sid, dto, None


@router.get("/api/sessions/{session_id}", response_model=None)
def get_session(
    session_id: str,
    container: Container = Depends(get_container),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    _sid, dto, miss = _resolve(session_id, db, user)
    if miss is not None:
        return miss
    return json_response(serialize_session(dto, agent_key_for(container, dto)))


@router.post("/api/sessions/{session_id}/finalize", response_model=None)
def finalize_session(
    session_id: str,
    container: Container = Depends(get_container),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    sid, _dto, miss = _resolve(session_id, db, user)
    if miss is not None:
        return miss

    try:
        result = _orchestrator(db, container).finalize_now(sid, user)
    except MitraError as e:
        return mitra_error_response(e)

    if result is None:
        return _not_found()
    return json_response(serialize_session(result, agent_key_for(container, result)))


@router.post("/api/sessions/{session_id}/resume", response_model=None)
def resume_session(
    session_id: str,
    container: Container = Depends(get_container),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Recover a turn Saarthi stopped listening for -- WITHOUT re-sending it.

    This is what the UI's Retry button must call during a remote_flow
    interview. Re-POSTing /api/chat with the same text is unsafe: Mitra had
    already answered and moved on, so the answer landed against the NEXT
    question (§1.6 answer destruction). This route is read-only against Mitra.

    Three outcomes, mirroring turn_recovery.TurnOutcome, with THREE DIFFERENT
    key sets -- which is why this route cannot have a response_model:
      200 {response, ...}    Mitra had answered; the reply is now stored.
      202 {retry_after}      Mitra is still generating; poll again.
      200 {can_resend: true} Mitra never received it; re-sending IS safe.
    """
    sid, _dto, miss = _resolve(session_id, db, user)
    if miss is not None:
        return miss

    try:
        result = _orchestrator(db, container).resume_turn(sid, user)
    except MitraError as e:
        return mitra_error_response(e)

    if result is None:
        return _not_found()

    if result.outcome is TurnOutcome.ANSWERED:
        return json_response({
            "status": "success",
            "outcome": result.outcome.value,
            "response": result.text,
            "options": [],   # not recoverable over REST -- see _recover_timed_out_turn
            "session": serialize_session(
                result.session, agent_key_for(container, result.session)
            ),
        })

    if result.outcome is TurnOutcome.PENDING:
        # No `session` and no `response` key here, on purpose.
        return json_response({
            "status": "pending",
            "outcome": result.outcome.value,
            "retry_after": 3,
        }, status_code=202)

    return json_response({
        "status": "success",
        "outcome": result.outcome.value,
        # The ONLY case where the client may re-submit the user's text.
        "can_resend": True,
    })


@router.post("/api/sessions/{session_id}/abandon", response_model=None)
def abandon_session(
    session_id: str,
    container: Container = Depends(get_container),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    _sid, dto, miss = _resolve(session_id, db, user)
    if miss is not None:
        return miss

    updated = SessionService(db).abandon(
        dto.conversation_id, reason="user_requested", actor=user.user_id,
    )
    if updated is not None and container.mitra_sessions is not None:
        container.mitra_sessions.close(dto.conversation_id)

    # Idempotent: if there was no open session to abandon (already terminal),
    # fall back to returning the cached row rather than a spurious 404.
    final_dto = updated if updated is not None else dto
    return json_response(serialize_session(final_dto, agent_key_for(container, final_dto)))


@router.get("/api/sessions/{session_id}/report", response_model=None)
def get_report(
    session_id: str,
    container: Container = Depends(get_container),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Serves a report URL, never a file.

    The 202 body carries no envelope at all -- just {"retry_after": 5} -- which
    is what the client polls on. Note MitraError is swallowed into that 202
    here, unlike every other session route.
    """
    _sid, dto, miss = _resolve(session_id, db, user)
    if miss is not None:
        return miss

    # SCOPE-RESOLVED, like every other read of an agent off a session.
    # get_by_id alone answers from the default-scope snapshot, so a tenant that
    # had customised report_media_type -- or that points at its own Mitra --
    # would have had its report fetched with the default scope's settings.
    orch = _orchestrator(db, container)
    agent = orch.agent_for_session(dto, user)
    media_type: Optional[str] = (
        agent.spec.remote.report_media_type if agent is not None else "application/pdf"
    )

    if dto.report_url:
        return json_response(
            {"report_url": dto.report_url, "media_type": media_type, "story_id": dto.result_ref}
        )

    rest = orch.rest_for(agent)
    if dto.state == "completed" and rest is not None and dto.remote_session_id:
        try:
            url = rest.get_report(dto.remote_session_id, media_type=media_type)
        except MitraError:
            url = None
        if url:
            return json_response(
                {"report_url": url, "media_type": media_type, "story_id": dto.result_ref}
            )

    return json_response({"retry_after": 5}, status_code=202)
