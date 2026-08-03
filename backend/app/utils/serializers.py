"""Row/DTO -> plain-JSON projections shared by more than one router.

Lifted verbatim from src/api/session_routes.py's module-level helpers. Key
names, key ORDER and null-handling are part of the API contract -- the golden
fixtures compare parsed dicts, but the frontend reads several of these fields
positionally in its own state machine.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def serialize_session(dto, agent_key: Optional[str]) -> Dict[str, Any]:
    """The full session payload used by every /api/sessions route.

    TWO KEYS NO LONGER HAVE A COLUMN BEHIND THEM, AND STILL BELONG HERE.
    `agent_sessions` dropped `started_at` (it was always exactly `created_at`)
    and `error_code` (nothing ever wrote it, so it was NULL in every row). The
    keys stay because this shape is the pinned API contract -- the golden
    fixtures compare it and the client reads it -- so the schema change is
    absorbed at this one boundary rather than propagated to every consumer.
    """
    return {
        "id": str(dto.id),
        "conversation_id": str(dto.conversation_id),
        "agent_key": agent_key,
        "state": dto.state,
        "step": dto.step,
        "turn_count": dto.turn_count,
        "result_ref": dto.result_ref,
        "report_url": dto.report_url,
        "error": dto.error,
        "error_code": None,
        "started_at": dto.created_at.isoformat(),
        "last_activity_at": dto.last_activity_at.isoformat(),
        "finalized_at": dto.finalized_at.isoformat() if dto.finalized_at else None,
        "ended_at": dto.ended_at.isoformat() if dto.ended_at else None,
    }


def serialize_session_summary(dto, agent_key: Optional[str]) -> Dict[str, Any]:
    """The SHORT session payload embedded in /api/chat and in the messages
    endpoint's `sessions` array.

    Deliberately a different, smaller shape from serialize_session -- do not
    collapse the two. The client's `_handleSession` reads only these fields,
    and widening them would change what a replayed conversation renders.
    """
    return {
        "id": str(dto.id),
        "state": dto.state,
        "step": dto.step,
        "agent_key": agent_key,
        "result_ref": dto.result_ref,
        "report_url": dto.report_url,
    }


def agent_key_for(container, dto) -> Optional[str]:
    """Resolve a session's agent_id to its key via the registry snapshot.

    Returns None when the agent is no longer in the enabled snapshot (it was
    disabled after the session ran). The client renders that as an unattributed
    session rather than an error.
    """
    agent = container.agent_registry.get_by_id(str(dto.agent_id))
    return agent.key if agent is not None else None
