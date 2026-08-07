"""Finalising a delegated interview, and recovering a turn Mitra swallowed.

Responsible for: the claim-then-submit sequence that ends an interview, and the
read-only reconciliation that recovers a timed-out turn.
Used by: OrchestrationService, which owns the ordering around it.

THE CLAIM IS STEP 1 AND NOTHING ELSE RUNS UNLESS IT IS WON. Mitra's
`Story.session` is UNIQUE, so a second finalize() for one session fails on
Mitra's side; claim_finalizing()'s conditional UPDATE makes a duplicate
structurally impossible here too. Do not reorder the numbered steps.

`rest_for` is injected as a callable because which client finalises a story is a
per-agent, per-tenant decision -- it must be the one built from the same
connection the interview ran over.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from app.agents.protocol import AgentTurn, SessionDelta, SessionState
from app.core.logger import get_logger
from app.integrations.mitra.exceptions import MitraError
from app.integrations.mitra.turn_recovery import Reconciliation, TurnOutcome, reconcile
from app.models.orm import SYSTEM_ACTOR

logger = get_logger("turn_finalization")

#: What Saarthi says once a delegated interview -- story OR discussion -- has
#: been submitted, so the transcript ends on a question to the user rather than
#: on a download link.
#:
#: MUST stay byte-identical to COPY.sessionFollowUp in
#: frontend/src/constants/index.js. The client renders this bubble locally the
#: moment its session poll reports 'completed' (there is no transcript refetch
#: after a turn), and this stored row is what a reload replays in its place --
#: so a drift between the two literals shows up as the message CHANGING when
#: the user refreshes. tests/guards/test_sync_contract.py pins them together.
SESSION_FOLLOW_UP = "Is there anything else I can help you with today?"


@dataclass
class Finalization:
    """The outcome of a finalisation attempt, plus WHO finalised.

    `claimed` is False when another request already owned finalisation and this
    call merely read back the result. That distinction is invisible in the
    returned session -- both paths can hand back a 'completed' row -- and it is
    exactly what decides whether this call may also write the follow-up message.
    Without it, POST /api/sessions/{id}/finalize (idempotent by design, §10.2)
    would append a second "anything else?" every time it is called again.
    """
    session: Any
    claimed: bool


class TurnFinalizer:
    """Finalisation and lost-turn recovery for one request's session.

    :param sessions: SessionService, for the claim and the state transitions.
    :param messages: MessageRepository, for the follow-up message.
    :param conversations: ConversationRepository, for seq allocation and touch.
    :param audit: AuditLogRepository.
    :param mitra_sessions: the channel pool, or None when Mitra is disabled.
    :param mitra_clients: the client registry, or None.
    :param mitra_rest: a single fallback client, for registry-less callers.
    :param rest_for: resolves the REST client for a scope-resolved agent.
    """

    def __init__(
        self, *, sessions, messages, conversations, audit,
        mitra_sessions, mitra_clients, mitra_rest,
        rest_for: Callable[[Any], Any],
    ) -> None:
        self._sessions = sessions
        self._messages = messages
        self._conversations = conversations
        self._audit = audit
        self._mitra_sessions = mitra_sessions
        self._mitra_clients = mitra_clients
        self._mitra_rest = mitra_rest
        self._rest_for = rest_for

    def reconcile_turn(self, agent, session_view, sent_text: str) -> Optional[Reconciliation]:
        """Ask Mitra what became of ``sent_text``. None if not applicable."""
        # Either wiring will do: production passes only the registry, tests and
        # registry-less callers pass only the single client. Checking just
        # mitra_rest would silently disable turn recovery in production.
        if (self._mitra_clients is None and self._mitra_rest is None) or session_view is None:
            return None
        if getattr(agent.spec, "agent_type", None) != "remote_flow":
            return None
        if not session_view.remote_session_id or not session_view.remote_profile_id:
            return None

        # Same rule as _finalize: ask the Mitra this agent's scope actually
        # interviewed against, not whichever one the default scope points at.
        rows = self._rest_for(agent).recent_chat(
            session_view.remote_session_id, session_view.remote_profile_id,
        )
        return reconcile(rows, sent_text)

    def recover_timed_out_turn(self, agent, session_view, sent_text, exc):
        """Turn a MitraTurnTimeout into the reply Mitra already produced.

        Returns an AgentTurn to carry on with, or None to let the timeout
        propagate (the client then gets its 504 as before).
        """
        try:
            result = self.reconcile_turn(agent, session_view, sent_text)
        except Exception as recovery_error:
            # Recovery is best-effort: never let it mask the original timeout.
            logger.warning(
                "turn recovery failed after %s: %s", exc, recovery_error,
                extra={"agent_key": getattr(agent, "key", None)},
            )
            return None

        if result is None or result.outcome is not TurnOutcome.ANSWERED:
            logger.info(
                "turn recovery: %s -- surfacing the timeout",
                result.outcome.value if result else "not applicable",
            )
            return None

        logger.info(
            "turn recovery: recovered a reply Mitra had already sent",
            extra={"stage": result.stage, "agent_key": getattr(agent, "key", None)},
        )

        # completion_poll_every_turn normally runs inside the handler, which
        # never got that far. Without this an interview that COMPLETED during
        # the timeout would never finalise.
        done = False
        try:
            done = bool(
                agent.spec.remote.completion_poll_every_turn
                and self._rest_for(agent).is_session_completed(session_view.remote_session_id)
            )
        except Exception as poll_error:
            logger.warning("turn recovery: completion poll failed: %s", poll_error)

        return AgentTurn(
            text=result.bot_text,
            # Deliberately no options: GET /api/companychat/ carries no
            # extra_content, so choice buttons cannot be recovered. Flows that
            # emit them degrade to text (record_stories/guided_guest never does).
            options=[],
            session_delta=SessionDelta(
                state=SessionState.awaiting_user,
                # step is left alone -- the REST payload has `stage`, not the
                # numeric step, and step is display-only.
                remote_session_id=session_view.remote_session_id,
                remote_profile_id=session_view.remote_profile_id,
                remote_flow=agent.spec.remote.flow_name,
                remote_bot_route=session_view.remote_bot_route,
            ),
            terminal=done,
        )

    def record_session_follow_up(
        self, conversation_id, agent_id, user, request_id=None,
    ) -> None:
        """Persist Saarthi's closing question as a real assistant message.

        WHY STORED, when the completion notice right above it is not: the
        notice ("Your discussion report is ready" + the download link) is a
        RENDERING of the agent_sessions row, which is why
        GET /api/conversations/{id}/messages returns sessions instead of a
        synthetic message row for it. This is not that -- it is a
        conversational turn. Stored, it survives a reload in the right place
        and reaches the next agent as history, so the model that answers
        "yes, one more thing" can see what was asked.

        `agent_id` is the session's OWN agent, and it is not optional:
        ck_conversation_messages_assistant_attribution requires every assistant
        row to name a speaker (migration 0007 -- "no reply is ever anonymous"),
        and it is what distinct_agent_sequence reads to rebuild the flow
        breadcrumb. Naming the interview agent leaves that breadcrumb
        unchanged (consecutive duplicates collapse) and matches how the client
        already attributes the completion notice directly above. It says
        nothing about who answers NEXT: the session is terminal, so
        RouterService Gate 2 no longer finds it and the next turn routes free.

        `agent_session_id` IS left null, deliberately: the client anchors
        per-session UI after the LAST message carrying that id, so tagging this
        row with the session would place the completion notice -- and its
        Download PDF link -- BELOW the follow-up on replay, inverting the two.
        """
        self._messages.insert(
            conversation_id,
            self._conversations.next_seq_for_update(conversation_id),
            role="assistant",
            content=SESSION_FOLLOW_UP,
            agent_id=agent_id,
            request_id=request_id,
            actor=getattr(user, "user_id", None) or SYSTEM_ACTOR,
        )
        self._conversations.touch(conversation_id)

    def finalize(self, session_view, agent, user):
        """Finalisation, as a plain session -- the shape every caller that does
        not care who won the claim wants. See _finalize_claiming."""
        return self.finalize_claiming(session_view, agent, user).session

    def finalize_claiming(self, session_view, agent, user) -> Finalization:
        """Finalisation, triggered by AgentTurn.terminal (design doc §4.7, §8.5).

        Mitra's Story.session is UNIQUE (story_models.py:30, verified against
        real source) -- a second finalize() call for one session fails on
        Mitra's side. claim_finalizing()'s conditional UPDATE + uq_agent_sessions_remote_session
        together make a duplicate structurally impossible on Saarthi's side
        too, which is why the claim is step 1 and everything else only runs
        if it's won.
        """
        # 1. THE CLAIM.
        claimed = self._sessions.claim_finalizing(session_view.id)
        if claimed is None:
            # Zero rows: another request already owns finalisation. Return
            # whatever the DB actually shows right now (still 'finalizing',
            # or already 'completed' with its real result_ref if the winner
            # finished first) -- the client polls either way.
            current = self._sessions.get(session_view.id)
            return Finalization(
                session=current if current is not None else session_view, claimed=False,
            )

        # 2. Audit the claim.
        self._audit.insert(
            action="session_finalize",
            entity_type="agent_session",
            entity_id=claimed.id,
            before=session_view.model_dump(mode="json"),
            after=claimed.model_dump(mode="json"),
        )

        # 3. CLOSE THE CHANNEL cleanly BEFORE calling finalize.
        if self._mitra_sessions is not None:
            self._mitra_sessions.close(claimed.conversation_id)

        # 4. finalize() with the user's token.
        #    THROUGH THIS AGENT'S OWN CLIENT: `agent` is scope-resolved by the
        #    caller, so for a tenant that points at its own Mitra this is that
        #    tenant's endpoint, not the default one.
        rest = self._rest_for(agent)
        try:
            story_id, _content = rest.finalize(
                session_id=claimed.remote_session_id,
                profile_id=claimed.remote_profile_id,
                flow=agent.spec.remote.flow_name,
                language=claimed.language,
                token=user.token,
                # Per-agent, because v1 and v2 resolve the story bot from
                # different Mitra tables -- see MitraRestClient's module
                # docstring. Sending a flow to the endpoint that cannot
                # resolve it is a deterministic HTTP 500.
                path=agent.spec.remote.finalize_path,
                # Also per-agent: Mitra turns token presence into auth=True and
                # picks the PDF template's user_type from it, so a guest flow
                # finalised with a token renders a BLANK pdf rather than
                # failing. Must match the socket's `access_token: None`.
                as_guest=agent.spec.remote.finalize_as_guest,
            )
        except Exception as e:
            # Don't leave the session stuck in 'finalizing' forever -- that
            # state has no other way out. Not explicitly in the doc's
            # sequence, but a session that can never reach a terminal state
            # is a real bug.
            self._sessions.apply(claimed, SessionDelta(state=SessionState.failed, error=str(e)))
            raise

        # 6. get_report() -- fetched BEFORE the completed-transition, not after.
        # SessionService.apply() rejects every call on an already-terminal
        # session, including same-state field-only updates (terminal states
        # are final, by design -- see SessionService.ALLOWED). A second
        # apply() call to attach report_url after transitioning to
        # 'completed' would raise InvalidTransitionError. Fetching the
        # report first lets result_ref and report_url land in the SAME
        # apply() call instead.
        # NON-FATAL, and it must stay that way. finalize() above already
        # succeeded and is IRREVERSIBLE -- Mitra's Story.session is UNIQUE, so
        # the story cannot be submitted a second time. Letting a report-URL
        # problem propagate here left the session in 'finalizing' forever
        # (the except above only guards finalize(), and nothing revisits
        # 'finalizing'), with a story that exists in Mitra and can never be
        # re-fetched. Observed live: Mitra serves report PDFs from a
        # different host than MITRA_BASE_URL, so an incomplete
        # MITRA_ALLOWED_HOSTS makes _validate_url raise MitraSSRFError on
        # EVERY successful story.
        #
        # report_url is designed to be null here anyway -- generation lags,
        # and GET /api/sessions/{id}/report polls for it later, where the
        # identical failure is already treated as "not ready yet" (202).
        try:
            report_url = rest.get_report(
                claimed.remote_session_id, media_type=agent.spec.remote.report_media_type,
            )
        except MitraError as e:
            logger.warning(
                "finalize: report fetch failed for session %s (%s); "
                "completing without report_url -- the report route will retry",
                claimed.id, e,
            )
            report_url = None

        # 5. Transition to completed -- apply() sets ended_at/finalized_at itself.
        #    report_url stays null if the report hasn't been generated yet;
        #    a client polls /api/sessions/{id}/report for it later (outside
        #    this method's scope).
        delta_fields = {"result_ref": story_id}
        if report_url:
            delta_fields["report_url"] = report_url
        #    Transitioning to 'completed' is itself the release: the session is
        #    terminal, so RouterService Gate 2 stops finding it and the next
        #    turn routes freely. The explicit conversations.unpin() that used to
        #    follow this line is gone with the column.
        completed = self._sessions.apply(
            claimed, SessionDelta(state=SessionState.completed, **delta_fields),
        )
        # The follow-up message is NOT written here. This runs at step 10 of
        # handle_turn, before the agent's own closing line is inserted at step
        # 11, and seq is what orders the transcript -- writing it here would
        # put "anything else?" ABOVE the reply it follows. The callers write it
        # once they have, which is also why they need `claimed`.
        return Finalization(session=completed, claimed=True)
