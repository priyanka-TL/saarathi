"""Agent administration: status, config versioning, registry reload, tools.

Port of src/api/admin_routes.py.

TWO THINGS THAT LOOK LIKE BUGS AND ARE NOT
------------------------------------------
1. **A different error envelope.** These routes answer with a bare
   `{"error": CODE}` (plus optional `path`/`msg`) -- no `status`, no
   `error_code`, no `request_id`. The standard envelope lives in
   app/exceptions/envelope.py and is deliberately NOT imported here.
   tests/integration/test_config_versioning_lifecycle.py pins this shape.

2. **These handlers commit their own session.** Everywhere else the
   `get_db` dependency owns the commit. Here each write commits before calling
   `agent_registry.reload(db)`, because the reload must observe the row it just
   wrote. The dependency's later commit is then a harmless no-op.

Raw `sqlalchemy.text()` is still how `agents` and `agent_configs` are queried --
they have no ORM model, see app/models/orm.py -- but that SQL now lives in
`app/repositories/agents.py` rather than in this module. The statements moved
verbatim; what changed is only where they live. In particular the
deactivate-before-insert ordering that `uq_agent_configs_one_active` requires is
now inside `AgentConfigRepository`, expressed once, instead of being spelled out
at each of the two call sites that activate a version.

This module routes, validates, authorises and shapes responses. It owns the
transaction boundary (point 2 above) but issues no statements of its own.
"""
from __future__ import annotations


from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from app.core.container import Container
from app.dependencies.admin_gate import require_admin
from app.dependencies.body import json_body_silent, json_body_strict
from app.dependencies.container import get_container
from app.dependencies.db import get_db
from app.dependencies.identity import get_current_user
from app.domain.agent_spec import AgentSpec, canonical_json
from app.domain.core import UserContext
from app.repositories.agents import AgentConfigRepository, AgentRepository
from app.repositories.audit import AuditLogRepository
from app.services.agent_config_validation import redact_secrets, remote_config_problem
from app.exceptions.admin_envelope import admin_error
from app.utils.responses import json_response

# Every route in this router is gated. The dependency raises AdminGateError,
# handled in app/exceptions/handlers.py.
router = APIRouter(tags=["admin"], dependencies=[Depends(require_admin)])


#: The bare admin envelope. Defined once in app/exceptions/admin_envelope.py;
#: aliased here because this module has ~20 call sites reading `_admin_error`.
_admin_error = admin_error


# --------------------------------------------------------------------------
# ORDER IS LOAD-BEARING. Flask/Werkzeug ranked routes by specificity, so the
# static `/api/agents/reload` always beat `/api/agents/<key>`. FastAPI matches
# in registration order, so `reload` MUST be declared before `{key}` or it is
# swallowed and interpreted as an agent named "reload".
# --------------------------------------------------------------------------


@router.post("/api/agents/reload", response_model=None)
def reload_agents(
    container: Container = Depends(get_container),
    db: Session = Depends(get_db),
) -> JSONResponse:
    new_version = container.agent_registry.reload(db)
    return json_response({"registry_version": new_version})


@router.get("/api/tools", response_model=None)
def list_tools(container: Container = Depends(get_container)) -> JSONResponse:
    """Bare JSON array of {name, description}."""
    return json_response(container.tool_registry.catalogue())


@router.get("/api/agents/{key}/config/versions", response_model=None)
def list_config_versions(
    key: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Bare JSON array, newest version first."""
    agent_row = AgentRepository(db).find_id_by_key(key)
    if not agent_row:
        return _admin_error("AGENT_NOT_FOUND", 404)

    rows = AgentConfigRepository(db).list_versions(agent_row.id)

    versions = [
        {
            "version": r.version,
            "checksum": r.checksum,
            "is_active": r.is_active,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
    return json_response(versions)


@router.post("/api/agents/{key}/config/{version}/activate", response_model=None)
def activate_config_version(
    key: str,
    version: str,
    container: Container = Depends(get_container),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    agent_row = AgentRepository(db).find_id_by_key(key)
    if not agent_row:
        return _admin_error("AGENT_NOT_FOUND", 404)

    # `version` is typed str and parsed here so a non-integer produces this
    # router's own envelope rather than FastAPI's 422 body.
    try:
        version_int = int(version)
    except (TypeError, ValueError):
        return _admin_error("INVALID_REQUEST", 400, msg="Version not found")

    configs = AgentConfigRepository(db)
    # 400, not 404 -- the agent exists, the requested version does not.
    if not configs.version_exists(agent_row.id, version_int):
        return _admin_error("INVALID_REQUEST", 400, msg="Version not found")

    # Deactivate-then-activate ordering lives in the repository, because
    # uq_agent_configs_one_active is a per-statement partial unique index.
    configs.activate_version(agent_row.id, version_int, actor=user.user_id)

    AuditLogRepository(db).insert(
        action="config_activate",
        entity_type="agent_configuration",
        entity_id=agent_row.id,
        actor=user.user_id,
        note=f"Activated version {version_int} for {key}",
    )

    db.commit()
    new_version = container.agent_registry.reload(db)

    return json_response({"version": version_int, "registry_version": new_version})


@router.post("/api/agents/{key}/config", response_model=None)
def create_config_version(
    key: str,
    body: Optional[Dict[str, Any]] = Depends(json_body_strict),
    container: Container = Depends(get_container),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    agent_row = AgentRepository(db).find_id_and_type_by_key(key)
    if not agent_row:
        return _admin_error("AGENT_NOT_FOUND", 404)

    if not body:
        return _admin_error("CONFIG_INVALID", 422, path=[])

    # Validated by hand rather than as a FastAPI body model: the discriminated
    # AgentSpec union's own errors must surface in THIS shape (first error only,
    # with `path` and `msg`), not FastAPI's {"detail": [...]}.
    try:
        spec = TypeAdapter(AgentSpec).validate_python(body)
    except ValidationError as e:
        err = e.errors()[0]
        return _admin_error("CONFIG_INVALID", 422, path=list(err["loc"]), msg=err["msg"])

    if spec.agent_type != agent_row.agent_type:
        return _admin_error(
            "CONFIG_INVALID", 422, path=["agent_type"], msg="Cannot change agent type"
        )

    if spec.agent_type == "llm" and getattr(spec, "tools", None):
        try:
            container.tool_registry.assert_all_known(spec.tools)
        except Exception as e:  # noqa: BLE001
            return _admin_error("CONFIG_INVALID", 422, path=["tools"], msg=str(e))

    if spec.agent_type == "remote_flow":
        problem = remote_config_problem(spec, container.settings)
        if problem is not None:
            path, msg = problem
            return _admin_error("CONFIG_INVALID", 422, path=path, msg=msg)

    canonical, checksum = canonical_json(spec)

    # The deactivate-before-insert ordering that uq_agent_configs_one_active
    # requires is the repository's job -- see AgentConfigRepository.
    new_version, row = AgentConfigRepository(db).create_active_version(
        agent_row.id, canonical, checksum, actor=user.user_id,
    )

    AuditLogRepository(db).insert(
        action="config_create",
        entity_type="agent_configuration",
        entity_id=agent_row.id,
        actor=user.user_id,
        note=f"Created version {new_version} for {key}",
    )

    db.commit()
    registry_version = container.agent_registry.reload(db)

    return json_response({
        "version": new_version,
        "checksum": checksum,
        "activated_at": row.created_at.isoformat() if row.created_at else None,
        "registry_version": registry_version,
    })


@router.get("/api/agents/{key}", response_model=None)
def get_agent_detail(key: str, db: Session = Depends(get_db)) -> JSONResponse:
    row = AgentRepository(db).find_detail_with_active_config(key)

    if not row:
        return _admin_error("AGENT_NOT_FOUND", 404)

    config = row.config if row.config else {}

    return json_response({
        "id": str(row.id),
        "key": key,
        "name": row.name,
        "description": row.description,
        "agent_type": row.agent_type,
        "status": row.status,
        "active_version": row.version,
        "activated_at": row.created_at.isoformat() if row.created_at else None,
        "config": redact_secrets(config),
    })


@router.patch("/api/agents/{key}", response_model=None)
def update_agent_status(
    key: str,
    body: Dict[str, Any] = Depends(json_body_silent),
    container: Container = Depends(get_container),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    agents = AgentRepository(db)
    agent_row = agents.find_id_by_key(key)
    if not agent_row:
        return _admin_error("AGENT_NOT_FOUND", 404)

    status = body.get("status")
    if status not in ("enabled", "disabled"):
        return _admin_error("INVALID_REQUEST", 400)

    agents.set_status(agent_row.id, status, actor=user.user_id)

    AuditLogRepository(db).insert(
        action="agent_enable" if status == "enabled" else "agent_disable",
        entity_type="agent",
        entity_id=agent_row.id,
        actor=user.user_id,
        note=f"Status set to {status}",
    )

    db.commit()
    new_version = container.agent_registry.reload(db)

    return json_response(
        {"id": str(agent_row.id), "key": key, "status": status, "registry_version": new_version}
    )
