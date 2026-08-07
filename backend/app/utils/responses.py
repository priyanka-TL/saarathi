"""JSONResponse helpers.

Responsible for: building every response body the API returns.
Used by: every router.

Routes declare `response_model=None` and return `JSONResponse` directly, because
`response_model` cannot express several of the pinned shapes (bare arrays, the
keyless synthetic agent entry, /resume's three incompatible key sets).

COROLLARY: nothing non-JSON-native may reach here. `json.dumps` runs but
`jsonable_encoder` does not, so a stray UUID or datetime is a 500 rather than a
coercion -- callers must `str()` UUIDs and `.isoformat()` datetimes.
"""
from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse


def json_response(content: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=content, status_code=status_code)


# Re-exported for existing call sites. Declared in utils/identifiers.py, which
# imports no web framework -- a service importing it from here would drag
# FastAPI into the framework-agnostic core.
from app.utils.identifiers import parse_uuid  # noqa: E402,F401  (re-exported)
