"""The standard error envelope.

Responsible for: the {status, error, error_code, request_id} body every
non-admin failure returns.
Used by: every router, and the exception handlers.

The admin routers deliberately do NOT use this -- they answer with a bare
{"error": CODE}. Do not unify them; tests assert both shapes.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi.responses import JSONResponse

from app.core.context import request_id_var
from app.providers.errors import (
    ProviderConcurrentTurnError,
    ProviderError,
    ProviderTurnTimeout,
)


def error_body(message: str, code: str, request_id: Optional[str]) -> Dict[str, Any]:
    """The standard error body as a plain dict.

    Separate from error_response because the unhandled-exception handler needs
    the same four keys but sources request_id from the request scope rather than
    the ContextVar.
    """
    return {
        "status": "error",
        "error": message,
        "error_code": code,
        "request_id": request_id,
    }


def error_response(message: str, code: str, status: int) -> JSONResponse:
    """The standard error response.

    RETURNS rather than raises, and callers must `return` it: a raise propagates
    through the get_db yield-dependency and rolls back the transaction the error
    response was meant to accompany.
    """
    return JSONResponse(
        error_body(message, code, request_id_var.get()),
        status_code=status,
    )


def upstream_error_response(exc: ProviderError) -> JSONResponse:
    """504 on a turn timeout (the session survives, so a retry is safe), 429 on
    a duplicate request, 502 for everything else upstream.

    The mapping is unchanged from when this took one platform's exception type.
    What changed is that it now takes the SHARED base, so a provider cannot ship
    an exception hierarchy the routers forget to catch -- which is exactly what
    had happened: one platform's errors subclassed plain Exception, were caught
    nowhere, and surfaced as 500 INTERNAL while the identical failure from the
    other platform surfaced as 502.
    """
    if isinstance(exc, ProviderTurnTimeout):
        return error_response(str(exc), "UPSTREAM_TIMEOUT", 504)
    if isinstance(exc, ProviderConcurrentTurnError):
        return error_response(str(exc), "CONCURRENT_TURN_REJECTED", 429)
    return error_response(str(exc), "UPSTREAM_UNAVAILABLE", 502)


class AdminGateError(Exception):
    """Raised by the admin gate; carries its own bare payload.

    Raising is safe here specifically because the gate short-circuits before any
    DB work, so the rollback it triggers is a no-op.
    """

    def __init__(self, payload: Dict[str, Any], status_code: int) -> None:
        super().__init__(payload.get("error", "admin gate"))
        self.payload = payload
        self.status_code = status_code


class UnauthorizedError(Exception):
    """Raised when the Authenticator cannot produce an identity.

    Unreachable in practice: identity resolves once at startup from config, so a
    bad token aborts the boot rather than reaching a client.
    """
