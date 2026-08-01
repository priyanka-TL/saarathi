"""Ambient per-request state, framework-agnostic.

Flask carried the request id on `flask.g`, which is a proxy onto a
request-scoped context that the logging filter could read without being handed
anything. `ContextVar` is the direct equivalent and, crucially, is copied into
the worker thread by `anyio.to_thread.run_sync` -- which is how every endpoint
in this app runs (see app/main.py on why nothing here is `async def`).

Set by `app.middleware.request_id.RequestIDMiddleware`, which MUST be pure-ASGI
middleware: `BaseHTTPMiddleware` runs the downstream app in a separate anyio
task, so a value set before `call_next` would be invisible to the endpoint and
every log line would carry `request_id: null`.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)


def get_request_id() -> Optional[str]:
    """The current request's id, or None outside a request."""
    return request_id_var.get()
