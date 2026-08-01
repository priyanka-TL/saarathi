"""Caller identity, injected once per request.

Port of the `g.user = provider.get_user(request)` half of Flask's
before_request. Ordering matters and is preserved: this dependency is declared
before `get_db` on every route, so a rejected credential never opens a DB
session -- exactly as Flask's hook ordering guaranteed.

There are no cookies and no server-side session. The Authenticator resolves the
identity once at startup -- from the env token when AUTH_CHECK is true, from
its hardcoded defaults when it is false (see app/services/identity.py) -- so
the request object is never inspected and every request resolves to the same
UserContext.
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
