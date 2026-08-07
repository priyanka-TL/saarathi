"""Response helpers.

Every router answers with `JSONResponse` and declares `response_model=None`.
FastAPI skips serialization and validation entirely for a `Response` instance,
which is the only way to preserve response shapes that a `response_model`
cannot express:

* bare JSON arrays (`GET /api/agents`, `.../config/versions`, `GET /api/tools`)
* the synthetic first agent entry, which has NO `key` field at all
* `/api/sessions/{id}/resume`'s three mutually incompatible key sets
* `/api/sessions/{id}/report`'s 200-vs-202 bodies, the 202 carrying no envelope
* `messages[].options`, passed through as the raw stored JSONB list rather than
  re-projected the way /api/chat does it

COROLLARY: nothing non-JSON-native may reach these helpers. `JSONResponse`
renders with `json.dumps` and does NOT run `jsonable_encoder`, so a stray UUID
or datetime is a 500, not a coercion. The routers `str()` every UUID and
`.isoformat()` every datetime for exactly this reason -- keep it that way.
"""
from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse


def json_response(content: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=content, status_code=status_code)


#: Re-exported so the existing `from app.utils.responses import json_response,
#: parse_uuid` call sites keep working. It is DECLARED in utils/identifiers.py,
#: which imports no web framework -- this module does, and a service that needed
#: parse_uuid was therefore dragging FastAPI into the framework-agnostic core.
from app.utils.identifiers import parse_uuid  # noqa: E402,F401  (re-exported)
