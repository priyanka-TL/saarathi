"""The current request id, as an injectable.

Read off the ASGI scope rather than the ContextVar so that it is explicit at
the call site. The two are always the same value; RequestIDMiddleware sets both.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Request


def get_request_id(request: Request) -> Optional[str]:
    return request.scope.get("state", {}).get("request_id")
