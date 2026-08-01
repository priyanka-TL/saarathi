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

import uuid
from typing import Any, Optional

from fastapi.responses import JSONResponse


def json_response(content: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=content, status_code=status_code)


def parse_uuid(raw: Any) -> Optional[uuid.UUID]:
    """Lenient UUID parse, returning None instead of raising.

    Path parameters are declared `str` and parsed with this rather than typed
    as `uuid.UUID`, because FastAPI would answer a malformed segment with a 422
    and its own `{"detail": [...]}` body. Flask's `<uuid:...>` converter simply
    failed to match, so the URL did not exist and the answer was a 404 -- and
    the frontend treats 404 on a conversation as "forget this conversation"
    (see loadConversationHistory). Returning None lets each route answer with
    its own 404 envelope.
    """
    if raw is None:
        return None
    try:
        return uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None
