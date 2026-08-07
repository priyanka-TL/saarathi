"""Session polling and control for a delegated (remote_flow) interview.

Responsible for: reading, finalising, resuming, abandoning a session and
serving its report URL.
Used by: the SPA, once /api/chat has returned a `session` payload.

Every route scopes the session to the caller via the owning conversation. A
wrong id and someone else's session both answer 404, never 403, so a client
cannot probe for sessions it does not own.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.container import Container
from app.dependencies.container import get_container
from app.dependencies.db import get_db
from app.dependencies.identity import get_current_user
from app.dependencies.orchestrator import get_orchestrator
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

# Seconds the client is told to wait before polling again on a 202.
#
# NOT Settings fields, deliberately: these go out IN THE RESPONSE BODY and the
# frontend polls on them, so they are part of the API contract rather than an
# operator tuning knob. The two differ because the waits differ -- a turn that
# is still running with Mitra settles in a few seconds, where finalisation has
# to generate a report.
TURN_PENDING_RETRY_AFTER_S = 3
FINALIZE_PENDING_RETRY_AFTER_S = 5


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
    orch: OrchestrationService = Depends(get_orchestrator),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    sid, _dto, miss = _resolve(session_id, db, user)
    if miss is not None:
        return miss

    try:
        result = orch.finalize_now(sid, user)
    except MitraError as e:
        return mitra_error_response(e)

    if result is None:
        return _not_found()
    return json_response(serialize_session(result, agent_key_for(container, result)))


@router.post("/api/sessions/{session_id}/resume", response_model=None)
def resume_session(
    session_id: str,
    container: Container = Depends(get_container),
    orch: OrchestrationService = Depends(get_orchestrator),
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
        result = orch.resume_turn(sid, user)
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
            "retry_after": TURN_PENDING_RETRY_AFTER_S,
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

    updated = SessionService(db).abandon_and_close(
        dto.conversation_id,
        reason="user_requested",
        actor=user.user_id,
        on_abandoned=(
            container.mitra_sessions.close
            if container.mitra_sessions is not None
            else None
        ),
    )

    # Idempotent: if there was no open session to abandon (already terminal),
    # fall back to returning the cached row rather than a spurious 404.
    final_dto = updated if updated is not None else dto
    return json_response(serialize_session(final_dto, agent_key_for(container, final_dto)))


@router.get("/api/sessions/{session_id}/report", response_model=None)
def get_report(
    session_id: str,
    container: Container = Depends(get_container),
    orch: OrchestrationService = Depends(get_orchestrator),
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

    report = orch.report_for(dto, user)
    if report.ready:
        return json_response({
            "report_url": report.report_url,
            "media_type": report.media_type,
            "story_id": report.story_id,
        })

    return json_response(
        {"retry_after": FINALIZE_PENDING_RETRY_AFTER_S}, status_code=202,
    )
