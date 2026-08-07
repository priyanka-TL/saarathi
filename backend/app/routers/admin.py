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

Raw `sqlalchemy.text()` is used rather than repositories because `agents` and
`agent_configs` have no ORM model -- see app/models/orm.py.
"""
from __future__ import annotations


from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.container import Container
from app.dependencies.admin_gate import require_admin
from app.dependencies.body import json_body_silent, json_body_strict
from app.dependencies.container import get_container
from app.dependencies.db import get_db
from app.dependencies.identity import get_current_user
from app.domain.agent_spec import AgentSpec, canonical_json
from app.domain.core import UserContext
from app.repositories.audit import AuditLogRepository
from app.exceptions.admin_envelope import admin_error
from app.utils.responses import json_response

# Every route in this router is gated. The dependency raises AdminGateError,
# handled in app/exceptions/handlers.py.
router = APIRouter(tags=["admin"], dependencies=[Depends(require_admin)])


#: The bare admin envelope. Defined once in app/exceptions/admin_envelope.py;
#: aliased here because this module has ~20 call sites reading `_admin_error`.
_admin_error = admin_error


def _redact_secrets(data: Any) -> Any:
    """Any string still carrying a `${...}` placeholder is an unresolved env
    reference and may name a credential. Blank the whole value."""
    if isinstance(data, dict):
        return {k: _redact_secrets(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [_redact_secrets(i) for i in data]
    elif isinstance(data, str) and "${" in data:
        return "<REDACTED>"
    return data


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
    agent_row = db.execute(text("SELECT id FROM agents WHERE key = :key"), {"key": key}).fetchone()
    if not agent_row:
        return _admin_error("AGENT_NOT_FOUND", 404)

    rows = db.execute(
        text("""
            SELECT version, checksum, is_active, created_at
            FROM agent_configs
            WHERE agent_id = :agent_id
            ORDER BY version DESC
        """),
        {"agent_id": agent_row.id},
    ).fetchall()

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
    agent_row = db.execute(text("SELECT id FROM agents WHERE key = :key"), {"key": key}).fetchone()
    if not agent_row:
        return _admin_error("AGENT_NOT_FOUND", 404)

    # `version` is typed str and parsed here so a non-integer produces this
    # router's own envelope rather than FastAPI's 422 body.
    try:
        version_int = int(version)
    except (TypeError, ValueError):
        return _admin_error("INVALID_REQUEST", 400, msg="Version not found")

    exists = db.execute(
        text("SELECT 1 FROM agent_configs WHERE agent_id = :agent_id AND version = :version"),
        {"agent_id": agent_row.id, "version": version_int},
    ).scalar()

    # 400, not 404 -- the agent exists, the requested version does not.
    if not exists:
        return _admin_error("INVALID_REQUEST", 400, msg="Version not found")

    db.execute(
        text("UPDATE agent_configs SET is_active = FALSE, "
             "updated_by = :actor, updated_at = now() WHERE agent_id = :agent_id"),
        {"agent_id": agent_row.id, "actor": user.user_id},
    )
    db.execute(
        text(
            "UPDATE agent_configs SET is_active = TRUE, activated_at = now(), "
            "updated_by = :actor, updated_at = now() "
            "WHERE agent_id = :agent_id AND version = :version"
        ),
        {"agent_id": agent_row.id, "version": version_int, "actor": user.user_id},
    )

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


def _remote_config_problem(spec, settings) -> Optional[tuple[list, str]]:
    """Reject a remote_flow config that would fail SILENTLY at interview time.

    THIS ROUTE IS THE ONLY GATE. There is no YAML and no startup sync any more,
    so a config reaches Mitra exactly as it was written here. `bot_route` and
    `company` are non-empty by schema; what the schema cannot check is
    `finalize_path`, because the endpoints are configurable and the domain layer
    cannot import Settings to express them as a Literal.

    Getting it wrong is not a loud failure: anything unrecognised falls through
    to the v1 branch and finalises with the wrong body shape, which Mitra
    ACCEPTS -- returning a story, a story_media row, a 200 from get-story and a
    downloadable, completely blank PDF, with nothing logged anywhere.

    Returns (path, msg) for the error envelope, or None when the config is fine.
    """
    from app.integrations.mitra.connection import resolve_connection

    remote = spec.remote
    # Against the endpoints THIS spec resolves to -- it may carry its own
    # remote.connection.paths, and checking against the global pair would both
    # reject correct configs and accept wrong ones.
    paths = resolve_connection(settings, remote).paths
    if not paths.is_known_finalize(remote.finalize_path):
        return (
            ["remote", "finalize_path"],
            f"finalize_path {remote.finalize_path!r} matches neither the resolved "
            f"v1 endpoint ({paths.finalize_v1!r}) nor the resolved v2 endpoint "
            f"({paths.finalize_v2!r})",
        )

    return None


@router.post("/api/agents/{key}/config", response_model=None)
def create_config_version(
    key: str,
    body: Optional[Dict[str, Any]] = Depends(json_body_strict),
    container: Container = Depends(get_container),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    agent_row = db.execute(
        text("SELECT id, agent_type FROM agents WHERE key = :key"), {"key": key}
    ).fetchone()
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
        problem = _remote_config_problem(spec, container.settings)
        if problem is not None:
            path, msg = problem
            return _admin_error("CONFIG_INVALID", 422, path=path, msg=msg)

    canonical, checksum = canonical_json(spec)

    # Deactivate BEFORE inserting: uq_agent_configs_one_active is a per-statement
    # partial unique index with no DEFERRABLE, so insert-then-deactivate raises
    # a UniqueViolation.
    db.execute(
        text("UPDATE agent_configs SET is_active = FALSE, "
             "updated_by = :actor, updated_at = now() WHERE agent_id = :agent_id"),
        {"agent_id": agent_row.id, "actor": user.user_id},
    )

    new_version = db.execute(
        text("SELECT COALESCE(MAX(version), 0) + 1 FROM agent_configs WHERE agent_id = :agent_id"),
        {"agent_id": agent_row.id},
    ).scalar()

    row = db.execute(
        text("""
            INSERT INTO agent_configs (agent_id, version, config, checksum, is_active,
                                       activated_at, created_by, updated_by)
            VALUES (:agent_id, :version, :config, :checksum, TRUE, now(), :actor, :actor)
            RETURNING created_at
        """),
        {
            "agent_id": agent_row.id,
            "version": new_version,
            "config": canonical,  # a JSON string -- psycopg can't adapt a raw dict to jsonb here
            "checksum": checksum,
            "actor": user.user_id,
        },
    ).fetchone()

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
    row = db.execute(
        text("""
            SELECT a.id, a.name, a.description, a.agent_type, a.status,
                   c.config, c.version, c.created_at
            FROM agents a
            LEFT JOIN agent_configs c ON a.id = c.agent_id AND c.is_active = TRUE
            WHERE a.key = :key
        """),
        {"key": key},
    ).fetchone()

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
        "config": _redact_secrets(config),
    })


@router.patch("/api/agents/{key}", response_model=None)
def update_agent_status(
    key: str,
    body: Dict[str, Any] = Depends(json_body_silent),
    container: Container = Depends(get_container),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    agent_row = db.execute(text("SELECT id FROM agents WHERE key = :key"), {"key": key}).fetchone()
    if not agent_row:
        return _admin_error("AGENT_NOT_FOUND", 404)

    status = body.get("status")
    if status not in ("enabled", "disabled"):
        return _admin_error("INVALID_REQUEST", 400)

    db.execute(
        text("UPDATE agents SET status = :status, "
             "updated_by = :actor, updated_at = now() WHERE id = :id"),
        {"status": status, "id": agent_row.id, "actor": user.user_id},
    )

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
