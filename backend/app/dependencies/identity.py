"""Caller identity, resolved per request from the Authorization header.

Responsible for: producing the UserContext every route is handed.
Used by: every authenticated route; declared BEFORE get_db, so a rejected
credential never opens a DB session.

The frontend logs in directly against ELEVATE's user service (never through
Saarthi) and sends the resulting JWT as `Authorization: Bearer <token>`. That
token is decoded WITH signature verification (app/services/identity.py) --
this app is no longer the sole, implicitly-trusted party in the loop. A
request with no such header (and AUTH_CHECK=true) is UNAUTHORIZED -- there is
deliberately no fallback identity for a missing token; one existed briefly
and was removed for being exactly that, an authentication bypass. The
AUTH_CHECK=false dev identity is a separate, explicit switch, not a per-request
fallback. Admin routes are unaffected: they take tenant/organization as
explicit parameters rather than deriving them from the caller.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Request

from app.domain.core import UserContext
from app.exceptions.envelope import UnauthorizedError


def _bearer_token(request: Request) -> Optional[str]:
    """The Authorization header's token, or None for anything else.

    A missing header, a non-bearer scheme, or an empty token all mean the
    same thing here: no token was supplied, which `authenticate()` itself
    turns into a 401 when AUTH_CHECK=true (or is ignored entirely when it's
    false). A token that IS present but invalid is a different matter -- that
    also fails inside `authenticate()`, just with a different reason.
    """
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def get_current_user(request: Request) -> UserContext:
    authenticator = request.app.state.authenticator
    if authenticator is None:
        raise UnauthorizedError("No authenticator configured")
    try:
        return authenticator.authenticate(_bearer_token(request))
    except Exception as exc:  # noqa: BLE001 -- mirrors Flask's blanket catch
        raise UnauthorizedError(str(exc)) from exc
