"""Default capability membership.

Responsible for: repairing a database where the capability and agent migrations
got out of step.
Used by: sync_and_reload, once at startup.

Only ever fills a default-scope capability with NO members at all, so a curated
membership is never contradicted.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from app.core.logger import get_logger
from app.domain.scope import DEFAULT_SCOPE
from app.models.orm import SYSTEM_ACTOR

logger = get_logger("capability_seed")

#: The shipped membership: capability key -> the agents beneath it, in order.
#: `autostart` is sent with autostart:true so the server does not title the
#: conversation from it.
DEFAULT_MEMBERSHIP = {
    "listening_at_scale": [
        {
            "agent_key": "record_stories",
            "label": "Record Stories",
            "display_order": 10,
            "autostart": "I want to record a story",
        },
        {
            "agent_key": "capture_discussion",
            "label": "Capture Discussions",
            "display_order": 20,
            "autostart": "I want to capture a discussion",
        },
    ],
    # sg_commons is deliberately absent: it has no agents, and an empty
    # capability is a legitimate shape the frontend renders without an actions
    # block.
}


def seed_default_membership(session) -> int:
    """Fill empty default-scope capabilities. Returns rows inserted."""
    inserted = 0

    for capability_key, members in DEFAULT_MEMBERSHIP.items():
        row = session.execute(
            text("""
                SELECT c.id,
                       (SELECT count(*) FROM capability_agents ca
                         WHERE ca.capability_id = c.id) AS member_count
                FROM capabilities c
                WHERE c.key = :key
                  AND c.tenant_id = :scope AND c.organization_id = :scope
            """),
            {"key": capability_key, "scope": DEFAULT_SCOPE},
        ).fetchone()

        if row is None or row.member_count:
            # Absent (an operator removed it) or already curated. Leave alone.
            continue

        for member in members:
            result = session.execute(
                text("""
                    INSERT INTO capability_agents (capability_id, agent_id, display_order,
                                                   label_override, is_visible, metadata,
                                                   created_by, updated_by)
                    SELECT :capability_id, a.id, :display_order, :label, TRUE,
                           CAST(:metadata AS jsonb),
                           CAST(:actor AS varchar), CAST(:actor AS varchar)
                    FROM agents a
                    WHERE a.key = :agent_key
                    ON CONFLICT (capability_id, agent_id) DO NOTHING
                """),
                {
                    "capability_id": row.id,
                    # A boot-time write with no user behind it.
                    "actor": SYSTEM_ACTOR,
                    "agent_id_key": member["agent_key"],
                    "agent_key": member["agent_key"],
                    "display_order": member["display_order"],
                    "label": member["label"],
                    "metadata": json.dumps({
                        "action": {"type": "start_agent", "autostart": member["autostart"]}
                    }),
                },
            )
            inserted += result.rowcount or 0

    if inserted:
        session.commit()
        logger.info("capability seed: linked %s default membership row(s)", inserted)
    return inserted
