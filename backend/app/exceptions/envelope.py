"""Typed error response envelope, shared by every non-admin router.

Port of src/api/errors.py. Every failure carries `status`, `error`,
`error_code`, and `request_id` -- before this existed, error bodies had no
`status` key at all, so a client could only detect failure by its absence,
never distinguish *which* failure occurred.

NOTE the admin router deliberately does NOT use this. It answers with a bare
`{"error": CODE}` and no envelope; see app/routers/admin.py. Do not unify them
-- the integration tests assert both shapes.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi.responses import JSONResponse

from app.core.context import request_id_var
from app.integrations.mitra.exceptions import (
    MitraConcurrentTurnError,
    MitraError,
    MitraTurnTimeout,
)


def error_response(message: str, code: str, status: int) -> JSONResponse:
    """The standard error body.

    `request_id` is read ambiently from the ContextVar rather than passed in,
    mirroring Flask's `getattr(g, "request_id", None)` -- including its None
    fallback when there is no request context.

    Returns a Response rather than raising, and callers must `return` it. A
    raised exception would propagate through the `get_db` yield-dependency and
    trigger a rollback, where Flask's teardown committed on every *returned*
    error tuple. See app/dependencies/db.py.
    """
    return JSONResponse(
        {
            "status": "error",
            "error": message,
            "error_code": code,
            "request_id": request_id_var.get(),
        },
        status_code=status,
    )


def mitra_error_response(exc: MitraError) -> JSONResponse:
    """504 UPSTREAM_TIMEOUT for a turn timeout -- the session survives, a
    retry is safe. 429 CONCURRENT_TURN_REJECTED for duplicate requests.
    Every other Mitra-side failure collapses to 502 UPSTREAM_UNAVAILABLE."""
    if isinstance(exc, MitraTurnTimeout):
        return error_response(str(exc), "UPSTREAM_TIMEOUT", 504)
    if isinstance(exc, MitraConcurrentTurnError):
        return error_response(str(exc), "CONCURRENT_TURN_REJECTED", 429)
    return error_response(str(exc), "UPSTREAM_UNAVAILABLE", 502)


class AdminGateError(Exception):
    """Raised by the admin gate dependency; carries its own bare payload.

    Raising (rather than returning) is correct here specifically because the
    gate short-circuits before the request has done any DB work, exactly as
    Flask's blueprint-level `before_request` did -- so the rollback the raise
    triggers is a no-op either way.
    """

    def __init__(self, payload: Dict[str, Any], status_code: int) -> None:
        super().__init__(payload.get("error", "admin gate"))
        self.payload = payload
        self.status_code = status_code


class UnauthorizedError(Exception):
    """Raised when the Authenticator cannot produce an identity.

    Flask let werkzeug.exceptions.Unauthorized escape the before_request hook,
    which produced an HTML 401. This produces the JSON envelope instead -- a
    documented divergence, and unreachable in practice: the Authenticator
    resolves identity once at startup and ignores the request entirely, so a
    bad token aborts the boot rather than reaching a client.
    """
