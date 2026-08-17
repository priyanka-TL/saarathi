"""The admin error envelope: bare `{"error": CODE}`, and deliberately separate.

THE TWO ENVELOPES MUST NEVER BE UNIFIED. Everything outside `/api/admin`
answers with `{status, error, error_code, request_id}` (see `envelope.py`); the
admin surface answers with a bare `{"error": CODE}` plus optional `path` / `msg`.
Both shapes are asserted independently by the integration tests, and the admin
console parses the bare one.

This module exists only so the constructor is written ONCE. It was previously
defined byte-identically in both `app/routers/admin.py` and
`app/routers/admin_config.py`, feeding roughly thirty-five call sites across the
two -- so "change the admin error shape" meant finding both copies.
"""
from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse

from app.utils.responses import json_response


def admin_error(code: str, status: int, **extra: Any) -> JSONResponse:
    """The admin envelope: bare, no status/error_code/request_id.

    :param code: the stable error code, e.g. ``"CAPABILITY_INVALID"``.
    :param status: HTTP status.
    :param extra: merged into the body -- conventionally ``path`` (which field
        was wrong, as a list) and ``msg`` (why).
    """
    return json_response({"error": code, **extra}, status_code=status)
