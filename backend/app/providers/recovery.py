"""Deciding what became of a turn a provider never answered.

Responsible for: reconciling the text we sent against the provider's own record
of the conversation.
Used by: TurnFinalizer, after a turn timeout and on POST /sessions/{id}/resume.

READ-ONLY against the provider, and that is the whole point. Re-sending is
unsafe: the provider may have answered and moved on, so the resend would land
against the NEXT question. Worse, a provider that merges consecutive messages
from the same sender would silently absorb the duplicate and destroy an answer.
Recovery is a reconcile, never a retry.

Promoted out of the Mitra package unchanged -- nothing here was ever
Mitra-specific. A `ChatRow` is "one message in the provider's own transcript",
which any provider that can be asked for its recent history can supply.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional


class TurnOutcome(Enum):
    """What the provider's record says happened to a turn we stopped listening for."""

    ANSWERED = "answered"
    """Received AND replied to. Take the reply; never re-send."""

    PENDING = "pending"
    """Received but not answered yet. Poll; never re-send."""

    NOT_DELIVERED = "not_delivered"
    """No record of the turn. This -- and only this -- makes re-sending safe."""


@dataclass(frozen=True)
class ChatRow:
    """One message in a provider's own transcript, normalised."""

    id: int
    from_user: bool
    message: str
    translated_message: str = ""
    stage: str = ""

    def matches(self, text: str) -> bool:
        """A provider may store the original in ``message`` and, for a
        non-English route, a translation alongside it -- and which one carries
        the user's own words depends on the flow's language config. Compare
        against both."""
        needle = (text or "").strip()
        if not needle:
            return False
        return needle in (
            (self.message or "").strip(),
            (self.translated_message or "").strip(),
        )


@dataclass(frozen=True)
class Reconciliation:
    outcome: TurnOutcome
    bot_text: str = ""
    stage: str = ""
    last_row_id: Optional[int] = None


def reconcile(rows: List[ChatRow], sent_text: str) -> Reconciliation:
    """Decide the fate of ``sent_text`` from the tail of the provider's transcript.

    ``rows`` must be in chronological order (ascending id), oldest first.
    """
    # Scan backwards for OUR message: the most recent matching user row is the
    # one we just sent. Searching forwards would match an identical answer given
    # earlier in the interview ("Yes", "No") and report a stale reply as ours.
    user_index = None
    for i in range(len(rows) - 1, -1, -1):
        row = rows[i]
        if row.from_user and row.matches(sent_text):
            user_index = i
            break

    if user_index is None:
        return NOT_DELIVERED_RESULT if not rows else Reconciliation(
            outcome=TurnOutcome.NOT_DELIVERED, last_row_id=rows[-1].id,
        )

    # The FIRST bot row after ours is the reply to it. Later bot rows would
    # belong to subsequent turns, which are not ours to claim.
    for row in rows[user_index + 1:]:
        if not row.from_user:
            return Reconciliation(
                outcome=TurnOutcome.ANSWERED,
                bot_text=(row.message or "").strip(),
                stage=row.stage or "",
                last_row_id=row.id,
            )

    return Reconciliation(outcome=TurnOutcome.PENDING, last_row_id=rows[-1].id)


NOT_DELIVERED_RESULT = Reconciliation(outcome=TurnOutcome.NOT_DELIVERED)
