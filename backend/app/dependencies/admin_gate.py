"""The admin surface gate.

Responsible for: refusing non-admins, and hiding the surface entirely when the
feature flag is off.
Used by: every route in the admin routers, as a router-level dependency.

Two orderings are load-bearing: get_current_user resolves first, so a bad
credential is a 401 before the gate is consulted; and the feature flag is
checked BEFORE the role, so a disabled admin surface answers 404 to everyone,
admins included.

`user.roles` is active-org scoped -- never flatten roles across organisations to
make this pass.
"""
from __future__ import annotations

from fastapi import Depends

from app.core.container import Container
from app.dependencies.container import get_container
from app.dependencies.identity import get_current_user
from app.domain.core import UserContext
from app.exceptions.envelope import AdminGateError


def require_admin(
    container: Container = Depends(get_container),
    user: UserContext = Depends(get_current_user),
) -> None:
    if not container.settings.saarthi_admin_enabled:
        raise AdminGateError({"error": "Not Found"}, 404)
    if "admin" not in getattr(user, "roles", []):
        raise AdminGateError({"error": "Forbidden"}, 403)
