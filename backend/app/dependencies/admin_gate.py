"""The admin surface gate.

Port of admin_bp's blueprint-level `before_request`. Two ordering facts are
load-bearing and preserved:

1. `get_current_user` resolves first, so a bad credential is still a 401 before
   the gate is consulted -- Flask's app-wide before_request ran before the
   blueprint's.
2. The feature flag is checked BEFORE the role, so a disabled admin surface
   answers 404 to everyone, admins included. It masquerades as "this route does
   not exist" on purpose.

`user.roles` is active-org scoped (see app/domain/core.py). Never flatten roles
across organisations to make this pass.
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
