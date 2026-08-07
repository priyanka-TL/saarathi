"""Identifier parsing.

Responsible for: lenient UUID parsing.
Used by: routers and services alike.

Framework-free on purpose, so services can import it without pulling FastAPI
into the framework-agnostic core.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional


def parse_uuid(raw: Any) -> Optional[uuid.UUID]:
    """Parse a UUID, returning None instead of raising.

    Path params are typed `str` and parsed here so a malformed segment gets this
    app's own 404 envelope rather than FastAPI's 422 `{"detail": [...]}`. The
    frontend treats a 404 on a conversation as "forget this conversation".
    """
    if raw is None:
        return None
    try:
        return uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None
