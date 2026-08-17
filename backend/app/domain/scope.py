"""Tenant/organization scoping.

Responsible for: the DEFAULT_SCOPE sentinel and the two ways to derive a scope.
Used by: repositories, services, routers and the ORM column defaults.

Pure, so every layer including app.domain itself can import it.
"""
from __future__ import annotations

from typing import Any, Optional, Tuple

#: Matches migration 0006. The sentinel meaning "applies to everyone".
#:
#: A row at (DEFAULT_SCOPE, DEFAULT_SCOPE) is visible to every tenant that has
#: not shadowed that key, which is how configuration ships to all tenants at
#: once. It is a real string in a NOT NULL column rather than a NULL, so the
#: unique indexes and the most-specific-wins ORDER BY both work without
#: NULL-handling special cases.
DEFAULT_SCOPE = "default"


def scope_or_default(
    tenant_id: Optional[str], organization_id: Optional[str]
) -> Tuple[str, str]:
    """Normalise an explicitly-supplied scope pair.

    For callers that receive scope as input -- notably the admin routes, where
    scope is an explicit query parameter and is deliberately NOT inferred from
    the calling admin's own token.

    An empty string is treated as absent, not as a tenant named "".
    """
    return (tenant_id or DEFAULT_SCOPE), (organization_id or DEFAULT_SCOPE)


def scope_for_user(user: Optional[Any]) -> Tuple[str, str]:
    """The (tenant_id, organization_id) a request should be resolved against.

    Accepts `None` and returns the default scope for it. An anonymous caller
    resolves to the same place an unknown tenant does: there is no tenants table
    to validate against (tenants belong to the user service), so an unrecognised
    code simply matches no tenant-specific row and inherits the default. Inert,
    not an error.

    Typed `Any` rather than `UserContext` on purpose. Several call sites reach
    this with duck-typed identity objects, and `getattr` keeps that working --
    a scope lookup is not the right place to start raising `AttributeError`.
    """
    if user is None:
        return DEFAULT_SCOPE, DEFAULT_SCOPE
    return (
        getattr(user, "tenant_code", "") or DEFAULT_SCOPE,
        getattr(user, "active_org_id", None) or DEFAULT_SCOPE,
    )
