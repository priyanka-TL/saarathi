"""Request-id ingress/egress, as pure ASGI middleware.

Responsible for: reading or generating X-Request-ID, and echoing it back.
Used by: mounted on the app in create_app(); runs for every HTTP request.

MUST stay pure ASGI. BaseHTTPMiddleware runs the downstream app in a separate
anyio task, so a ContextVar set before `call_next` is invisible inside the
endpoint -- which would silently put "request_id": null on every log line.

Both channels are populated: scope["state"] for Depends(get_request_id), and the
ContextVar for the logging filter, which is handed nothing.

It also opens the per-turn timing record (`app.core.timing`). Same ContextVar
rules, same reset-in-`finally`, so the two have to live together: a stage
recorded against a previous request's accumulator would be indistinguishable
from a real measurement, which is the one thing instrumentation must never be.
"""
from __future__ import annotations

import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.context import request_id_var
from app.core.timing import TurnTimings, turn_timings_var

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
        # A FRESH RECORD PER REQUEST. Set here rather than lazily on first use so
        # that a stage recorded outside any turn stays a no-op instead of
        # silently starting an accumulator nothing will ever read or clear.
        timings_token = turn_timings_var.set(TurnTimings())

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[HEADER_NAME] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            turn_timings_var.reset(timings_token)
            request_id_var.reset(token)
