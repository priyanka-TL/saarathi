"""The chat surface: POST /api/chat and POST /api/reset.

Responsible for: validating the request, delegating one turn, mapping failures.
Used by: the SPA on every message the user sends.

The turn pipeline itself is OrchestrationService; /api/reset is
ConversationService.begin_new_chat. This module holds neither.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.container import Container
from app.core.logger import get_logger
from app.dependencies.body import json_body_silent, json_body_strict
from app.dependencies.container import get_container
from app.dependencies.db import get_db
from app.dependencies.identity import get_current_user
from app.dependencies.orchestrator import get_orchestrator
from app.dependencies.request_context import get_request_id
from app.domain.core import UserContext
from app.domain.scope import scope_for_user
from app.exceptions.domain import SaarthiError
from app.exceptions.envelope import error_response, upstream_error_response
from app.providers.errors import ProviderError
from app.services.conversations import ConversationService
from app.services.orchestration import (
    ConcurrentTurnError,
    OrchestrationService,
    TurnInput,
    TurnLimitExceeded,
)
from app.utils.responses import json_response, parse_uuid
from app.utils.serializers import serialize_session_summary

logger = get_logger("api.chat")

router = APIRouter(tags=["chat"])


@router.post("/api/chat", response_model=None)
def chat(
    data: Optional[Dict[str, Any]] = Depends(json_body_strict),
    request_id: Optional[str] = Depends(get_request_id),
    container: Container = Depends(get_container),
    orch: OrchestrationService = Depends(get_orchestrator),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Handle one incoming chat message."""
    # `"message" in data`, NOT truthiness: an empty string is a valid message
    # and must reach the orchestrator (pinned by test_chat_errors.py).
    if not data or "message" not in data:
        return error_response("No message provided", "INVALID_REQUEST", 400)

    user_message = data["message"]
    target_agent = data.get("agent_key") or data.get("agent_name")
    conversation_id_str = data.get("conversation_id")
    option_id = data.get("option_id")
    # locale defaults to user.locale in the DB logic

    # Parsed BEFORE the try, so a malformed id is the client error it is rather
    # than being swallowed by the generic handler below and reported as a 500.
    req_conv_id = parse_uuid(conversation_id_str) if conversation_id_str else None
    if conversation_id_str and req_conv_id is None:
        return error_response("conversation_id must be a UUID", "INVALID_REQUEST", 400)

    try:
        ctx_in = TurnInput(
            request_id=request_id,
            conversation_id=req_conv_id,
            user=user,
            text=user_message,
            option_id=option_id,
            # The literal display name "Saarthi" means "no explicit selection,
            # route me" -- it is the synthetic first entry in /api/agents and
            # has no key.
            agent_key=target_agent if target_agent and target_agent != "Saarthi" else None,
            autostart=bool(data.get("autostart")),
        )

        res = orch.handle_turn(ctx_in)
        svc = ConversationService(db)

        session_payload = None
        if res.session is not None:
            session_payload = serialize_session_summary(res.session, res.agent.key)

        return json_response({
            "agent_name": res.agent.name,
            "response": res.turn.text,
            "status": "success",
            "flow": svc.flow_payload(res.conversation.id),
            "conversation_id": str(res.conversation.id),
            "agent_key": res.agent.key,
            "agent_type": res.agent.spec.agent_type,
            "options": [{"id": o.id, "label": o.label, "value": o.value} for o in res.turn.options],
            # Downloadable documents this turn produced. ALWAYS PRESENT, empty
            # for a turn that produced none -- a key that appears and disappears
            # is harder for a client to consume than an empty list.
            "attachments": [
                {
                    "file_name": a.file_name,
                    "format": a.format,
                    "media_type": a.media_type,
                    "url": a.url,
                }
                for a in res.turn.attachments
            ],
            "session": session_payload,
        })
    except ConcurrentTurnError:
        # A double-submit. 409 and NOT an automatic retry: re-sending is what
        # merges two user messages into one upstream and destroys an answer.
        return error_response(
            "A reply is already on its way. Please wait for it before sending again.",
            "CONCURRENT_TURN", 409,
        )
    except TurnLimitExceeded as e:
        return error_response(e.detail, "RATE_LIMITED", 429)
    except ProviderError as e:
        # Every remote platform's failures, in one clause. A provider that adds
        # an exception type inherits this mapping instead of needing a catch.
        return upstream_error_response(e)
    except SaarthiError as e:
        # Every mapped domain failure, LLM ones included, in one clause. Each
        # carries its own status, code and client-safe message, so a new domain
        # exception needs no edit here.
        return error_response(e.public_message, e.error_code, e.status_code)
    except Exception as e:  # noqa: BLE001 -- mirrors the original blanket catch
        # exc_info puts the traceback in the JSON record; the fields make this
        # answerable without a redeploy.
        tenant_id, organization_id = scope_for_user(user)
        logger.error(
            "Error handling request: %s", e,
            exc_info=True,
            extra={
                "conversation_id": str(req_conv_id) if req_conv_id else None,
                "tenant_id": tenant_id,
                "organization_id": organization_id,
                "user_id": getattr(user, "user_id", None),
                "agent_key": target_agent,
            },
        )
        return error_response("An internal error occurred.", "INTERNAL", 500)


@router.post("/api/reset", response_model=None)
def reset(
    data: Dict[str, Any] = Depends(json_body_silent),
    container: Container = Depends(get_container),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Clear the conversation and start a new flow.

    The body is optional: the frontend posts here with no body and no
    Content-Type at all, which json_body_silent tolerates.
    """
    conversation_id_str = data.get("conversation_id")
    req_conv_id = parse_uuid(conversation_id_str) if conversation_id_str else None
    if conversation_id_str and req_conv_id is None:
        # No handler of its own here, so a malformed id would escape as a 500.
        return error_response("conversation_id must be a UUID", "INVALID_REQUEST", 400)

    # Ordering lives in the service. This route supplies only what the service
    # cannot know: how to drop the conversation's transport.
    #
    # PROVIDER-AGNOSTIC. This used to close one named platform's pool, so a
    # conversation abandoned on the other platform left its socket open until
    # the idle reaper eventually noticed.
    new_conv = ConversationService(db).begin_new_chat(
        req_conv_id,
        user,
        on_session_abandoned=container.providers.close_conversation,
    )

    return json_response({
        "status": "success",
        "conversation_id": str(new_conv.id),
    })
