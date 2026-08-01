"""ToolExecutionRepository — persists one tool invocation per row.

Design invariants (§4.8):
  * result_excerpt is TRUNCATED to 4096 chars at write time. This is done here,
    not in a DB CHECK constraint, so truncation is observable in application
    logs rather than silently enforced. One large tool result must not be able
    to make the table unreadable.
  * ck_tool_error (DB-level CHECK) rejects any non-success row whose error
    column is NULL. This repository enforces the same rule at the Python level
    so the error surfaces before hitting the DB round-trip.
  * bulk_insert is the hot path — called once per assistant turn. It flushes
    after the batch rather than per row to avoid N round-trips.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional, Sequence

from sqlalchemy.orm import Session

from app.agents.protocol import ToolTrace
from app.models.orm import ToolExecution, ToolStatusEnum

logger = logging.getLogger(__name__)

RESULT_EXCERPT_MAX = 4096


class ToolExecutionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def bulk_insert(
        self,
        message_id: uuid.UUID,
        agent_id: Optional[uuid.UUID],
        tool_traces: Sequence[ToolTrace],
        request_id: Optional[str] = None,
    ) -> None:
        """Persist one row per ToolTrace.

        Args:
            message_id:   The assistant ConversationMessage this turn produced.
            agent_id:     The agent that ran the tools (SET NULL on agent deletion).
            tool_traces:  The traces emitted by LlmAgentHandler.
            request_id:   The HTTP request id for log correlation.
        """
        if not tool_traces:
            return

        rows = []
        for trace in tool_traces:
            status = ToolStatusEnum(trace.status)

            # Python-level guard mirroring ck_tool_error. Raises before the
            # DB round-trip so the caller gets a clear error, not a vague
            # IntegrityError that loses the original context.
            if status != ToolStatusEnum.success and not trace.error:
                raise ValueError(
                    f"tool_executions ck_tool_error: status={trace.status!r} "
                    f"but error is None for tool {trace.tool_name!r}"
                )

            # Truncate at write time. Log so truncation is observable.
            excerpt = trace.result_excerpt
            if excerpt and len(excerpt) > RESULT_EXCERPT_MAX:
                logger.warning(
                    "tool_executions: result_excerpt truncated",
                    extra={
                        "tool_name": trace.tool_name,
                        "original_bytes": len(excerpt),
                        "truncated_to": RESULT_EXCERPT_MAX,
                    },
                )
                excerpt = excerpt[:RESULT_EXCERPT_MAX]

            rows.append(ToolExecution(
                message_id=message_id,
                agent_id=agent_id,
                tool_name=trace.tool_name,
                iteration=trace.iteration,
                arguments={},           # ToolTrace doesn't carry raw args; extend if needed
                result_excerpt=excerpt,
                result_bytes=len(trace.result_excerpt.encode()) if trace.result_excerpt else None,
                status=status,
                error=trace.error,
                duration_ms=trace.duration_ms,
                request_id=request_id,
            ))

        self._session.add_all(rows)
        self._session.flush()   # one round-trip for the batch; commit owned by the service layer
