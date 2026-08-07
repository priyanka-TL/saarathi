"""The turn endpoint and the conversation reset.

Port of the /api/chat and /api/reset halves of src/api/chat_routes.py.

This router is a plain `def`, like every other. That is what lets
OrchestrationService.handle_turn hold a session-scoped Postgres advisory lock
on one connection across a commit and across a handler call of up to 60s. See
app/dependencies/db.py and app/main.py.
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
from app.dependencies.request_context import get_request_id
from app.domain.core import UserContext
from app.domain.scope import scope_for_user
from app.exceptions.domain import SaarthiError
from app.exceptions.envelope import error_response, mitra_error_response
from app.integrations.mitra.exceptions import MitraError
from app.services.conversations import ConversationService
from app.services.orchestration import (
    ConcurrentTurnError,
    OrchestrationService,
    TurnInput,
    TurnLimitExceeded,
)
from app.services.session_service import SessionService
from app.utils.responses import json_response, parse_uuid
from app.utils.serializers import serialize_session_summary

logger = get_logger("api.chat")

router = APIRouter(tags=["chat"])


@router.post("/api/chat", response_model=None)
def chat(
    data: Optional[Dict[str, Any]] = Depends(json_body_strict),
    request_id: Optional[str] = Depends(get_request_id),
    container: Container = Depends(get_container),
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
        orch = OrchestrationService(
            session=db,
            registry=container.agent_registry,
            handler_factory=container.handler_factory,
            llm_factory=container.llm_factory,
            mitra_sessions=container.mitra_sessions,
            mitra_clients=container.mitra_clients,
            settings=container.settings,
        )

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
            "session": session_payload,
        })
    except ConcurrentTurnError:
        # A double-submit. 409 and NOT an automatic retry: re-sending is what
        # merges two user messages into one in Mitra and destroys an answer.
        return error_response(
            "A reply is already on its way. Please wait for it before sending again.",
            "CONCURRENT_TURN", 409,
        )
    except TurnLimitExceeded as e:
        return error_response(e.detail, "RATE_LIMITED", 429)
    except MitraError as e:
        return mitra_error_response(e)
    except SaarthiError as e:
        # Every mapped domain failure, including the LLM ones, in one clause.
        # Each carries its own status, code and client-safe message
        # (app/exceptions/domain.py, app/llm/exceptions.py), so a new domain
        # exception needs no edit here.
        #
        # This replaces a runtime `from app.services.router_service import
        # AgentNotFound` INSIDE the blanket except below, followed by an
        # isinstance check -- which meant every other domain failure, an LLM
        # rate limit included, fell through to an indistinguishable 500.
        return error_response(e.public_message, e.error_code, e.status_code)
    except Exception as e:  # noqa: BLE001 -- mirrors the original blanket catch
        # exc_info so the traceback lands IN the JSON record rather than on
        # stderr where nothing can correlate it. The fields are what make this
        # answerable without a redeploy: which conversation, whose tenant, and
        # which agent was selected when it broke.
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
        # This route has no exception handler of its own, so a malformed id used
        # to escape as a bare 500.
        return error_response("conversation_id must be a UUID", "INVALID_REQUEST", 400)

    svc = ConversationService(db)
    # 1. Find the conversation being left, WITHOUT creating one. resolve() is
    #    get_or_create: on a first-ever reset it would materialise an empty
    #    conversation purely to abandon nothing, and step 2 then creates a
    #    second one -- two empty rows per reset, and the stray one competes to
    #    be "most recent active" on the next turn.
    conv = svc.find_current(req_conv_id, user)

    # Abandon any open session and close its Mitra channel BEFORE moving on --
    # otherwise a reset mid-interview orphans the socket and the story is
    # never finalized (design doc §10.2).
    if conv is not None:
        abandoned = SessionService(db).abandon(conv.id, reason="reset", actor=user.user_id)
        if abandoned is not None and container.mitra_sessions is not None:
            container.mitra_sessions.close(conv.id)

    # 2. Start a fresh conversation, LEAVING THE PREVIOUS ONE IN HISTORY.
    #    start_new() reuses an already-empty conversation when the user has one,
    #    so repeated resets don't accumulate dead rows.
    new_conv = svc.start_new(user)

    # 3. Return the new id.
    return json_response({
        "status": "success",
        "conversation_id": str(new_conv.id),
    })
