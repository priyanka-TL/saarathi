"""Request-id ingress/egress, as pure ASGI middleware.

Replaces Flask's `before_request` (read/generate the id onto `g`) and
`after_request` (echo it back on the response) pair.

WHY PURE ASGI AND NOT `BaseHTTPMiddleware`
------------------------------------------
`BaseHTTPMiddleware` runs the downstream app in a *separate anyio task*, and a
ContextVar set before `call_next` is therefore invisible inside the endpoint.
`app.core.logger.RequestIDFilter` reads exactly that ContextVar, so using
`BaseHTTPMiddleware` here would silently put `"request_id": null` on every log
line in the process. Verified before this file was written.

A plain ASGI class sets the value in the same task the endpoint is dispatched
from, and `anyio.to_thread.run_sync` (how every `def` endpoint runs) copies the
context into the worker thread.

Both channels are populated on purpose:
  * `scope["state"]["request_id"]` -- for `Depends(get_request_id)`, which the
    routers hand to `TurnInput` and the error envelope.
  * the ContextVar -- for the logging filter, which is handed nothing.
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
