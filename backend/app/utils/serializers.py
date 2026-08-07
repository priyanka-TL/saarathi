"""Row/DTO -> plain-JSON projections shared by more than one router.

Responsible for: the session payload shapes returned by the API.
Used by: the sessions, chat and conversations routers.

Key names and null-handling are part of the pinned API contract; the frontend
reads several fields positionally.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def serialize_session(dto, agent_key: Optional[str]) -> Dict[str, Any]:
    """The full session payload used by every /api/sessions route."""
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
        # `error_code` and `started_at` have no column behind them any more
        # (nothing wrote error_code; started_at always equalled created_at). The
        # keys stay because the API shape is pinned -- the schema change is
        # absorbed here rather than pushed to every consumer.
        "error_code": None,
        "started_at": dto.created_at.isoformat(),
        "last_activity_at": dto.last_activity_at.isoformat(),
        "finalized_at": dto.finalized_at.isoformat() if dto.finalized_at else None,
        "ended_at": dto.ended_at.isoformat() if dto.ended_at else None,
    }


def serialize_session_summary(dto, agent_key: Optional[str]) -> Dict[str, Any]:
    """The short session payload embedded in /api/chat and the messages endpoint.

    Deliberately smaller than serialize_session -- do not collapse the two.
    Widening it changes what a replayed conversation renders.
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
    """A session's agent key, or None if that agent is no longer enabled.

    None renders as an unattributed session rather than an error.
    """
    agent = container.agent_registry.get_by_id(str(dto.agent_id))
    return agent.key if agent is not None else None
