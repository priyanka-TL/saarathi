import uuid
from datetime import datetime
from unittest.mock import MagicMock, call, patch

import pytest

from app.agents.protocol import AgentSessionView, SessionDelta, SessionState
from app.domain.sessions import AgentSessionDTO
from app.integrations.mitra.exceptions import MitraError, MitraSSRFError, MitraTurnTimeout
from app.integrations.mitra.turn_recovery import ChatRow
from app.services.orchestration import OrchestrationService
from app.services.session_service import SessionService


def _session_dto(
    *,
    state: str = "awaiting_user",
    result_ref: str | None = None,
    report_url: str | None = None,
) -> AgentSessionDTO:
    """Build a minimal AgentSessionDTO for tests."""
    return AgentSessionDTO(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        state=state,
        remote_session_id="mitra-sess-abc",
        remote_profile_id="1355",
        remote_flow="guest-mi-story",
        remote_bot_route="some-bot-route",
        language="en",
        step=14,
        turn_count=14,
        result_ref=result_ref,
        report_url=report_url,
        error=None,
        state_data={},
        created_by="1",
        updated_by="1",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        last_activity_at=datetime.utcnow(),
        finalized_at=None,
        ended_at=None,
    )


def _completed_dto(session_dto: AgentSessionDTO, story_id: str) -> AgentSessionDTO:
    """Return a copy of session_dto in the completed state with result_ref set."""
    return session_dto.model_copy(update={
        "state": "completed",
        "result_ref": story_id,
        "finalized_at": datetime.utcnow(),
        "ended_at": datetime.utcnow(),
    })



def _make_agent(
    spec_remote_flow="guest-mi-story",
    spec_report_media="application/pdf",
    spec_finalize_path="/api/end-story/",
    spec_finalize_as_guest=False,
):
    agent = MagicMock()
    agent.spec.remote.flow_name = spec_remote_flow
    agent.spec.remote.report_media_type = spec_report_media
    agent.spec.remote.finalize_path = spec_finalize_path
    # Set explicitly: a MagicMock would auto-create a TRUTHY attribute here, so
    # these tests would silently assert a guest finalisation while claiming to
    # cover record_stories, which sends its token.
    agent.spec.remote.finalize_as_guest = spec_finalize_as_guest
    return agent


def _make_orch(
    *,
    db_session=None,
    mitra_rest=None,
    mitra_sessions=None,
    registry=None,
    sessions_service=None,
) -> OrchestrationService:
    """Build an OrchestrationService with all external dependencies mocked."""
    orch = OrchestrationService.__new__(OrchestrationService)
    orch._db = db_session or MagicMock()
    orch._registry = registry or MagicMock()
    orch._handlers = MagicMock()
    orch._llm_factory = MagicMock()
    orch._mitra_rest = mitra_rest or MagicMock()
    orch._mitra_sessions = mitra_sessions or MagicMock()
    # No client registry: rest_for() then falls back to the injected single
    # client above, which is what every assertion here is written against.
    # A registry is exercised in tests/guards/test_tenant_isolation.py, where
    # the point IS that two scopes get different clients.
    orch._mitra_clients = None
    orch._settings = None
    orch._conversations = MagicMock()
    orch._messages = MagicMock()
    orch._sessions = sessions_service or MagicMock()
    orch._sessions_repo = MagicMock()
    orch._audit = MagicMock()
    orch._router = MagicMock()
    orch._rate_limits = MagicMock()
    orch._tools_repo = MagicMock()
    return orch


# ---------------------------------------------------------------------------
# Test: two concurrent terminal turns → exactly one finalize() call
# ---------------------------------------------------------------------------

class TestConcurrentFinalisation:
    """claim_finalizing() is the idempotency guard (design doc §4.7).

    The DB conditional UPDATE is simulated by a mock that returns the session
    DTO on the first call (claim won) and None on the second (claim lost).
    """

    def _setup_winner_loser(self):
        """Shared setup for winner/loser pair tests."""
        session_dto = _session_dto()
        story_id = "story-9931"
        completed_dto = _completed_dto(session_dto, story_id)
        agent = _make_agent()

        mitra_rest = MagicMock()
        mitra_rest.finalize.return_value = (story_id, "Story narrative content.")
        mitra_rest.get_report.return_value = None  # report not yet generated

        mitra_sessions = MagicMock()

        sessions_svc = MagicMock(spec=SessionService)
        # First call (winner): claim_finalizing returns the session
        # Second call (loser): claim_finalizing returns None
        sessions_svc.claim_finalizing.side_effect = [session_dto, None]
        # apply() is called by the winner to set state=completed
        sessions_svc.apply.return_value = completed_dto
        # get() is called by the loser to fetch the cached result
        sessions_svc.get.return_value = completed_dto

        orch = _make_orch(
            mitra_rest=mitra_rest,
            mitra_sessions=mitra_sessions,
            sessions_service=sessions_svc,
        )

        user = MagicMock()
        user.token = "test-jwt-token"

        return orch, agent, session_dto, completed_dto, mitra_rest, mitra_sessions, sessions_svc, user

    def test_finalize_called_exactly_once_on_two_concurrent_calls(self):
        """Two calls to _finalize() produce exactly one mitra_rest.finalize()."""
        orch, agent, session_dto, completed_dto, mitra_rest, _, sessions_svc, user = (
            self._setup_winner_loser()
        )

        # Simulate two concurrent requests both reaching _finalize()
        result_winner = orch._finalize(session_dto, agent, user)
        result_loser  = orch._finalize(session_dto, agent, user)

        assert mitra_rest.finalize.call_count == 1, (
            "mitra_rest.finalize() must be called exactly ONCE regardless of "
            "how many concurrent callers reach _finalize(). Mitra's Story.session "
            "is UNIQUE (story_models.py:30) — a second call would 4xx."
        )

    def test_finalize_uses_the_agents_configured_endpoint(self):
        """_finalize must pass spec.remote.finalize_path through.

        Without it the client falls back to v2 for every agent, which for a
        flow Mitra has no Flow row for is a guaranteed HTTP 500 -- the
        end-story failure this pins.
        """
        orch, agent, session_dto, _, mitra_rest, _, _, user = self._setup_winner_loser()

        orch._finalize(session_dto, agent, user)

        assert mitra_rest.finalize.call_args.kwargs["path"] == "/api/end-story/"

    def test_finalize_passes_the_agents_token_presence_choice_through(self):
        """_finalize must pass spec.remote.finalize_as_guest through too.

        Mitra derives `auth = access_token is not None` and picks the PDF
        template's user_type from it, so getting this wrong does not fail the
        call -- it renders an EMPTY pdf and returns 200. record_stories sends
        its token (False); capture_discussion does not (True).
        """
        orch, agent, session_dto, _, mitra_rest, _, _, user = self._setup_winner_loser()

        orch._finalize(session_dto, agent, user)

        assert mitra_rest.finalize.call_args.kwargs["as_guest"] is False

    def test_report_fetch_failure_still_completes_the_session(self):
        """A failing get_report must NOT undo a successful finalize.

        finalize() is irreversible (Mitra's Story.session is UNIQUE), so a
        raise between it and the completed-transition leaves the session in
        'finalizing' forever with a story that exists remotely and can never
        be resubmitted. Observed live: report PDFs are served from a host
        outside MITRA_ALLOWED_HOSTS, so _validate_url raised MitraSSRFError
        on every successful story. GET /api/sessions/{id}/report already
        treats the same failure as 'not ready yet'.
        """
        orch, agent, session_dto, _, mitra_rest, _, sessions_svc, user = (
            self._setup_winner_loser()
        )
        mitra_rest.get_report.side_effect = MitraSSRFError()

        result = orch._finalize(session_dto, agent, user)

        assert result.state == "completed", (
            "session must reach 'completed' even when the report URL cannot "
            "be fetched -- 'finalizing' has no other way out"
        )
        applied_delta = sessions_svc.apply.call_args.args[1]
        assert applied_delta.state == SessionState.completed
        assert applied_delta.report_url is None

    def test_winner_returns_completed_session(self):
        orch, agent, session_dto, completed_dto, _, _, _, user = self._setup_winner_loser()
        result_winner = orch._finalize(session_dto, agent, user)
        orch._finalize(session_dto, agent, user)  # loser

        assert result_winner.state == "completed"
        assert result_winner.result_ref == "story-9931"

    def test_loser_returns_cached_result_not_error(self):
        """The loser must not raise — it returns the cached completed DTO."""
        orch, agent, session_dto, completed_dto, _, _, sessions_svc, user = (
            self._setup_winner_loser()
        )
        orch._finalize(session_dto, agent, user)  # winner
        result_loser = orch._finalize(session_dto, agent, user)  # loser

        assert result_loser.state == "completed"
        assert result_loser.result_ref == "story-9931"
        # Loser must call sessions_svc.get() to fetch cached result
        sessions_svc.get.assert_called_once_with(session_dto.id)

    def test_loser_does_not_close_channel(self):
        """The loser must not call mitra_sessions.close() — the winner already did."""
        orch, agent, session_dto, _, _, mitra_sessions, _, user = (
            self._setup_winner_loser()
        )
        orch._finalize(session_dto, agent, user)  # winner
        orch._finalize(session_dto, agent, user)  # loser

        # close() should have been called exactly once (by the winner)
        mitra_sessions.close.assert_called_once()

    def test_channel_closed_before_finalize(self):
        """§8.5: channel must close BEFORE end-story, not after.

        Verified by checking call order on the mock objects.
        """
        orch, agent, session_dto, completed_dto, mitra_rest, mitra_sessions, _, user = (
            self._setup_winner_loser()
        )
        # Track call order with a shared call recorder
        call_order = []
        mitra_sessions.close.side_effect = lambda *a, **kw: call_order.append("close")
        mitra_rest.finalize.side_effect = lambda **kw: (
            call_order.append("finalize"),
            ("story-9931", "content"),
        )[-1]

        orch._finalize(session_dto, agent, user)

        assert call_order == ["close", "finalize"], (
            "Channel must be closed BEFORE calling end-story (§8.5). "
            "If finalize fails, the channel would otherwise remain open."
        )

    def test_finalize_uses_authorization_bearer_token(self):
        """finalize() receives the user.token, not a hardcoded secret."""
        orch, agent, session_dto, _, mitra_rest, _, _, user = self._setup_winner_loser()
        user.token = "live-jwt-abc"

        orch._finalize(session_dto, agent, user)

        mitra_rest.finalize.assert_called_once()
        call_kwargs = mitra_rest.finalize.call_args[1]
        assert call_kwargs["token"] == "live-jwt-abc"
        # Confirm the token was passed in the header (via the client), not in body
        # (The client contract is tested in test_mitra_rest_client.py — here we
        # just confirm the right token reaches finalize().)

    def test_get_report_called_after_finalize(self):
        """get_report() is called to attach report_url in the same apply()."""
        orch, agent, session_dto, _, mitra_rest, _, _, user = self._setup_winner_loser()

        orch._finalize(session_dto, agent, user)

        mitra_rest.get_report.assert_called_once_with(
            session_dto.remote_session_id, media_type=agent.spec.remote.report_media_type
        )

    def test_completed_transition_includes_result_ref(self):
        """apply() must be called with result_ref set — DB ck_sess_completed_has_result."""
        orch, agent, session_dto, _, _, _, sessions_svc, user = self._setup_winner_loser()

        orch._finalize(session_dto, agent, user)

        apply_call = sessions_svc.apply.call_args
        delta: SessionDelta = apply_call[0][1]
        assert delta.state == SessionState.completed
        assert delta.result_ref == "story-9931"

    def test_completion_releases_the_conversation_for_routing(self):
        """The next turn must be routable again once the interview finishes.

        Reaching the terminal 'completed' state IS the release: RouterService
        Gate 2 finds the conversation's agent by looking for a NON-TERMINAL
        agent_sessions row, so a completed session is no longer found. This used
        to additionally assert a conversations.unpin() call against a
        pinned_agent_id column that duplicated the same fact; the column is gone
        and the state transition is the whole mechanism.
        """
        orch, agent, session_dto, _, _, _, sessions_svc, user = self._setup_winner_loser()

        orch._finalize(session_dto, agent, user)

        applied = [c for c in sessions_svc.apply.call_args_list
                   if c.args[1].state is SessionState.completed]
        assert len(applied) == 1, "exactly one transition to 'completed'"
        assert applied[0].args[0] is session_dto

    def test_finalize_failure_transitions_session_to_failed(self):
        """If mitra_rest.finalize() raises, the session must move to 'failed',
        not stay stuck in 'finalizing' forever."""
        session_dto = _session_dto()
        agent = _make_agent()

        mitra_rest = MagicMock()
        mitra_rest.finalize.side_effect = RuntimeError("Mitra 503")

        sessions_svc = MagicMock(spec=SessionService)
        sessions_svc.claim_finalizing.return_value = session_dto
        sessions_svc.apply.return_value = session_dto  # return something for the failed apply

        orch = _make_orch(mitra_rest=mitra_rest, sessions_service=sessions_svc)
        user = MagicMock(token="tok")

        with pytest.raises(RuntimeError, match="Mitra 503"):
            orch._finalize(session_dto, agent, user)

        # apply() was called with failed state
        apply_call = sessions_svc.apply.call_args
        delta: SessionDelta = apply_call[0][1]
        assert delta.state == SessionState.failed
        assert "503" in (delta.error or "")

    def test_audit_logged_on_successful_claim(self):
        """The claim must be audited (design doc §8.5 diagram step 2)."""
        orch, agent, session_dto, _, _, _, _, user = self._setup_winner_loser()

        orch._finalize(session_dto, agent, user)

        orch._audit.insert.assert_called_once()
        audit_kwargs = orch._audit.insert.call_args[1]
        assert audit_kwargs.get("action") == "session_finalize"

    def test_report_url_stored_when_pdf_already_generated(self):
        """When get_report() returns immediately, report_url lands in the same apply()."""
        session_dto = _session_dto()
        story_id = "story-xyz"
        agent = _make_agent()

        mitra_rest = MagicMock()
        mitra_rest.finalize.return_value = (story_id, "content")
        pdf_url = "https://mitra.example.com/stories/xyz.pdf"
        mitra_rest.get_report.return_value = pdf_url

        completed_dto = _completed_dto(session_dto, story_id)
        sessions_svc = MagicMock(spec=SessionService)
        sessions_svc.claim_finalizing.side_effect = [session_dto, None]
        sessions_svc.apply.return_value = completed_dto
        sessions_svc.get.return_value = completed_dto

        orch = _make_orch(mitra_rest=mitra_rest, sessions_service=sessions_svc)
        user = MagicMock(token="tok")

        orch._finalize(session_dto, agent, user)

        apply_call = sessions_svc.apply.call_args
        delta: SessionDelta = apply_call[0][1]
        assert delta.report_url == pdf_url

    def test_report_url_null_when_pdf_not_yet_generated(self):
        """When get_report() returns None, apply() omits report_url — client polls later."""
        session_dto = _session_dto()
        story_id = "story-xyz"
        agent = _make_agent()

        mitra_rest = MagicMock()
        mitra_rest.finalize.return_value = (story_id, "content")
        mitra_rest.get_report.return_value = None  # PDF not ready

        completed_dto = _completed_dto(session_dto, story_id)
        sessions_svc = MagicMock(spec=SessionService)
        sessions_svc.claim_finalizing.side_effect = [session_dto, None]
        sessions_svc.apply.return_value = completed_dto
        sessions_svc.get.return_value = completed_dto

        orch = _make_orch(mitra_rest=mitra_rest, sessions_service=sessions_svc)
        user = MagicMock(token="tok")

        orch._finalize(session_dto, agent, user)

        apply_call = sessions_svc.apply.call_args
        delta: SessionDelta = apply_call[0][1]
        assert delta.report_url is None


# ---------------------------------------------------------------------------
# Automatic recovery of a turn Saarthi stopped listening for.
#
# A MitraTurnTimeout does not mean the turn failed. Verified live: Mitra
# answered in 8.4s and only this side gave up. Without recovery the reply is
# lost even if the user does nothing -- MitraChannel._drain_stale() discards it
# at the start of the next turn -- so the interview desyncs silently.
# ---------------------------------------------------------------------------

class TestTimedOutTurnRecovery:

    SENT = "The improvement was implemented in Melur village, Madurai district, Tamil Nadu."
    REPLY = "What was the main problem you noticed in your school or community?"

    def _orch_and_agent(self, rows, completed=False):
        agent = _make_agent()
        agent.spec.agent_type = "remote_flow"
        agent.spec.remote.completion_poll_every_turn = True

        mitra_rest = MagicMock()
        mitra_rest.recent_chat.return_value = rows
        mitra_rest.is_session_completed.return_value = completed

        orch = _make_orch(mitra_rest=mitra_rest)
        session = _session_dto()
        return orch, agent, session, mitra_rest

    def test_answered_turn_is_recovered_instead_of_surfacing_a_timeout(self):
        rows = [
            ChatRow(id=1, from_user=True, message=self.SENT),
            ChatRow(id=2, from_user=False, message=self.REPLY),
        ]
        orch, agent, session, mitra_rest = self._orch_and_agent(rows)

        turn = orch._recover_timed_out_turn(agent, session, self.SENT, MitraTurnTimeout())

        assert turn is not None, "the reply Mitra already sent must not be thrown away"
        assert turn.text == self.REPLY
        assert turn.session_delta.state == SessionState.awaiting_user
        # Nothing was re-sent: recovery is read-only against Mitra.
        assert mitra_rest.recent_chat.called

    def test_recovery_never_re_sends_the_users_turn(self):
        """The whole point. A re-send lands against the NEXT question (§1.6)."""
        rows = [
            ChatRow(id=1, from_user=True, message=self.SENT),
            ChatRow(id=2, from_user=False, message=self.REPLY),
        ]
        orch, agent, session, mitra_rest = self._orch_and_agent(rows)

        orch._recover_timed_out_turn(agent, session, self.SENT, MitraTurnTimeout())

        for forbidden in ("send_and_await_turn", "send", "post"):
            assert not hasattr(mitra_rest, forbidden) or not getattr(mitra_rest, forbidden).called

    def test_still_pending_lets_the_timeout_propagate(self):
        """Mitra has the message but no answer yet -- the client must see the
        timeout and poll, not receive an empty turn."""
        rows = [ChatRow(id=1, from_user=True, message=self.SENT)]
        orch, agent, session, _ = self._orch_and_agent(rows)

        assert orch._recover_timed_out_turn(agent, session, self.SENT, MitraTurnTimeout()) is None

    def test_not_delivered_lets_the_timeout_propagate(self):
        orch, agent, session, _ = self._orch_and_agent([])

        assert orch._recover_timed_out_turn(agent, session, self.SENT, MitraTurnTimeout()) is None

    def test_completion_is_still_polled_so_a_finished_story_finalises(self):
        """completion_poll_every_turn normally runs inside the handler, which
        never got that far. Skipping it would strand an interview that reached
        COMPLETED during the timeout -- it would never finalise."""
        rows = [
            ChatRow(id=1, from_user=True, message=self.SENT),
            ChatRow(id=2, from_user=False, message=self.REPLY),
        ]
        orch, agent, session, _ = self._orch_and_agent(rows, completed=True)

        turn = orch._recover_timed_out_turn(agent, session, self.SENT, MitraTurnTimeout())

        assert turn.terminal is True

    def test_a_failing_recovery_never_masks_the_original_timeout(self):
        """Best-effort: if Mitra is unreachable the user must still get the
        timeout, not a confusing secondary error."""
        orch, agent, session, mitra_rest = self._orch_and_agent([])
        mitra_rest.recent_chat.side_effect = MitraError("mitra unreachable")

        assert orch._recover_timed_out_turn(agent, session, self.SENT, MitraTurnTimeout()) is None

    def test_llm_agents_are_not_reconciled(self):
        """Only remote_flow has server-side state at Mitra to reconcile with."""
        orch, agent, session, mitra_rest = self._orch_and_agent([])
        agent.spec.agent_type = "llm"

        assert orch._recover_timed_out_turn(agent, session, self.SENT, MitraTurnTimeout()) is None
        assert not mitra_rest.recent_chat.called
