"""Links seeded capabilities to seeded agents, once agents exist.

WHY THIS EXISTS AT ALL -- an ordering problem, not a design preference
---------------------------------------------------------------------
Two seeds run at different times against different sources:

  * `capabilities` are seeded by MIGRATION 0006. A migration cannot depend on
    agents existing, because on a fresh database it runs before anything has
    been synced.
  * `agents` are seeded by migration 0007, which is
    after every migration has run.

`capability_agents` needs both. On a fresh database the migration therefore
cannot create the membership rows -- the agents do not exist yet, and the table
holds a real foreign key. Without this module a brand-new deployment would come
up with capability cards that have no buttons.

WHY IT IS SAFE TO RUN ON EVERY BOOT
-----------------------------------
It only ever fills a capability that has NO membership at all, and only at the
DEFAULT scope. So:

  * a fresh database gets a working catalogue;
  * an admin who curated membership -- including one who deliberately removed a
    single agent -- is never contradicted, because that capability still has
    members and is skipped entirely;
  * a tenant's own capabilities are never touched.

An admin who empties a default capability completely WILL see it refilled on
the next restart. That is the accepted edge: "no members at all" is
indistinguishable from "never seeded", and self-healing a fresh install matters
more than honouring a deliberate total-emptying that the admin can express
better by disabling the capability.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from app.core.logger import get_logger

logger = get_logger("capability_seed")

DEFAULT_SCOPE = "default"

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
                                                   label_override, is_visible, metadata)
                    SELECT :capability_id, a.id, :display_order, :label, TRUE,
                           CAST(:metadata AS jsonb)
                    FROM agents a
                    WHERE a.key = :agent_key
                    ON CONFLICT (capability_id, agent_id) DO NOTHING
                """),
                {
                    "capability_id": row.id,
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
