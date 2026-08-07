"""Turn limits, enforced against the agent's own LimitsSpec.

This was a no-op stub (`RateLimitsDummy`), so every `limits:` block in every
agent config was decorative -- max_turns and both rate limits were declared,
validated, checksummed into the config table, and then never consulted. An
interview had no turn ceiling at all.

Deliberately cheap: two indexed counts against tables the turn is about to write
to anyway. No Redis, and no in-process state -- which would be wrong the moment
there is a second worker, and MITRA_ENABLED=1 already pins this deployment to
one worker for an unrelated reason that will not always hold.
"""
from __future__ import annotations

from sqlalchemy import text as sql_text

from app.exceptions.domain import TurnLimitExceeded


class RateLimits:
    """Enforces LimitsSpec. This was a no-op stub (`RateLimitsDummy`), so every
    `limits:` block in every agent YAML was decorative -- max_turns and both
    rate limits were declared, validated, checksummed into the config table,
    and then never consulted. An interview had no turn ceiling at all.

    Deliberately cheap: two indexed counts against tables the turn is about to
    write to anyway. No Redis, no in-process state (which would be wrong the
    moment there is a second worker).
    """

    def __init__(self, db, sessions_repo):
        self._db = db
        self._sessions = sessions_repo

    def check(self, conv_id, user, limits) -> None:
        if limits is None:
            return

        max_turns = getattr(limits, "max_turns", None)
        if max_turns:
            open_session = self._sessions.get_open_for_conversation(conv_id)
            if open_session is not None and open_session.turn_count >= max_turns:
                raise TurnLimitExceeded(
                    "max_turns",
                    f"This interview has reached its limit of {max_turns} turns.",
                )

        per_min = getattr(limits, "rate_limit_per_conversation_per_min", None)
        if per_min:
            recent = self._db.execute(
                sql_text(
                    "SELECT count(*) FROM conversation_messages "
                    "WHERE conversation_id = :cid AND role = 'user' "
                    "AND created_at > now() - interval '1 minute'"
                ),
                {"cid": str(conv_id)},
            ).scalar() or 0
            # The current turn's user message is already inserted by the time
            # check() runs, so `>` not `>=`: a limit of 20 must allow the 20th.
            if recent > per_min:
                raise TurnLimitExceeded(
                    "rate_limit_per_conversation_per_min",
                    "You're sending messages too quickly. Please wait a moment.",
                )
