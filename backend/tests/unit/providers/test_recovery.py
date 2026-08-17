"""Reconciling a lost turn against Mitra's CompanyChat record.

The rows in the "answered" case are the REAL ones from the reported incident
(session q6geuug..., message ids 101699-101704), which is the case the old
Retry button got wrong: it assumed NOT_DELIVERED and re-sent, so the answer
landed against the next question.
"""
from __future__ import annotations

from app.providers.recovery import (
    ChatRow,
    TurnOutcome,
    reconcile,
)

SENT = "The improvement was implemented in Melur village, Madurai district, Tamil Nadu."

# Verbatim from the incident.
INCIDENT_ROWS = [
    ChatRow(id=101699, from_user=True, message="GHS Melur", stage="IN_PROGRESS"),
    ChatRow(id=101700, from_user=False, message="Thanks for sharing. Can you tell me the name of the village, district, and state?", stage="PAUSED"),
    ChatRow(id=101701, from_user=True, message=SENT, stage="RESUME"),
    ChatRow(id=101702, from_user=False, message="What was the main problem you noticed in your school or community?", stage="IN_PROGRESS"),
]


class TestAnswered:

    def test_the_reported_incident_is_recognised_as_answered_not_lost(self):
        """THE regression test. Re-sending here is what destroyed the answer."""
        result = reconcile(INCIDENT_ROWS, SENT)

        assert result.outcome is TurnOutcome.ANSWERED
        assert result.bot_text == "What was the main problem you noticed in your school or community?"
        assert result.last_row_id == 101702

    def test_takes_the_first_bot_reply_after_ours_not_the_latest(self):
        """A later bot row belongs to a subsequent turn. Claiming it would show
        the user an answer to a question they never saw."""
        rows = INCIDENT_ROWS + [
            ChatRow(id=101703, from_user=True, message="Attendance was low", stage="IN_PROGRESS"),
            ChatRow(id=101704, from_user=False, message="What are the reasons for the problem?", stage="PAUSED"),
        ]

        result = reconcile(rows, SENT)

        assert result.bot_text == "What was the main problem you noticed in your school or community?"

    def test_matches_the_translated_message_too(self):
        """For a non-English route Mitra stores the translation alongside the
        original, and which field holds the user's own words varies by flow."""
        rows = [
            ChatRow(id=1, from_user=True, message="", translated_message="मेरा उत्तर"),
            ChatRow(id=2, from_user=False, message="धन्यवाद"),
        ]

        assert reconcile(rows, "मेरा उत्तर").outcome is TurnOutcome.ANSWERED


class TestPending:

    def test_message_recorded_but_no_reply_yet(self):
        """Mitra is still generating -- poll, never re-send."""
        result = reconcile(INCIDENT_ROWS[:3], SENT)

        assert result.outcome is TurnOutcome.PENDING
        assert result.bot_text == ""
        assert result.last_row_id == 101701


class TestNotDelivered:

    def test_message_absent_from_mitras_record(self):
        """The only outcome where re-sending is safe."""
        result = reconcile(INCIDENT_ROWS[:2], SENT)

        assert result.outcome is TurnOutcome.NOT_DELIVERED

    def test_empty_history(self):
        assert reconcile([], SENT).outcome is TurnOutcome.NOT_DELIVERED

    def test_a_bot_row_quoting_our_text_is_not_our_message(self):
        """Only USER rows can match -- otherwise a bot echoing the answer back
        would be mistaken for delivery confirmation."""
        rows = [ChatRow(id=1, from_user=False, message=SENT)]

        assert reconcile(rows, SENT).outcome is TurnOutcome.NOT_DELIVERED


class TestRepeatedAnswers:

    def test_matches_the_most_recent_occurrence_of_a_repeated_answer(self):
        """Short answers repeat across an interview ("Yes", "No"). Scanning
        forwards would match the FIRST "Yes" and hand back a stale reply that
        the user has already seen."""
        rows = [
            ChatRow(id=1, from_user=True, message="Yes"),
            ChatRow(id=2, from_user=False, message="First question?"),
            ChatRow(id=3, from_user=True, message="Yes"),
            ChatRow(id=4, from_user=False, message="Second question?"),
        ]

        result = reconcile(rows, "Yes")

        assert result.bot_text == "Second question?"
        assert result.last_row_id == 4

    def test_repeated_answer_still_pending_is_not_answered_by_the_older_reply(self):
        rows = [
            ChatRow(id=1, from_user=True, message="Yes"),
            ChatRow(id=2, from_user=False, message="First question?"),
            ChatRow(id=3, from_user=True, message="Yes"),
        ]

        assert reconcile(rows, "Yes").outcome is TurnOutcome.PENDING


class TestTolerance:

    def test_whitespace_differences_do_not_break_the_match(self):
        rows = [
            ChatRow(id=1, from_user=True, message="  my answer  "),
            ChatRow(id=2, from_user=False, message="Understood."),
        ]

        assert reconcile(rows, "my answer").outcome is TurnOutcome.ANSWERED

    def test_empty_sent_text_never_matches(self):
        """Guard: an empty needle must not match every blank row and invent a
        recovery out of nothing."""
        rows = [
            ChatRow(id=1, from_user=True, message=""),
            ChatRow(id=2, from_user=False, message="Hello?"),
        ]

        assert reconcile(rows, "").outcome is TurnOutcome.NOT_DELIVERED
