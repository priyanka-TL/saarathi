"""JSON body parsing with Flask's exact semantics.

Deliberately NOT Pydantic body models. Two reasons:

* `POST /api/chat` must answer a missing/!JSON body with its OWN
  `400 INVALID_REQUEST` envelope. A Pydantic model would emit FastAPI's 422
  first and the handler would never run.
* `{"message": ""}` is VALID -- an empty string is an accepted message (pinned
  by tests/characterisation/test_chat_errors.py). The check is
  `"message" in data`, never truthiness, so no model may mark it required-and-
  non-empty.

These are the only `async def` callables in the request path, and correctly so:
reading the request body is genuine async I/O with no blocking work. Every
endpoint that consumes them is still a plain `def`.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from fastapi import Request


async def json_body_strict(request: Request) -> Optional[Dict[str, Any]]:
    """Flask's `request.get_json()`, minus the 415.

    Returns None for a missing, empty, malformed or non-object body; callers
    turn that into their own 400. Flask 3.1 raised a 415 on a wrong
    Content-Type before the view ran, which produced an HTML body no client
    could parse -- collapsing that into the route's own JSON 400 is the one
    behaviour here that is deliberately kinder than the original.
    """
    raw = await request.body()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def json_body_silent(request: Request) -> Dict[str, Any]:
    """Flask's `request.get_json(silent=True) or {}`.

    Tolerant by design: the frontend posts `/api/reset` with no body and no
    Content-Type at all.
    """
    return await json_body_strict(request) or {}


async def raw_body(request: Request) -> bytes:
    """The request body, unparsed.

    For `PUT /api/voice/upload-local/{key}`, the only endpoint that receives
    bytes rather than JSON -- the browser PUTs a recording straight to it under
    the `local` storage provider.

    A dependency rather than `await request.body()` inside the route, because
    the route is a plain `def` (as every route here must be) and so cannot
    await anything. Reading the body is real async I/O with no blocking work,
    which is exactly the exception the two functions above already occupy.
    """
    return await request.body()
