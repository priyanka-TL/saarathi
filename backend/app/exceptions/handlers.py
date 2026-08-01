"""App-wide exception handlers.

The routers return their mapped errors directly (see envelope.error_response),
so these handlers only cover the paths that cannot be expressed as a return:
the admin gate's short-circuit, a rejected credential, a malformed request body
that FastAPI itself rejects, and anything genuinely unhandled.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.logger import get_logger
from app.exceptions.envelope import AdminGateError, UnauthorizedError, error_response

logger = get_logger("api.errors")


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AdminGateError)
    def _admin_gate(request: Request, exc: AdminGateError) -> JSONResponse:
        # Bare payload, NOT the standard envelope -- admin has always answered
        # with {"error": CODE} and the tests pin that.
        return JSONResponse(exc.payload, status_code=exc.status_code)

    @app.exception_handler(UnauthorizedError)
    def _unauthorized(request: Request, exc: UnauthorizedError) -> JSONResponse:
        return error_response(str(exc), "UNAUTHORIZED", 401)

    @app.exception_handler(RequestValidationError)
    def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's own 422 {"detail": [...]} would be a new shape no client
        # knows. Flask answered a malformed request with its own 400
        # INVALID_REQUEST envelope, so keep that.
        return error_response("Invalid request", "INVALID_REQUEST", 400)

    @app.exception_handler(Exception)
    def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Reached only for exceptions no router mapped. Starlette's default is
        # a text/plain body; Flask's was HTML. Neither is useful to a JSON
        # client, so this answers with the standard envelope instead -- a
        # documented divergence.
        #
        # This runs inside Starlette's ServerErrorMiddleware, which re-raises
        # after sending, so uvicorn still logs the traceback.
        request_id = request.scope.get("state", {}).get("request_id")
        logger.error("unhandled error on %s %s: %s", request.method, request.url.path, exc)
        headers = {"X-Request-ID": request_id} if request_id else None
        return JSONResponse(
            {
                "status": "error",
                "error": "An internal error occurred.",
                "error_code": "INTERNAL",
                "request_id": request_id,
            },
            status_code=500,
            headers=headers,
        )
