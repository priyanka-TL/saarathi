"""Conversation listing and transcript replay.

Split out of src/api/chat_routes.py; behaviour is unchanged.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.container import Container
from app.dependencies.container import get_container
from app.dependencies.db import get_db
from app.dependencies.identity import get_current_user
from app.domain.core import UserContext
from app.exceptions.envelope import error_response
from app.repositories.conversations import ConversationRepository
from app.repositories.sessions import AgentSessionRepository
from app.services.conversations import ConversationService
from app.utils.responses import json_response, parse_uuid
from app.utils.serializers import agent_key_for, serialize_session_summary

router = APIRouter(tags=["conversations"])


@router.get("/api/conversations", response_model=None)
def list_conversations(
    request: Request,
    limit: str = Query("5"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """The caller's most recently active conversations, newest first --
    powers the sidebar's recent-conversations list.

    `limit` is typed `str` and parsed by hand so that a non-integer produces
    the app's own 400 INVALID_REQUEST envelope. Declaring it `int` would let
    FastAPI answer 422 with its own body shape instead.
    """
    try:
        parsed_limit = int(limit)
    except (TypeError, ValueError):
        return error_response("limit must be an integer", "INVALID_REQUEST", 400)
    parsed_limit = max(1, min(parsed_limit, 20))

    svc = ConversationService(db)
    page = svc.list_recent(user, parsed_limit)

    return json_response({
        "conversations": [
            {
                "id": str(c.id),
                "title": c.title or "New conversation",
                "last_message_at": c.last_message_at.isoformat() if c.last_message_at else None,
                "message_count": c.message_count,
            }
            for c in page.conversations
        ]
    })


@router.get("/api/conversations/{conversation_id}/messages", response_model=None)
def get_conversation_messages(
    conversation_id: str,
    container: Container = Depends(get_container),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Full message history for one conversation, chronological -- powers
    resuming a conversation from the sidebar (or restoring it on reload)."""
    conv_id = parse_uuid(conversation_id)
    if conv_id is None:
        # Flask's <uuid:...> converter simply failed to match, so this was a
        # plain 404 that never entered the view. The client treats 404 here as
        # "forget this conversation", so it must stay a 404 and not become a
        # 422.
        return error_response("Conversation not found", "CONVERSATION_NOT_FOUND", 404)

    conv = ConversationRepository(db).get_scoped(conv_id, user)
    if conv is None:
        return error_response("Conversation not found", "CONVERSATION_NOT_FOUND", 404)

    svc = ConversationService(db)
    messages = svc.list_messages(conv_id)
    agent_names = svc.resolve_agent_names({m.agent_id for m in messages if m.agent_id})

    # The completion notice (story ready + report link) is rendered by the
    # client from the /api/chat response and is NOT a stored message, so a
    # reload replayed the transcript without it and the download link vanished.
    # Returning the sessions here lets the client rebuild that state from server
    # truth instead of persisting a synthetic message row for it.
    #
    # ALL of them, not just the latest. A conversation can hold several (the
    # router allows switching agents mid-conversation), and returning only the
    # newest meant a later agent's session masked an earlier COMPLETED one --
    # its report_url never reached the client, so reopening a finished story
    # from history showed no Download PDF button even though the report existed.
    session_dtos = AgentSessionRepository(db).list_for_conversation(conv_id)
    sessions_payload = [
        serialize_session_summary(dto, agent_key_for(container, dto)) for dto in session_dtos
    ]

    return json_response({
        "conversation_id": str(conv_id),
        # The agent-journey breadcrumb, in the same shape /api/chat returns and
        # derived from the same conversation_messages.agent_id sequence. Without
        # it the client had nothing to rebuild the journey from on resume, so
        # reopening a conversation from history collapsed
        # "Capture Discussions -> Record Stories -> General Support Agent" down
        # to whichever agent happened to speak last.
        "flow": svc.flow_payload(conv_id),
        "sessions": sessions_payload,
        "messages": [
            {
                "id": str(m.id),
                "role": m.role,
                "content": m.content,
                "agent_name": agent_names.get(m.agent_id),
                # Which session produced this message, so the client can anchor
                # per-session UI (the report link) to the right point in the
                # timeline instead of dumping it at the end.
                "agent_session_id": str(m.agent_session_id) if m.agent_session_id else None,
                # The RAW stored JSONB list, deliberately NOT re-projected to
                # {id,label,value} the way /api/chat does. The asymmetry is
                # part of the contract.
                "options": m.options,
                "selected_option_id": m.selected_option_id,
                "created_at": m.created_at.isoformat(),
            }
            for m in messages
        ],
    })
