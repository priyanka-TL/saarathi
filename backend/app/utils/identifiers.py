"""Identifier parsing. Framework-free, deliberately.

`parse_uuid` used to live in `app/utils/responses.py` next to `json_response`,
which imports `fastapi.responses`. That was fine while only routers called it --
but the moment a service needed it, importing it dragged FastAPI into the
framework-agnostic core and broke the `core_is_framework_free` import-linter
contract. The function itself has nothing to do with HTTP; only its neighbour
did.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional


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
