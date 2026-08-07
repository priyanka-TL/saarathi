"""The per-conversation turn lock.

Responsible for: guaranteeing at most one turn in flight per conversation.
Used by: OrchestrationService.handle_turn, around the whole turn.

WHY IT EXISTS: two concurrent posts for one conversation both reached Mitra,
where two user messages in a row MERGE and the first answer is lost (§1.6).
"""
from __future__ import annotations

import uuid

from sqlalchemy import text as sql_text

from app.core.logger import get_logger

logger = get_logger("turn_lock")


class ConversationTurnLock:
    """A session-scoped Postgres advisory lock, keyed by conversation.

    :param session: the request's SQLAlchemy session. MUST be pinned to one
        connection for the whole request -- see `app/dependencies/db.py`. A
        session-scoped advisory lock lives on the CONNECTION, so acquiring it on
        one pooled connection and releasing it on another silently leaks it.
    """

    def __init__(self, session) -> None:
        self._db = session

    def _is_postgres(self) -> bool:
        try:
            return self._db.get_bind().dialect.name == "postgresql"
        except Exception:
            return False

    def _key(self, conversation_id: uuid.UUID) -> str:
        return f"saarthi:turn:{conversation_id}"

    def acquire(self, conversation_id: uuid.UUID) -> bool:
        """Claim the right to run a turn on this conversation. False if another
        request already holds it.

        SESSION-scoped (pg_try_advisory_lock), not transaction-scoped, and
        deliberately so. A transaction-scoped lock would have to stay open
        across handler.handle(), and handler.handle() is a Mitra round trip of
        up to 60s -- holding a database transaction (and its pooled connection)
        for that long is exactly what test_no_transaction_held_during_handler
        exists to prevent. A session-scoped lock spans the commit at step 8
        without keeping a transaction open, so it can guard the handler call
        without reintroducing that problem.

        NON-BLOCKING, so a genuine double-submit is refused rather than queued
        and then executed a second time. Queueing would be the wrong answer
        anyway: the second copy of the same answer is exactly what triggers
        Mitra's consecutive-same-sender merge and destroys the first (§1.6).
        """
        if not self._is_postgres():
            return True  # sqlite (unit tests) has no advisory locks
        return bool(self._db.execute(
            sql_text("SELECT pg_try_advisory_lock(hashtext(:key))"),
            {"key": self._key(conversation_id)},
        ).scalar())

    def release(self, conversation_id: uuid.UUID) -> None:
        """Release the turn lock.

        REQUIRED, and not optional cleanup: a session-scoped advisory lock is
        NOT released by commit, by rollback, or by the connection being returned
        to the pool -- only by an explicit unlock or the connection actually
        closing. A missed release wedges that conversation until the connection
        is recycled.

        Best-effort on failure: the caller is already in a `finally`, and
        raising here would mask whatever sent it there.
        """
        if not self._is_postgres():
            return
        try:
            self._db.execute(
                sql_text("SELECT pg_advisory_unlock(hashtext(:key))"),
                {"key": self._key(conversation_id)},
            )
        except Exception as e:
            logger.warning(
                "failed to release turn lock: %s", e,
                extra={"conversation_id": str(conversation_id)},
            )
