"""Caller identity, resolved from configuration -- not from the request.

Responsible for: producing the UserContext every route is handed.
Used by: every authenticated route; declared BEFORE get_db, so a rejected
credential never opens a DB session.

The identity source is SAARTHI_STATIC_TOKEN, always -- never the request's
Authorization header. The frontend is a bare SPA with no login flow, so reading
identity from the request would 401 every browser call. Admin routes are
unaffected: they take tenant/organization as explicit parameters rather than
deriving them from the caller.
"""
from __future__ import annotations

from fastapi import Request

from app.domain.core import UserContext
from app.exceptions.envelope import UnauthorizedError


def get_current_user(request: Request) -> UserContext:
    authenticator = request.app.state.authenticator
    if authenticator is None:
        raise UnauthorizedError("No authenticator configured")
    try:
        return authenticator.authenticate()
    except Exception as exc:  # noqa: BLE001 -- mirrors Flask's blanket catch
        raise UnauthorizedError(str(exc)) from exc
