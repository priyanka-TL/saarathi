"""Persistence for the `agents` catalogue and its `agent_configs` versions.

`SELECT id FROM agents WHERE key = :key` was written out four times across
`app/routers/admin.py` and `app/routers/admin_config.py`, alongside the
config-versioning statements and the ordering rule that protects
`uq_agent_configs_one_active`.

Same two conventions as `capabilities.py`: raw `text()` SQL (the admin surface's
deliberate style, moved verbatim) and NO commits (the admin handlers own their
transaction boundary so a write is visible to the read that follows it).
"""
from __future__ import annotations

from typing import Any, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session


class AgentRepository:
    """Reads and writes the `agents` catalogue rows."""

    def __init__(self, session: Session) -> None:
        self._db = session

    def find_id_by_key(self, key: str) -> Optional[Any]:
        """The agent's id row, or None. The most-used lookup on this table."""
        return self._db.execute(
            text("SELECT id FROM agents WHERE key = :key"), {"key": key},
        ).fetchone()

    def find_id_and_type_by_key(self, key: str) -> Optional[Any]:
        """Id plus `agent_type`, for the config route.

        The type is needed because a config may not CHANGE an agent's type --
        the handler registry is keyed on it, and a spec whose type no longer
        matches its agent would build the wrong handler.
        """
        return self._db.execute(
            text("SELECT id, agent_type FROM agents WHERE key = :key"), {"key": key},
        ).fetchone()

    def find_detail_with_active_config(self, key: str) -> Optional[Any]:
        """The agent joined to its ACTIVE config version, for the detail route.

        LEFT JOIN on purpose: an agent with no active config is a real state
        (nothing has been published yet) and must render as an agent with an
        empty config, not as a 404.
        """
        return self._db.execute(
            text("""
                SELECT a.id, a.name, a.description, a.agent_type, a.status,
                       c.config, c.version, c.created_at
                FROM agents a
                LEFT JOIN agent_configs c ON a.id = c.agent_id AND c.is_active = TRUE
                WHERE a.key = :key
            """),
            {"key": key},
        ).fetchone()

    def set_status(self, agent_id: Any, status: str, actor: str) -> None:
        """Enable or disable one agent. The caller validates `status`."""
        self._db.execute(
            text("UPDATE agents SET status = :status, "
                 "updated_by = :actor, updated_at = now() WHERE id = :id"),
            {"status": status, "id": agent_id, "actor": actor},
        )


class AgentConfigRepository:
    """Versioned agent configurations.

    THE ONE INVARIANT THIS CLASS EXISTS TO PROTECT
    ----------------------------------------------
    `uq_agent_configs_one_active` is a PER-STATEMENT partial unique index with
    no DEFERRABLE, so at most one row per agent may have `is_active = TRUE` at
    the end of every statement -- not merely at commit. Every write that
    activates a version therefore has to deactivate the others FIRST, and
    insert-then-deactivate raises a UniqueViolation.

    That ordering used to live in the router, spelled out at two separate call
    sites. Both are now `create_active_version` and `activate_version`, which
    are the only supported ways to change which version is active.
    """

    def __init__(self, session: Session) -> None:
        self._db = session

    def list_versions(self, agent_id: Any) -> List[Any]:
        """Every version for one agent, newest first."""
        return self._db.execute(
            text("""
                SELECT version, checksum, is_active, created_at
                FROM agent_configs
                WHERE agent_id = :agent_id
                ORDER BY version DESC
            """),
            {"agent_id": agent_id},
        ).fetchall()

    def version_exists(self, agent_id: Any, version: int) -> bool:
        return bool(self._db.execute(
            text("SELECT 1 FROM agent_configs WHERE agent_id = :agent_id AND version = :version"),
            {"agent_id": agent_id, "version": version},
        ).scalar())

    def _deactivate_all(self, agent_id: Any, actor: str) -> None:
        """Clear `is_active` across every version of one agent.

        Private: it must never be the last statement of a request, or the agent
        is left with NO active config and the registry falls back to whatever it
        has cached. Both public writers below follow it with an activation.
        """
        self._db.execute(
            text("UPDATE agent_configs SET is_active = FALSE, "
                 "updated_by = :actor, updated_at = now() WHERE agent_id = :agent_id"),
            {"agent_id": agent_id, "actor": actor},
        )

    def activate_version(self, agent_id: Any, version: int, actor: str) -> None:
        """Make `version` the active one. Deactivates the rest first."""
        self._deactivate_all(agent_id, actor)
        self._db.execute(
            text(
                "UPDATE agent_configs SET is_active = TRUE, activated_at = now(), "
                "updated_by = :actor, updated_at = now() "
                "WHERE agent_id = :agent_id AND version = :version"
            ),
            {"agent_id": agent_id, "version": version, "actor": actor},
        )

    def create_active_version(
        self, agent_id: Any, canonical_config: str, checksum: str, actor: str,
    ) -> Tuple[int, Any]:
        """Insert the next version and make it active. Returns (version, row).

        :param canonical_config: the spec as a JSON STRING. psycopg cannot adapt
            a raw dict to jsonb here, and the string is also what the checksum
            was computed over -- so passing the dict would risk storing something
            that does not hash to the stored checksum.
        :returns: the new version number and the inserted row (for `created_at`).
        """
        self._deactivate_all(agent_id, actor)

        new_version = self._db.execute(
            text("SELECT COALESCE(MAX(version), 0) + 1 FROM agent_configs "
                 "WHERE agent_id = :agent_id"),
            {"agent_id": agent_id},
        ).scalar()

        row = self._db.execute(
            text("""
                INSERT INTO agent_configs (agent_id, version, config, checksum, is_active,
                                           activated_at, created_by, updated_by)
                VALUES (:agent_id, :version, :config, :checksum, TRUE, now(), :actor, :actor)
                RETURNING created_at
            """),
            {
                "agent_id": agent_id,
                "version": new_version,
                "config": canonical_config,
                "checksum": checksum,
                "actor": actor,
            },
        ).fetchone()

        return new_version, row
