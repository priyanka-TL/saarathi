"""Persistence for `capabilities` and `capability_agents`.

WHY THIS EXISTS
---------------
There was no capabilities repository at all: `app/routers/admin_config.py` held
410 lines of raw SQL, hand-assembled UPDATE statements and transaction control,
so the ROUTER WAS THE PERSISTENCE LAYER. The same `WHERE key/tenant/organization`
filter appeared four times and the same eleven-column SELECT list three times,
in one file.

TWO CONVENTIONS OF THIS TABLE'S CALLERS ARE PRESERVED EXACTLY
-------------------------------------------------------------
1. **Raw `text()` SQL, not the ORM.** The admin surface is deliberately written
   in SQL (see the header of `app/routers/admin_config.py`). The statements here
   are moved verbatim; converting them to the ORM would have made a mechanical
   move into a behavioural rewrite.

2. **Nothing here commits.** The admin handlers own their transaction boundary
   and commit explicitly, because a write must be visible to the read that
   follows it in the same request. A `commit()` in this layer would silently
   take that decision away from them.

SCOPE IS ALWAYS AN ARGUMENT, never derived here. Admin routes take tenant and
organization as explicit query parameters and must never fall back to the
calling admin's own token -- editing another tenant's configuration is the
normal case.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

#: Every column the admin API exposes for a capability, in the response's order.
#: One declaration: this was written out three times in the router, and a column
#: added to one copy but not the others is invisible until a client misses it.
CAPABILITY_COLUMNS = """id, tenant_id, organization_id, key, name, description, icon, badge,
                   status, display_order, metadata"""

#: Identifies exactly one row. A capability key is unique WITHIN a scope, never
#: globally -- omitting either scope term silently matches another tenant's row.
_BY_KEY_IN_SCOPE = "key = :key AND tenant_id = :tenant AND organization_id = :organization"

#: Fields a caller may set. `status` and `metadata` need casts, so they are
#: handled separately in `update_fields`.
_STATUS_FIELD = "status"
_METADATA_FIELD = "metadata"


class CapabilityRepository:
    """Reads and writes `capabilities` and its membership rows.

    :param session: the request's SQLAlchemy session. Not committed here.
    """

    def __init__(self, session: Session) -> None:
        self._db = session

    # ---- capabilities -----------------------------------------------------

    def list_in_scope(self, tenant: str, organization: str) -> List[Any]:
        """Every row in one scope, INCLUDING disabled ones.

        Deliberately not the resolved/inherited view: an admin needs to see what
        exists in this scope in order to edit it. `CapabilityService` answers the
        other question, for end users.
        """
        return self._db.execute(
            text(f"""
                SELECT {CAPABILITY_COLUMNS}
                FROM capabilities
                WHERE tenant_id = :tenant AND organization_id = :organization
                ORDER BY display_order, key
            """),
            {"tenant": tenant, "organization": organization},
        ).fetchall()

    def find_by_key(self, key: str, tenant: str, organization: str) -> Optional[Any]:
        """One scope's row for `key`, or None."""
        return self._db.execute(
            text(f"""
                SELECT {CAPABILITY_COLUMNS}
                FROM capabilities
                WHERE {_BY_KEY_IN_SCOPE}
            """),
            {"key": key, "tenant": tenant, "organization": organization},
        ).fetchone()

    def exists(self, key: str, tenant: str, organization: str) -> bool:
        row = self._db.execute(
            text(f"SELECT 1 FROM capabilities WHERE {_BY_KEY_IN_SCOPE}"),
            {"key": key, "tenant": tenant, "organization": organization},
        ).fetchone()
        return row is not None

    def insert(
        self,
        *,
        key: str,
        name: str,
        tenant: str,
        organization: str,
        actor: str,
        description: Optional[str] = None,
        icon: Optional[str] = None,
        badge: Optional[str] = None,
        status: str = "active",
        display_order: int = 100,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Insert one capability and return the created row.

        May raise on `ck_capabilities_key_slug` -- the caller decides how to
        report a malformed key, because only it knows the response envelope.
        """
        return self._db.execute(
            text(f"""
                INSERT INTO capabilities (tenant_id, organization_id, key, name, description,
                                          icon, badge, status, display_order, metadata,
                                          created_by, updated_by)
                VALUES (:tenant, :organization, :key, :name, :description, :icon, :badge,
                        CAST(:status AS capability_status_enum), :display_order,
                        CAST(:metadata AS jsonb), :actor, :actor)
                RETURNING {CAPABILITY_COLUMNS}
            """),
            {
                "tenant": tenant, "organization": organization, "key": key, "name": name,
                "description": description, "icon": icon, "badge": badge, "status": status,
                "display_order": display_order,
                "metadata": json.dumps(metadata or {}),
                "actor": actor,
            },
        ).fetchone()

    def update_fields(
        self, capability_id: Any, fields: Dict[str, Any], actor: str,
    ) -> Optional[Any]:
        """Set exactly the supplied `fields` on one row; return the updated row.

        Returns None when `fields` is empty, so the caller can answer with the
        unchanged row rather than issuing a pointless UPDATE.

        `fields` MUST already be validated against the allowed set by the
        caller: the keys are interpolated into the SQL (there is no other way to
        express a dynamic SET list), so an unvalidated key here would be an
        injection point. Values are always bound.
        """
        if not fields:
            return None

        sets: List[str] = []
        # `actor` is bound unconditionally: the UPDATE always sets updated_by.
        params: Dict[str, Any] = {"id": capability_id, "actor": actor}

        for field, value in fields.items():
            if field == _STATUS_FIELD:
                sets.append("status = CAST(:status AS capability_status_enum)")
                params["status"] = value
            elif field == _METADATA_FIELD:
                sets.append("metadata = CAST(:metadata AS jsonb)")
                params["metadata"] = json.dumps(value or {})
            else:
                sets.append(f"{field} = :{field}")
                params[field] = value

        return self._db.execute(
            text(f"""
                UPDATE capabilities SET {", ".join(sets)},
                                        updated_by = :actor, updated_at = now()
                WHERE id = :id
                RETURNING {CAPABILITY_COLUMNS}
            """),
            params,
        ).fetchone()

    def delete(self, capability_id: Any) -> None:
        """Delete one row. Membership rows cascade; the agents are untouched."""
        self._db.execute(
            text("DELETE FROM capabilities WHERE id = :id"), {"id": capability_id},
        )

    # ---- membership -------------------------------------------------------

    def find_id_by_key(self, key: str, tenant: str, organization: str) -> Optional[Any]:
        """Just the id, for the membership routes."""
        return self._db.execute(
            text(f"SELECT id FROM capabilities WHERE {_BY_KEY_IN_SCOPE}"),
            {"key": key, "tenant": tenant, "organization": organization},
        ).fetchone()

    def replace_members(
        self,
        capability_id: Any,
        members: Sequence[Tuple[Any, Dict[str, Any], int]],
        actor: str,
    ) -> None:
        """Replace this capability's membership wholesale.

        DELETE-then-INSERT rather than a diff, matching the PUT semantics of the
        route: membership is an ORDERED list, and incremental operations on one
        need either a diff protocol or a sequence of calls that can half-fail.

        `members` is `(agent_id, member_body, index)`, ALREADY RESOLVED AND
        VALIDATED by the caller. That split is deliberate: validating every
        agent_key before the DELETE runs is what makes an unknown key a clean
        422 rather than a partial write that emptied the membership first.

        :param members: resolved triples, in the order they should display.
        """
        self._db.execute(
            text("DELETE FROM capability_agents WHERE capability_id = :id"),
            {"id": capability_id},
        )
        for agent_id, member, index in members:
            self._db.execute(
                text("""
                    INSERT INTO capability_agents (capability_id, agent_id, display_order,
                                                   label_override, is_visible, metadata,
                                                   created_by, updated_by)
                    VALUES (:capability_id, :agent_id, :display_order, :label, :is_visible,
                            CAST(:metadata AS jsonb), :actor, :actor)
                """),
                {
                    "capability_id": capability_id,
                    "agent_id": agent_id,
                    # Falls back to position, so a caller that sends an ordered
                    # list without explicit orders still gets that order.
                    "display_order": member.get("display_order", (index + 1) * 10),
                    "label": member.get("label_override"),
                    "is_visible": member.get("is_visible", True),
                    "metadata": json.dumps(member.get("metadata") or {}),
                    "actor": actor,
                },
            )
