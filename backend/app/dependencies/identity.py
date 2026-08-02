"""Caller identity, resolved from configuration -- not from the request.

Port of the `g.user = provider.get_user(request)` half of Flask's
before_request. Ordering matters and is preserved: this dependency is declared
before `get_db` on every route, so a rejected credential never opens a DB
session -- exactly as Flask's hook ordering guaranteed.

THE IDENTITY SOURCE IS `SAARTHI_STATIC_TOKEN` IN `.env`, ALWAYS -- never the
request's `Authorization` header. This is deliberate: the frontend is a bare
SPA with no login flow and nothing that could supply a real per-caller JWT, so
a design that read identity from the request would either 401 every browser
request (nothing to read) or require inventing a token source the frontend
does not have. Reading from configuration is what the whole rest of the app --
turn handling, conversation scoping, agent access -- has always assumed.

See app/services/identity.py for AUTH_CHECK's two branches (decode the env
token vs. the hardcoded development identity); this dependency does not choose
between them, `Authenticator.authenticate()` already has.

Multi-tenancy at the schema and admin-API layer is UNCHANGED by this: an
admin's `/api/admin/capabilities` calls specify `tenant_id`/`organization_id`
explicitly as parameters, not derived from the caller's own identity, so
per-tenant configuration remains fully writable and readable through that
route regardless of how many distinct identities ever reach this dependency.
What is NOT currently possible is an ordinary end-user request resolving to
more than one tenant -- there is no live path that produces that, because
nothing upstream of this dependency can assert who the browser's user is.
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
