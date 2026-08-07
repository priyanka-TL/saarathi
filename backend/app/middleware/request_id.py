"""Request-id ingress/egress, as pure ASGI middleware.

Responsible for: reading or generating X-Request-ID, and echoing it back.
Used by: mounted on the app in create_app(); runs for every HTTP request.

MUST stay pure ASGI. BaseHTTPMiddleware runs the downstream app in a separate
anyio task, so a ContextVar set before `call_next` is invisible inside the
endpoint -- which would silently put "request_id": null on every log line.

Both channels are populated: scope["state"] for Depends(get_request_id), and the
ContextVar for the logging filter, which is handed nothing.
"""
from __future__ import annotations

import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.context import request_id_var

HEADER_NAME = "X-Request-ID"


class RequestIDMiddleware:
    """Reads X-Request-ID or generates a uuid4 hex; echoes it on the response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = Headers(scope=scope).get(HEADER_NAME) or uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        token = request_id_var.set(request_id)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[HEADER_NAME] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            request_id_var.reset(token)
