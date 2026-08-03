"""Capability administration: the CRUD that makes the sidebar configurable.

This is what "add a capability without a deployment" actually means -- every
route here writes rows that `GET /api/ui/capabilities` reads on the next
request, with no restart and no rebuild.

CONVENTIONS INHERITED FROM app/routers/admin.py, deliberately:

1. **The bare error envelope.** `{"error": CODE}` with optional `path`/`msg` --
   no `status`, no `error_code`, no `request_id`. The standard envelope
   (app/exceptions/envelope.py) is NOT used on the admin surface; the
   integration tests assert both shapes and they must not be unified.

2. **These handlers commit their own session.** Elsewhere `get_db` owns the
   commit. Here a write must be visible to the read that follows it in the same
   request, so each mutation commits explicitly; the dependency's later commit
   is then a harmless no-op.

3. **Raw `text()` rather than the ORM**, matching admin.py. The ORM models
   exist (migration 0006 needed them for the FK), but staying on raw SQL keeps
   this file consistent with the rest of the admin surface and keeps the
   scope-precedence queries readable as SQL.

SCOPE IS EXPLICIT ON EVERY ROUTE, never inferred from the calling admin's own
token. An admin editing another tenant's configuration is the normal case, and
silently defaulting to the caller's tenant would make that impossible to
express -- and would make a mistake look like success.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.container import Container
from app.dependencies.admin_gate import require_admin
from app.dependencies.body import json_body_strict
from app.dependencies.container import get_container
from app.dependencies.db import get_db
from app.dependencies.identity import get_current_user
from app.domain.core import UserContext
from app.repositories.audit import AuditLogRepository
from app.utils.responses import json_response

router = APIRouter(tags=["admin"], dependencies=[Depends(require_admin)])

DEFAULT_SCOPE = "default"

_VALID_STATUSES = {"active", "disabled", "coming_soon"}

#: Fields a caller may set on a capability. Anything else in the body is
#: rejected rather than ignored -- a typo'd key that silently did nothing would
#: look exactly like a successful edit.
_CAPABILITY_FIELDS = {
    "name", "description", "icon", "badge", "status", "display_order", "metadata",
}


def _admin_error(code: str, status: int, **extra: Any) -> JSONResponse:
    return json_response({"error": code, **extra}, status_code=status)


def _scope(tenant_id: Optional[str], organization_id: Optional[str]) -> tuple[str, str]:
    return (tenant_id or DEFAULT_SCOPE), (organization_id or DEFAULT_SCOPE)


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
    rows = db.execute(
        text("""
            SELECT id, tenant_id, organization_id, key, name, description, icon, badge,
                   status, display_order, metadata
            FROM capabilities
            WHERE tenant_id = :tenant AND organization_id = :organization
            ORDER BY display_order, key
        """),
        {"tenant": tenant, "organization": organization},
    ).fetchall()

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

    exists = db.execute(
        text("""
            SELECT 1 FROM capabilities
            WHERE key = :key AND tenant_id = :tenant AND organization_id = :organization
        """),
        {"key": key, "tenant": tenant, "organization": organization},
    ).fetchone()
    if exists:
        return _admin_error("CAPABILITY_EXISTS", 409)

    try:
        row = db.execute(
            text("""
                INSERT INTO capabilities (tenant_id, organization_id, key, name, description,
                                          icon, badge, status, display_order, metadata,
                                          created_by, updated_by)
                VALUES (:tenant, :organization, :key, :name, :description, :icon, :badge,
                        CAST(:status AS capability_status_enum), :display_order,
                        CAST(:metadata AS jsonb), :actor, :actor)
                RETURNING id, tenant_id, organization_id, key, name, description, icon,
                          badge, status, display_order, metadata
            """),
            {
                "tenant": tenant, "organization": organization, "key": key, "name": name,
                "description": body.get("description"), "icon": body.get("icon"),
                "badge": body.get("badge"), "status": status,
                "display_order": body.get("display_order", 100),
                "metadata": json.dumps(body.get("metadata") or {}),
                "actor": user.user_id,
            },
        ).fetchone()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        # ck_capabilities_key_slug is the likely cause; surfacing the DB text
        # would leak the constraint name into an API response.
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
    before = db.execute(
        text("""
            SELECT id, tenant_id, organization_id, key, name, description, icon, badge,
                   status, display_order, metadata
            FROM capabilities
            WHERE key = :key AND tenant_id = :tenant AND organization_id = :organization
        """),
        {"key": key, "tenant": tenant, "organization": organization},
    ).fetchone()
    if not before:
        return _admin_error("CAPABILITY_NOT_FOUND", 404)

    # `actor` is bound unconditionally: the UPDATE below always sets updated_by,
    # whether or not the caller asked to change any of _CAPABILITY_FIELDS.
    sets, params = [], {"id": before.id, "actor": user.user_id}
    for field in _CAPABILITY_FIELDS:
        if field not in body:
            continue
        if field == "status":
            sets.append("status = CAST(:status AS capability_status_enum)")
            params["status"] = body["status"]
        elif field == "metadata":
            sets.append("metadata = CAST(:metadata AS jsonb)")
            params["metadata"] = json.dumps(body["metadata"] or {})
        else:
            sets.append(f"{field} = :{field}")
            params[field] = body[field]

    if not sets:
        return json_response(_serialize(before))

    row = db.execute(
        text(f"""
            UPDATE capabilities SET {", ".join(sets)},
                                    updated_by = :actor, updated_at = now()
            WHERE id = :id
            RETURNING id, tenant_id, organization_id, key, name, description, icon,
                      badge, status, display_order, metadata
        """),
        params,
    ).fetchone()

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
    before = db.execute(
        text("""
            SELECT id, tenant_id, organization_id, key, name, description, icon, badge,
                   status, display_order, metadata
            FROM capabilities
            WHERE key = :key AND tenant_id = :tenant AND organization_id = :organization
        """),
        {"key": key, "tenant": tenant, "organization": organization},
    ).fetchone()
    if not before:
        return _admin_error("CAPABILITY_NOT_FOUND", 404)

    db.execute(text("DELETE FROM capabilities WHERE id = :id"), {"id": before.id})
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
    capability = db.execute(
        text("""
            SELECT id FROM capabilities
            WHERE key = :key AND tenant_id = :tenant AND organization_id = :organization
        """),
        {"key": key, "tenant": tenant, "organization": organization},
    ).fetchone()
    if not capability:
        return _admin_error("CAPABILITY_NOT_FOUND", 404)

    members = body["agents"]
    resolved = []
    for index, member in enumerate(members):
        if not isinstance(member, dict) or not member.get("agent_key"):
            return _admin_error("MEMBERSHIP_INVALID", 422,
                                path=["agents", index, "agent_key"], msg="agent_key is required")
        agent = db.execute(
            text("SELECT id FROM agents WHERE key = :key"), {"key": member["agent_key"]}
        ).fetchone()
        if not agent:
            return _admin_error("AGENT_NOT_FOUND", 422,
                                path=["agents", index, "agent_key"],
                                msg=f"unknown agent {member['agent_key']!r}")
        resolved.append((agent.id, member, index))

    db.execute(
        text("DELETE FROM capability_agents WHERE capability_id = :id"),
        {"id": capability.id},
    )
    for agent_id, member, index in resolved:
        db.execute(
            text("""
                INSERT INTO capability_agents (capability_id, agent_id, display_order,
                                               label_override, is_visible, metadata,
                                               created_by, updated_by)
                VALUES (:capability_id, :agent_id, :display_order, :label, :is_visible,
                        CAST(:metadata AS jsonb), :actor, :actor)
            """),
            {
                "capability_id": capability.id,
                "agent_id": agent_id,
                # Falls back to position, so a caller that sends an ordered
                # list without explicit orders still gets that order.
                "display_order": member.get("display_order", (index + 1) * 10),
                "label": member.get("label_override"),
                "is_visible": member.get("is_visible", True),
                "metadata": json.dumps(member.get("metadata") or {}),
                "actor": user.user_id,
            },
        )

    AuditLogRepository(db).insert(
        action="capability_agent_set", entity_type="capability", entity_id=capability.id,
        actor=user.user_id, after={"agents": members},
        note=f"membership replaced ({len(members)} agent(s))",
    )
    db.commit()
    return json_response({"key": key, "tenant_id": tenant, "organization_id": organization,
                          "agents": members})
