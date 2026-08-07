"""Capability administration -- the CRUD that makes the sidebar configurable.

Responsible for: validating capability and membership writes, then shaping the
response. The SQL lives in app/repositories/capabilities.py.
Used by: the admin console. A write here changes GET /api/ui/capabilities on the
next request, with no restart.

Three conventions inherited from admin.py, all deliberate:

1. The bare {"error": CODE} envelope, not the standard one. Tests assert both
   shapes and they must not be unified.
2. These handlers commit their own session, so a write is visible to the read
   that follows it in the same request.
3. SCOPE IS EXPLICIT ON EVERY ROUTE, never inferred from the calling admin's
   token -- editing another tenant's configuration is the normal case, and
   defaulting to the caller's tenant would make a mistake look like success.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.container import Container
from app.dependencies.admin_gate import require_admin
from app.dependencies.body import json_body_strict
from app.dependencies.container import get_container
from app.dependencies.db import get_db
from app.dependencies.identity import get_current_user
from app.domain.core import UserContext
from app.domain.scope import scope_or_default
from app.exceptions.admin_envelope import admin_error
from app.repositories.agents import AgentRepository
from app.repositories.audit import AuditLogRepository
from app.repositories.capabilities import CapabilityRepository
from app.utils.responses import json_response

router = APIRouter(tags=["admin"], dependencies=[Depends(require_admin)])

_VALID_STATUSES = {"active", "disabled", "coming_soon"}

#: Fields a caller may set on a capability. Anything else in the body is
#: rejected rather than ignored -- a typo'd key that silently did nothing would
#: look exactly like a successful edit.
_CAPABILITY_FIELDS = {
    "name", "description", "icon", "badge", "status", "display_order", "metadata",
}


#: The bare admin envelope -- see app/exceptions/admin_envelope.py. Aliased
#: rather than imported under its own name to keep the call sites below short.
_admin_error = admin_error


def _scope(tenant_id: Optional[str], organization_id: Optional[str]) -> tuple[str, str]:
    """Normalise the EXPLICIT scope query params. See domain/scope.py."""
    return scope_or_default(tenant_id, organization_id)


def _serialize(row) -> Dict[str, Any]:
    return {
        "id": str(row.id),
        "tenant_id": row.tenant_id,
        "organization_id": row.organization_id,
        "key": row.key,
        "name": row.name,
        "description": row.description,
        "icon": row.icon,
        "badge": row.badge,
        "status": row.status,
        "display_order": row.display_order,
        "metadata": row.metadata,
    }


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


@router.get("/api/admin/capabilities", response_model=None)
def list_capabilities(
    tenant_id: Optional[str] = Query(None),
    organization_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Every capability row in one scope, INCLUDING disabled ones.

    Deliberately not the resolved view: an admin needs to see what exists in
    this scope in order to edit it, where /api/ui/capabilities shows what a
    user would see after inheritance and filtering. Confusing the two would
    make a disabled capability look deleted.
    """
    tenant, organization = _scope(tenant_id, organization_id)
    rows = CapabilityRepository(db).list_in_scope(tenant, organization)

    return json_response({"capabilities": [_serialize(r) for r in rows]})


@router.post("/api/admin/capabilities", response_model=None)
def create_capability(
    body: Optional[Dict[str, Any]] = Depends(json_body_strict),
    container: Container = Depends(get_container),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Create a capability in an explicit scope.

    A capability created at ('default', 'default') is visible to EVERY tenant
    that has not shadowed that key -- which is how a capability ships to
    everyone in one insert.
    """
    if not body:
        return _admin_error("CAPABILITY_INVALID", 422, path=[], msg="empty body")

    key = body.get("key")
    if not key or not isinstance(key, str):
        return _admin_error("CAPABILITY_INVALID", 422, path=["key"], msg="key is required")

    name = body.get("name")
    if not name or not isinstance(name, str):
        return _admin_error("CAPABILITY_INVALID", 422, path=["name"], msg="name is required")

    status = body.get("status", "active")
    if status not in _VALID_STATUSES:
        return _admin_error("CAPABILITY_INVALID", 422, path=["status"],
                            msg=f"status must be one of {sorted(_VALID_STATUSES)}")

    tenant, organization = _scope(body.get("tenant_id"), body.get("organization_id"))

    capabilities = CapabilityRepository(db)
    if capabilities.exists(key, tenant, organization):
        return _admin_error("CAPABILITY_EXISTS", 409)

    try:
        row = capabilities.insert(
            key=key, name=name, tenant=tenant, organization=organization,
            actor=user.user_id,
            description=body.get("description"), icon=body.get("icon"),
            badge=body.get("badge"), status=status,
            display_order=body.get("display_order", 100),
            metadata=body.get("metadata"),
        )
    except IntegrityError:
        db.rollback()
        # ck_capabilities_key_slug is the only integrity rule this INSERT can
        # break that is the CALLER's fault -- the scope collision was already
        # ruled out by exists() above. Surfacing the DB text would leak the
        # constraint name into an API response.
        #
        # Narrowed from `except Exception`, which reported ANY database failure
        # -- a dropped connection, a permissions error -- as a 422 complaining
        # about the key format.
        return _admin_error("CAPABILITY_INVALID", 422, path=["key"],
                            msg="key must match ^[a-z][a-z0-9_]{1,62}$")

    AuditLogRepository(db).insert(
        action="capability_create", entity_type="capability", entity_id=row.id,
        actor=user.user_id, after=_serialize(row),
    )
    db.commit()
    return json_response(_serialize(row))


@router.patch("/api/admin/capabilities/{key}", response_model=None)
def update_capability(
    key: str,
    body: Optional[Dict[str, Any]] = Depends(json_body_strict),
    tenant_id: Optional[str] = Query(None),
    organization_id: Optional[str] = Query(None),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Partial update, within one scope.

    Editing the ('default','default') row changes what every uncustomised
    tenant sees. Editing a tenant's own row changes only that tenant. The
    scope query parameters are what choose between those two, and they are
    never inferred.
    """
    if not body:
        return _admin_error("CAPABILITY_INVALID", 422, path=[], msg="empty body")

    unknown = set(body) - _CAPABILITY_FIELDS
    if unknown:
        # Rejected, not ignored: a silently dropped field looks like a
        # successful edit and is discovered much later, in the UI.
        return _admin_error("CAPABILITY_INVALID", 422, path=sorted(unknown),
                            msg="unknown field(s)")

    if "status" in body and body["status"] not in _VALID_STATUSES:
        return _admin_error("CAPABILITY_INVALID", 422, path=["status"],
                            msg=f"status must be one of {sorted(_VALID_STATUSES)}")

    tenant, organization = _scope(tenant_id, organization_id)
    capabilities = CapabilityRepository(db)
    before = capabilities.find_by_key(key, tenant, organization)
    if not before:
        return _admin_error("CAPABILITY_NOT_FOUND", 404)

    # Only the allowed fields reach the repository. The check above already
    # rejected anything outside _CAPABILITY_FIELDS, and update_fields
    # interpolates these keys into its SET list -- so this intersection is what
    # keeps that safe, not a second guard down there.
    changes = {f: body[f] for f in _CAPABILITY_FIELDS if f in body}

    row = capabilities.update_fields(before.id, changes, actor=user.user_id)
    if row is None:
        # Nothing to set: answer with the unchanged row rather than issue an
        # UPDATE whose only effect would be to bump updated_at.
        return json_response(_serialize(before))

    AuditLogRepository(db).insert(
        action="capability_update", entity_type="capability", entity_id=row.id,
        actor=user.user_id, before=_serialize(before), after=_serialize(row),
    )
    db.commit()
    return json_response(_serialize(row))


@router.delete("/api/admin/capabilities/{key}", response_model=None)
def delete_capability(
    key: str,
    tenant_id: Optional[str] = Query(None),
    organization_id: Optional[str] = Query(None),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Delete one scope's row.

    THE MEANING DEPENDS ON THE SCOPE, and it is worth being explicit:

      * deleting a TENANT's row reverts that tenant to the default -- this is
        the "undo my customisation" operation;
      * deleting the DEFAULT row removes the capability for everyone who has
        not shadowed it.

    Membership rows cascade (fk ... ON DELETE CASCADE); the agents themselves
    are untouched.
    """
    tenant, organization = _scope(tenant_id, organization_id)
    capabilities = CapabilityRepository(db)
    before = capabilities.find_by_key(key, tenant, organization)
    if not before:
        return _admin_error("CAPABILITY_NOT_FOUND", 404)

    capabilities.delete(before.id)
    AuditLogRepository(db).insert(
        action="capability_delete", entity_type="capability", entity_id=before.id,
        actor=user.user_id, before=_serialize(before),
    )
    db.commit()
    return json_response({"deleted": True, "key": key,
                          "tenant_id": tenant, "organization_id": organization})


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------


@router.put("/api/admin/capabilities/{key}/agents", response_model=None)
def set_capability_agents(
    key: str,
    body: Optional[Dict[str, Any]] = Depends(json_body_strict),
    tenant_id: Optional[str] = Query(None),
    organization_id: Optional[str] = Query(None),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Replace a capability's membership wholesale.

    PUT, not PATCH, and that is the interesting decision: membership is an
    ORDERED LIST, and incremental add/remove/reorder operations on an ordered
    list need either a diff protocol or a sequence of calls that can half-fail.
    Sending the whole list makes the result a function of one request.

    EVERY agent_key IS VALIDATED BEFORE ANYTHING IS WRITTEN. An unknown key is
    a 422 naming it, not a partial write -- a dangling reference would render a
    button that fails at click time, which the FK would reject anyway, but with
    a 500 instead of an explanation.
    """
    if not body or not isinstance(body.get("agents"), list):
        return _admin_error("MEMBERSHIP_INVALID", 422, path=["agents"],
                            msg="agents must be a list")

    tenant, organization = _scope(tenant_id, organization_id)
    capabilities = CapabilityRepository(db)
    capability = capabilities.find_id_by_key(key, tenant, organization)
    if not capability:
        return _admin_error("CAPABILITY_NOT_FOUND", 404)

    members = body["agents"]

    # RESOLVE AND VALIDATE EVERYTHING BEFORE WRITING ANYTHING. The repository's
    # replace_members deletes the existing rows first, so a key that turned out
    # to be unknown halfway through would leave the capability with a truncated
    # membership and still answer 422.
    agents = AgentRepository(db)
    resolved = []
    for index, member in enumerate(members):
        if not isinstance(member, dict) or not member.get("agent_key"):
            return _admin_error("MEMBERSHIP_INVALID", 422,
                                path=["agents", index, "agent_key"], msg="agent_key is required")
        agent = agents.find_id_by_key(member["agent_key"])
        if not agent:
            return _admin_error("AGENT_NOT_FOUND", 422,
                                path=["agents", index, "agent_key"],
                                msg=f"unknown agent {member['agent_key']!r}")
        resolved.append((agent.id, member, index))

    capabilities.replace_members(capability.id, resolved, actor=user.user_id)

    AuditLogRepository(db).insert(
        action="capability_agent_set", entity_type="capability", entity_id=capability.id,
        actor=user.user_id, after={"agents": members},
        note=f"membership replaced ({len(members)} agent(s))",
    )
    db.commit()
    return json_response({"key": key, "tenant_id": tenant, "organization_id": organization,
                          "agents": members})
