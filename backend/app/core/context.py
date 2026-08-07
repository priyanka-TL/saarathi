"""Ambient per-request state.

Responsible for: carrying the request id without threading it through calls.
Used by: the logging filter, and any layer that needs the id but is not handed one.

Set by RequestIDMiddleware, which MUST stay pure-ASGI: BaseHTTPMiddleware runs
the downstream app in a separate anyio task, so a value set before `call_next`
would be invisible to the endpoint. ContextVar (unlike a thread-local) is copied
into the worker thread by `anyio.to_thread.run_sync`, which is how every
endpoint here runs.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)


def get_request_id() -> Optional[str]:
    """The current request's id, or None outside a request."""
    return request_id_var.get()
