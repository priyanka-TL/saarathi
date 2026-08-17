"""Finalising a delegated conversation, and recovering a turn the remote
platform swallowed.

Responsible for: the claim-then-submit sequence that ends a session, and the
read-only reconciliation that recovers a timed-out turn.
Used by: OrchestrationService, which owns the ordering around it.

THE CLAIM IS STEP 1 AND NOTHING ELSE RUNS UNLESS IT IS WON. A platform that
finalises into an artifact typically keys it uniquely on the session, so a
second finalize() for one session fails upstream; claim_finalizing()'s
conditional UPDATE makes a duplicate structurally impossible here too. Do not
reorder the numbered steps.

`provider_for` is injected as a callable because which provider finalises a
session is a per-agent, per-tenant decision -- it must be the one built from the
same connection the conversation ran over.

KNOWS NO PLATFORM. This module used to import one vendor's exception module,
close one vendor's channel pool, call one vendor's REST methods by name, and
gate recovery on `agent_type != "remote_flow"` -- which silently excluded the
other platform's agents from turn recovery entirely.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from app.agents.protocol import AgentTurn, SessionDelta, SessionState
from app.core.logger import get_logger
from app.providers.recovery import Reconciliation, TurnOutcome
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
    :param provider_for: resolves the provider for a scope-resolved agent, or
        None when the agent is not delegated / no registry was supplied.
    """

    def __init__(
        self, *, sessions, messages, conversations, audit,
        provider_for: Callable[[Any], Any],
    ) -> None:
        self._sessions = sessions
        self._messages = messages
        self._conversations = conversations
        self._audit = audit
        self._provider_for = provider_for

    def reconcile_turn(self, agent, session_view, sent_text: str, user) -> Optional[Reconciliation]:
        """Ask the remote platform what became of ``sent_text``. None if not
        applicable.

        WHETHER RECOVERY APPLIES IS THE PROVIDER'S DECLARATION, not a check on
        the agent type. That check used to read `agent_type != "remote_flow"`,
        which excluded the second platform's agents from recovery altogether --
        their timeouts surfaced as 504s even when the reply had already been
        recorded upstream.
        """
        if session_view is None:
            return None

        # Same rule as finalisation: ask the deployment this agent's scope
        # actually conversed against, not whichever one the default scope names.
        provider = self._provider_for(agent)
        if provider is None or not getattr(provider, "supports_recovery", False):
            return None

        return provider.reconcile(agent.spec.remote, session_view, sent_text, user)

    def recover_timed_out_turn(self, agent, session_view, sent_text, exc, user):
        """Turn a MitraTurnTimeout into the reply Mitra already produced.

        Returns an AgentTurn to carry on with, or None to let the timeout
        propagate (the client then gets its 504 as before).
        """
        try:
            result = self.reconcile_turn(agent, session_view, sent_text, user)
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
            "turn recovery: recovered a reply the provider had already sent",
            extra={"stage": result.stage, "agent_key": getattr(agent, "key", None)},
        )

        # The completion poll normally runs inside the handler, which never got
        # that far. Without this a conversation that COMPLETED during the
        # timeout would never finalise.
        # `.done`, NEVER the returned object. `is_complete` answers with a
        # `CompletionCheck`, and a dataclass instance is always truthy -- the old
        # `bool(provider is not None and provider.is_complete(...))` here would
        # report every recovered turn as terminal and finalise the interview.
        #
        # The refreshed count this check carries is deliberately NOT written back
        # on the delta below: this path exists because a turn timed out, so the
        # session's own state is what should survive. A cache left one turn
        # behind is exactly what the over-seek is built to absorb.
        done = False
        try:
            provider = self._provider_for(agent)
            if provider is not None:
                done = provider.is_complete(
                    agent.spec.remote, session_view, user,
                ).done
        except Exception as poll_error:  # noqa: BLE001
            logger.warning("turn recovery: completion poll failed: %s", poll_error)

        return AgentTurn(
            text=result.bot_text,
            # Deliberately no options: GET /api/companychat/ carries no
            # extra_content, so choice buttons cannot be recovered. Flows that
            # emit them degrade to text (record_stories/guided_guest never does).
            options=[],
            session_delta=SessionDelta(
                state=SessionState.awaiting_user,
                # step is left alone -- the recovered payload carries `stage`,
                # not the numeric step, and step is display-only.
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

        A platform that finalises into an artifact typically keys it uniquely
        on the session, so a second finalize() call for one session fails
        upstream. claim_finalizing()'s conditional UPDATE +
        uq_agent_sessions_remote_session together make a duplicate structurally
        impossible on Saarthi's side too, which is why the claim is step 1 and
        everything else only runs if it's won.
        """
        provider = self._provider_for(agent)
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
        if provider is not None:
            provider.close_channel(claimed.conversation_id)

        # 3b. THE NO-ARTIFACT PATH.
        #
        # A flow that creates no artifact has nothing to submit, and calling
        # finalize() on it raises -- which the except below would turn into a
        # FAILED session. A perfectly normal conversation would end up looking
        # like an outage.
        #
        # result_ref is the REMOTE SESSION ID rather than an artifact id, and
        # that is honest rather than a placeholder: the transcript is
        # retrievable from the platform with exactly this value, so it really is
        # the reference to what this session produced. It also satisfies
        # ck_agent_sessions_completed_has_result without weakening that
        # constraint for every other provider.
        #
        # The claim above still ran, so two concurrent terminal turns cannot
        # both write the follow-up message.
        if provider is None or not getattr(agent.spec.remote, "produces_artifact", True):
            completed = self._sessions.apply(
                claimed,
                SessionDelta(
                    state=SessionState.completed,
                    result_ref=claimed.remote_session_id,
                ),
            )
            logger.info(
                "session completed with no artifact",
                extra={
                    "session_id": str(claimed.id),
                    "agent_key": getattr(agent, "key", None),
                    "provider": getattr(agent.spec.remote, "provider", None),
                },
            )
            return Finalization(session=completed, claimed=True)

        # 4. FINALIZE, THROUGH THIS AGENT'S OWN PROVIDER: `agent` is
        #    scope-resolved by the caller, so for a tenant that points at its own
        #    deployment this is that tenant's endpoint, not the default one.
        #
        #    HOW finalisation is performed is entirely the provider's business
        #    -- which endpoint, whether the user's token goes in a header or a
        #    body, whether it is sent at all. Those three used to be read off the
        #    spec and passed as arguments HERE, which is what made this method
        #    know one platform's API. It now knows only that finalisation
        #    yields a result reference and possibly an artifact.
        #
        #    The artifact is fetched by the provider BEFORE this returns, and
        #    that ordering is load-bearing: SessionService.apply() rejects every
        #    call on an already-terminal session, including same-state
        #    field-only updates, so a second apply() to attach the URL after
        #    transitioning to 'completed' would raise InvalidTransitionError.
        #    Fetching first lets result_ref and report_url land in the SAME
        #    apply() call. A failed artifact fetch is non-fatal inside the
        #    provider, for the same reason: finalisation already succeeded and
        #    is irreversible.
        try:
            result = provider.finalize(agent.spec.remote, claimed, user)
        except Exception as e:  # noqa: BLE001
            # Don't leave the session stuck in 'finalizing' forever -- that
            # state has no other way out. A session that can never reach a
            # terminal state is a real bug.
            self._sessions.apply(claimed, SessionDelta(state=SessionState.failed, error=str(e)))
            raise

        # 5. Transition to completed -- apply() sets ended_at/finalized_at itself.
        #    report_url stays null if the artifact hasn't been generated yet;
        #    a client polls /api/sessions/{id}/report for it later (outside
        #    this method's scope).
        delta_fields = {"result_ref": result.result_ref}
        if result.artifact_url:
            delta_fields["report_url"] = result.artifact_url
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
