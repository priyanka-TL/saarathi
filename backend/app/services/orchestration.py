import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Any

from sqlalchemy import text as sql_text

from app.agents.protocol import TurnContext, AgentSessionView, Option, SessionDelta, SessionState
from app.exceptions.domain import ConcurrentTurnError, TurnLimitExceeded
from app.domain.agent_spec import DEFAULT_REPORT_MEDIA_TYPE
from app.domain.scope import scope_for_user
from app.domain.sessions import AgentSessionDTO
from app.models.orm import SYSTEM_ACTOR
from app.repositories.audit import AuditLogRepository
from app.repositories.conversations import ConversationRepository
from app.repositories.messages import MessageRepository
from app.repositories.sessions import AgentSessionRepository
from app.repositories.tool_executions import ToolExecutionRepository
from app.services.router_service import RouterService, RouteDecision
from app.services.session_service import SessionService
from app.services.agent_registry import AgentRegistry
from app.agents.factory import HandlerFactory
from app.integrations.mitra.exceptions import MitraError, MitraTurnTimeout
from app.integrations.mitra.turn_recovery import Reconciliation, TurnOutcome, reconcile
from app.agents.protocol import AgentTurn
from app.core.logger import get_logger

logger = get_logger("orchestration")

TITLE_MAX_LEN = 60
TITLE_TRUNCATE_AT = 57

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


def _conversation_title(text: str) -> str:
    """The conversation title is the first user message, truncated per contract
    (<=60 chars, else 57 + an ellipsis)."""
    return text if len(text) <= TITLE_MAX_LEN else text[:TITLE_TRUNCATE_AT] + "..."


def _agent_fallback_title(agent) -> str:
    """Title for a conversation opened by an autostart turn, where there is no
    user text to name it after. Dated so several interviews with the same agent
    stay distinguishable in the sidebar -- which is the whole point, since
    titling them from the canned opener made them all identical."""
    return _conversation_title(f"{agent.name} — {datetime.now(timezone.utc):%d %b %H:%M}")


def _to_session_view(dto: AgentSessionDTO) -> AgentSessionView:
    """AgentSessionDTO (app.domain.sessions) is the persisted-row Pydantic
    model SessionService deals in; AgentSessionView (app.agents.protocol) is
    the narrower, handler-facing dataclass every AgentHandler is written
    against -- RemoteFlowAgentHandler in particular calls dataclasses.replace()
    on ctx.session, which raises on a Pydantic instance. TurnContext.session
    must always be the latter, never the DTO directly."""
    return AgentSessionView(
        id=dto.id,
        conversation_id=dto.conversation_id,
        agent_id=dto.agent_id,
        state=SessionState(dto.state),
        remote_session_id=dto.remote_session_id,
        remote_profile_id=dto.remote_profile_id,
        remote_flow=dto.remote_flow,
        remote_bot_route=dto.remote_bot_route,
        language=dto.language,
        step=dto.step,
        turn_count=dto.turn_count,
        result_ref=dto.result_ref,
        report_url=dto.report_url,
        error=dto.error,
        state_data=dto.state_data,
    )

@dataclass
class TurnInput:
    request_id: str
    conversation_id: Optional[uuid.UUID]
    user: Any
    text: str
    option_id: Optional[str] = None
    agent_key: Optional[str] = None
    # True when the UI sent this message on the user's behalf to open an
    # interview (a capability button's data-autostart). It is a real turn -- the
    # remote flow needs it -- but it is not something the user typed, so it must
    # never become the conversation's title. Every Capture Discussion in the
    # sidebar was titled "I want to capture a discussion" because of this.
    autostart: bool = False

@dataclass
class ResumeResult:
    """Outcome of POST /api/sessions/{id}/resume."""
    outcome: TurnOutcome
    session: Any
    text: str = ""
    message: Any = None


@dataclass
class _Finalization:
    """The outcome of a `_finalize` attempt, plus WHO finalised.

    `claimed` is False when another request already owned finalisation and this
    call merely read back the result. That distinction is invisible in the
    returned session -- both paths can hand back a 'completed' row -- and it is
    exactly what decides whether this call may also write the follow-up
    message. Without it, POST /api/sessions/{id}/finalize (idempotent by
    design, §10.2) would append a second "anything else?" every time it is
    called again.
    """
    session: Any
    claimed: bool


@dataclass
class TurnResult:
    conversation: Any
    message: Any
    agent: Any
    decision: RouteDecision
    turn: Any
    session: Optional[AgentSessionView]


@dataclass(frozen=True)
class SessionReport:
    """The outcome of asking for a session's finalised report.

    `report_url is None` means "not ready, poll again" -- which covers both an
    interview that has not completed and a Mitra that was briefly unreachable.
    `media_type` is populated either way, because the client is told what it
    will be downloading before the URL exists.
    """
    report_url: Optional[str]
    media_type: str
    story_id: Optional[str]

    @property
    def ready(self) -> bool:
        return self.report_url is not None

# ConcurrentTurnError / TurnLimitExceeded are imported from
# app/exceptions/domain.py at the top of this module and remain importable from
# here -- `from app.services.orchestration import ConcurrentTurnError` is an
# established path used by the chat router and the tests.


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

class OrchestrationService:
    def __init__(
        self,
        session,
        registry: AgentRegistry,
        handler_factory: HandlerFactory,
        llm_factory: Any,
        router_service: Optional[RouterService] = None,
        mitra_rest: Optional[Any] = None,
        mitra_sessions: Optional[Any] = None,
        mitra_clients: Optional[Any] = None,
        settings: Optional[Any] = None,
    ):
        self._db = session
        self._registry = registry
        self._handlers = handler_factory
        self._llm_factory = llm_factory
        # mitra_clients is a MitraClientRegistry and is what production passes:
        # the finalisation paths must reach Mitra through the SAME endpoint the
        # turn used, and that endpoint is per agent and per tenant. mitra_rest
        # is the fallback for a caller that has no registry -- a single client
        # that knows one endpoint, which is why nothing in production builds one
        # any more (there is no MITRA_BASE_URL to build it from). See rest_for().
        self._mitra_rest = mitra_rest
        self._mitra_clients = mitra_clients
        self._settings = settings
        self._mitra_sessions = mitra_sessions

        self._conversations = ConversationRepository(session)
        self._messages = MessageRepository(session)
        self._sessions = SessionService(session)
        self._sessions_repo = AgentSessionRepository(session)
        self._audit = AuditLogRepository(session)
        self._tools_repo = ToolExecutionRepository(session)

        self._router = router_service or RouterService(session, registry, llm_factory)

        self._rate_limits = RateLimits(session, self._sessions_repo)

    # ------------------------------------------------------------------
    # Scope resolution for the paths that do NOT go through handle_turn
    # ------------------------------------------------------------------
    #
    # `AgentRegistry.get_by_id` answers from the DEFAULT-SCOPE snapshot -- it
    # is the process-wide cache of ('default','default') configs. handle_turn
    # has always corrected for that with resolve_for_scope(); resume, finalize
    # and the report route did not, so a tenant's own finalize_path,
    # report_media_type and (since remote.company / remote.bot_route moved into
    # the spec) its Mitra company were silently ignored on exactly the paths
    # that submit the story and fetch the PDF.
    #
    # Every path that reads an agent off a SESSION must go through here.

    def agent_for_session(self, session_view, user):
        """The session's agent, as this caller's tenant/organization sees it."""
        agent = self._registry.get_by_id(str(session_view.agent_id))
        if agent is None:
            return None
        tenant_id, organization_id = scope_for_user(user)
        return self._registry.resolve_for_scope(
            self._db, agent, tenant_id, organization_id,
        )

    def rest_for(self, agent):
        """The Mitra REST client for this (already scope-resolved) agent.

        A client carries a base URL, timeouts and the Origin credential, so the
        one that finalises a story must be the one built from the same
        connection the interview ran over. Falls back to the injected single
        client when no registry is available.
        """
        if self._mitra_clients is None or agent is None:
            return self._mitra_rest
        remote = getattr(agent.spec, "remote", None)
        if remote is None:
            return self._mitra_rest
        from app.integrations.mitra.connection import resolve_connection

        return self._mitra_clients.get(resolve_connection(self._settings, remote))

    def report_for(self, dto, user) -> "SessionReport":
        """The finalised interview report for a session, if it is ready yet.

        Three cases, and only the first two produce a URL:

        1. the session already carries a `report_url` -- serve it, no network;
        2. the interview is COMPLETED and Mitra has the document -- fetch the
           URL and serve that;
        3. anything else -- `report_url` is None and the client should poll.

        The agent is SCOPE-RESOLVED, like every other read of an agent off a
        session. `get_by_id` alone answers from the default-scope snapshot, so a
        tenant that had customised `report_media_type` -- or that points at its
        own Mitra -- would have had its report fetched with the default scope's
        settings.

        A MitraError is swallowed into case 3 rather than propagating, which is
        deliberate and unlike every other session route: a report that is not
        ready yet and a Mitra that is briefly unreachable are the same thing
        from the client's point of view, and both are fixed by polling again.
        """
        agent = self.agent_for_session(dto, user)
        media_type = (
            agent.spec.remote.report_media_type
            if agent is not None
            else DEFAULT_REPORT_MEDIA_TYPE
        )

        if dto.report_url:
            return SessionReport(dto.report_url, media_type, dto.result_ref)

        rest = self.rest_for(agent)
        if dto.state == "completed" and rest is not None and dto.remote_session_id:
            try:
                url = rest.get_report(dto.remote_session_id, media_type=media_type)
            except MitraError:
                url = None
            if url:
                return SessionReport(url, media_type, dto.result_ref)

        return SessionReport(None, media_type, dto.result_ref)

    def handle_turn(self, ctx_in: TurnInput) -> TurnResult:
        # 1. get_or_create conv
        conv = self._conversations.get_or_create(ctx_in.conversation_id, ctx_in.user)

        # 1b. CLAIM THE TURN, covering the handler call too.
        #
        # Step 2's row lock is released by the COMMIT at step 9, which happens
        # BEFORE the handler runs -- so two concurrent posts for one
        # conversation both sailed through and both called Mitra. On a first
        # turn that meant two upsert_profile + generate_session pairs and an
        # orphaned remote session; on a later turn it is §1.6 answer
        # destruction, since two user messages in a row silently merge in
        # Mitra's database. Claimed here, before the user message is written,
        # so a refused turn leaves nothing behind.
        if not self._try_lock_conversation(conv.id):
            raise ConcurrentTurnError(conv.id)
        try:
            return self._handle_turn_locked(ctx_in, conv)
        finally:
            self._unlock_conversation(conv.id)

    def _handle_turn_locked(self, ctx_in: TurnInput, conv) -> TurnResult:
        # 2. allocate seq under a row lock — also serialises double-submits (§1.6)
        seq = self._conversations.next_seq_for_update(conv.id)

        # 3. insert user message
        self._messages.insert(
            conv.id, seq, role="user", content=ctx_in.text,
            selected_option_id=ctx_in.option_id, request_id=ctx_in.request_id,
            actor=ctx_in.user.user_id,
        )

        # 3b. Title and timestamp the conversation NOW, from the user's own
        # message, not at step 13. Step 13 only runs on a successful turn, so a
        # turn that failed after this commit (an upstream 429, say) left the
        # conversation titleless and with a NULL last_message_at -- it showed up
        # in the sidebar as "New conversation / No messages yet" even though the
        # user had clearly said something. touch() COALESCEs the title, so the
        # first message still wins and step 13 remains harmless.
        #
        # An AUTOSTART turn timestamps but does not title: its text is the UI's
        # canned opener, so titling from it gave every discussion in the sidebar
        # the identical, useless name "I want to capture a discussion". The
        # first thing the user actually types titles the conversation instead,
        # and _agent_fallback_title covers the case where the interview is
        # answered entirely with option buttons.
        if ctx_in.autostart:
            self._conversations.touch(conv.id)
        else:
            self._conversations.touch(conv.id, title_from=_conversation_title(ctx_in.text))
            # If an autostart turn left a placeholder title behind, this is the
            # first thing the user has actually said -- promote it. touch()'s
            # COALESCE cannot do this on its own, and without it every
            # discussion would keep the generated name forever.
            self._conversations.replace_placeholder_title(
                conv.id, _conversation_title(ctx_in.text),
            )

        # 4. route (v2: 5 gates)
        # Build partial context for router
        ctx_partial = TurnContext(
            request_id=ctx_in.request_id,
            conversation_id=conv.id,
            user=ctx_in.user,
            text=ctx_in.text,
            option_id=ctx_in.option_id,
            history=[], # router might use recent history
            session=None,
            locale=ctx_in.user.locale if hasattr(ctx_in.user, 'locale') else "en"
        )
        # Populate history for router so it can be history-aware (gate 4)
        ctx_partial = TurnContext(
            request_id=ctx_in.request_id,
            conversation_id=conv.id,
            user=ctx_in.user,
            text=ctx_in.text,
            option_id=ctx_in.option_id,
            history=self._messages.recent(conv.id, self._registry.default().spec.memory) if self._registry.default() else [],
            session=None,
            locale=ctx_partial.locale
        )
        decision = self._router.select(conv, ctx_partial, explicit_key=ctx_in.agent_key)
        agent = decision.agent

        # 4b. Resolve the agent for THIS caller's tenant/organization.
        #
        # ONE PLACE, deliberately. `agent.spec` is read a dozen times below and
        # `agent.checksum` keys HandlerFactory's cache, so resolving once here
        # makes every one of those tenant-correct with no further changes --
        # and means there is a single line to audit when asking "can one tenant
        # be served another's configuration?".
        #
        # A no-op for a tenant that has not customised anything, which is the
        # common case: one indexed lookup, then the unchanged agent.
        turn_tenant_id, turn_organization_id = scope_for_user(ctx_in.user)
        agent = self._registry.resolve_for_scope(
            self._db, agent, turn_tenant_id, turn_organization_id,
        )

        # 5. enforce limits
        self._rate_limits.check(conv.id, ctx_in.user, agent.spec.limits)

        # 6. open the session -- which IS the pin. open_for() creates a row only
        #    when the agent declares routing.pin_session, and
        #    uq_agent_sessions_one_open_per_conversation makes that row the
        #    single answer to "which agent is driving this conversation", so
        #    RouterService Gate 2 finds it on the next turn with no LLM call.
        #    There is no separate step writing conversations.pinned_agent_id any
        #    more: that column duplicated this row under the identical condition
        #    and had to be cleared in step with it by hand.
        #
        #    on_displace fires when the conversation had an open session
        #    belonging to a DIFFERENT agent: that session is abandoned, and its
        #    Mitra socket has to go with it or the pool would hand the new agent
        #    a channel still authenticated against the old agent's remote
        #    session. That path is how one conversation spans several agents.
        session_view = self._sessions.open_for(
            conv.id, agent, on_displace=self._close_remote_channel,
            actor=ctx_in.user.user_id,
        )

        # 7. assemble history
        history = self._messages.recent(conv.id, agent.spec.memory)
        
        # 8. COMMIT <- releases the lock
        self._db.commit()

        # 9. handler.handle
        handler = self._handlers.build(agent.spec, agent.checksum)
        
        locale = ctx_in.user.locale if hasattr(ctx_in.user, 'locale') else "en"
        ctx = TurnContext(
            request_id=ctx_in.request_id,
            conversation_id=conv.id,
            user=ctx_in.user,
            text=ctx_in.text,
            option_id=ctx_in.option_id,
            history=history,
            session=_to_session_view(session_view) if session_view else None,
            locale=locale
        )
        
        try:
            turn = handler.handle(ctx)
        except MitraTurnTimeout as exc:
            # A timeout does NOT mean the turn failed -- it means we stopped
            # listening. Ask Mitra what actually happened before surfacing an
            # error, because nothing else will: the late frame is discarded by
            # _drain_stale() on the next turn, so an unrecovered reply desyncs
            # the interview even if the user does nothing at all.
            turn = self._recover_timed_out_turn(agent, session_view, ctx_in.text, exc)
            if turn is None:
                raise

        # 10. apply session delta; finalise if terminal
        if turn.session_delta and session_view:
            session_view = self._sessions.apply(session_view, turn.session_delta)
        finalization = None
        if turn.terminal and session_view:
            finalization = self._finalize_claiming(session_view, agent, ctx_in.user)
            session_view = finalization.session

        # 11. persist the agent message
        options_dict = [o.__dict__ for o in turn.options] if turn.options else None
        
        msg = self._messages.insert(
            conv.id, 
            self._conversations.next_seq_for_update(conv.id),
            role="assistant", 
            content=turn.text,
            agent_id=agent.id,
            agent_session_id=session_view.id if session_view else None,
            route_reason=decision.reason,
            route_confidence=decision.confidence,
            options=options_dict, 
            model=turn.model,
            prompt_tokens=turn.prompt_tokens,
            completion_tokens=turn.completion_tokens,
            latency_ms=turn.latency_ms,
            error=turn.error,
            request_id=ctx_in.request_id,
            actor=ctx_in.user.user_id,
        )

        # 11b. The interview just ended -- hand the conversation back to the
        #      user, as a stored message so a reload replays it.
        #
        #      HERE, not inside _finalize: step 11's insert has to come first or
        #      the follow-up outranks the agent's closing line by seq. And
        #      _finalize itself cannot simply move down to join it -- step 11
        #      holds the conversation row lock (next_seq_for_update) until the
        #      commit at step 13, and finalisation is a Mitra round trip.
        if (
            finalization is not None
            and finalization.claimed
            and session_view is not None
            and session_view.state == SessionState.completed.value
        ):
            self._record_session_follow_up(
                conv.id, session_view.agent_id, ctx_in.user, ctx_in.request_id,
            )

        # 12. persist tool traces
        if agent.spec.features.record_tool_executions:
            self._tools_repo.bulk_insert(
                msg.id, agent.id, turn.tool_traces,
                request_id=ctx_in.request_id,
                actor=ctx_in.user.user_id,
            )
        
        # 13. refresh last_message_at now the turn actually completed. The
        #     title was already set at step 3b and touch() COALESCEs it, so
        #     passing it again cannot overwrite the original.
        #
        #     On an autostart turn step 3b passed no title, so the conversation
        #     is still untitled here. Give it the agent's own name rather than
        #     leaving it to render as "New conversation" -- an interview
        #     answered entirely with option buttons may never produce a typed
        #     message at all. It is written as a PLACEHOLDER, so the first thing
        #     the user does type replaces it (step 3b above).
        if ctx_in.autostart:
            self._conversations.touch(conv.id)
            self._conversations.set_placeholder_title(conv.id, _agent_fallback_title(agent))
        else:
            self._conversations.touch(conv.id, title_from=_conversation_title(ctx_in.text))
        self._db.commit()

        # ONE line per completed turn, all fields, no interpolation.
        #
        # There was previously no success log at all: the pipeline logged only
        # its failures, so "which agent served this conversation, on what model,
        # and how long did it take" was answerable only from the database. The
        # latency was already measured and persisted -- it just never reached a
        # log line, which is where anyone actually looks first.
        logger.info(
            "turn completed",
            extra={
                "conversation_id": str(conv.id),
                "tenant_id": turn_tenant_id,
                "organization_id": turn_organization_id,
                "user_id": ctx_in.user.user_id,
                "agent_key": agent.key,
                "agent_type": agent.spec.agent_type,
                "route_reason": decision.reason,
                "route_confidence": decision.confidence,
                "router_latency_ms": decision.router_latency_ms,
                "model": turn.model,
                "latency_ms": turn.latency_ms,
                "tool_call_count": len(turn.tool_traces),
            },
        )

        return TurnResult(
            conversation=conv,
            message=msg,
            agent=agent,
            decision=decision,
            turn=turn,
            session=session_view
        )
        
    def _is_postgres(self) -> bool:
        try:
            return self._db.get_bind().dialect.name == "postgresql"
        except Exception:
            return False

    def _turn_lock_key(self, conversation_id: uuid.UUID) -> str:
        return f"saarthi:turn:{conversation_id}"

    def _try_lock_conversation(self, conversation_id: uuid.UUID) -> bool:
        """Claim the right to run a turn on this conversation. False if another
        request already holds it.

        SESSION-scoped (pg_try_advisory_lock), not transaction-scoped, and
        deliberately so. A transaction-scoped lock would have to stay open
        across handler.handle(), and handler.handle() is a Mitra round trip of
        up to 60s -- holding a database transaction (and its pooled connection)
        for that long is exactly what test_no_transaction_held_during_handler
        exists to prevent. A session-scoped lock spans the commit at step 9
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
            {"key": self._turn_lock_key(conversation_id)},
        ).scalar())

    def _unlock_conversation(self, conversation_id: uuid.UUID) -> None:
        """Release the turn lock. Required: a session-scoped advisory lock is
        NOT released by commit, rollback, or by the connection being returned
        to the pool -- only by an explicit unlock or the connection actually
        closing."""
        if not self._is_postgres():
            return
        try:
            self._db.execute(
                sql_text("SELECT pg_advisory_unlock(hashtext(:key))"),
                {"key": self._turn_lock_key(conversation_id)},
            )
        except Exception as e:
            logger.warning(
                "failed to release turn lock: %s", e,
                extra={"conversation_id": str(conversation_id)},
            )

    def _close_remote_channel(self, conversation_id: uuid.UUID) -> None:
        """Drop this conversation's pooled Mitra socket. Best-effort: the DB
        change that prompted it is already committed-or-committing, and a
        socket that fails to close is reaped by MitraSessionManager anyway."""
        if self._mitra_sessions is None:
            return
        try:
            self._mitra_sessions.close(conversation_id)
        except Exception as e:
            logger.warning(
                "failed to close Mitra channel: %s", e,
                extra={"conversation_id": str(conversation_id)},
            )

    # ------------------------------------------------------------------
    # Lost-turn recovery (see src/integrations/mitra/turn_recovery.py)
    # ------------------------------------------------------------------

    def _reconcile(self, agent, session_view, sent_text: str) -> Optional[Reconciliation]:
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
        rows = self.rest_for(agent).recent_chat(
            session_view.remote_session_id, session_view.remote_profile_id,
        )
        return reconcile(rows, sent_text)

    def _recover_timed_out_turn(self, agent, session_view, sent_text, exc):
        """Turn a MitraTurnTimeout into the reply Mitra already produced.

        Returns an AgentTurn to carry on with, or None to let the timeout
        propagate (the client then gets its 504 as before).
        """
        try:
            result = self._reconcile(agent, session_view, sent_text)
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
                and self.rest_for(agent).is_session_completed(session_view.remote_session_id)
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

    def resume_turn(self, session_id: uuid.UUID, user) -> Optional["ResumeResult"]:
        """Public entry point for POST /api/sessions/{id}/resume.

        The manual counterpart to _recover_timed_out_turn, for when automatic
        recovery came back PENDING (Mitra was still generating) and the client
        is polling. Read-only against Mitra: it never re-sends the user's turn.
        Returns None if no such session exists so the route can 404.
        """
        session_view = self._sessions.get(session_id)
        if session_view is None:
            return None

        agent = self.agent_for_session(session_view, user)
        last_user_text = self._messages.last_user_content(session_view.conversation_id)
        if agent is None or not last_user_text:
            return ResumeResult(outcome=TurnOutcome.NOT_DELIVERED, session=session_view)

        result = self._reconcile(agent, session_view, last_user_text)
        if result is None:
            return ResumeResult(outcome=TurnOutcome.NOT_DELIVERED, session=session_view)
        if result.outcome is not TurnOutcome.ANSWERED:
            return ResumeResult(outcome=result.outcome, session=session_view)

        # Persist the recovered reply so it survives a reload like any other
        # assistant message, then mirror handle_turn's tail.
        conv_id = session_view.conversation_id
        msg = self._messages.insert(
            conv_id,
            self._conversations.next_seq_for_update(conv_id),
            role="assistant",
            content=result.bot_text,
            agent_id=agent.id,
            agent_session_id=session_view.id,
        )
        updated = self._sessions.apply(
            session_view, SessionDelta(state=SessionState.awaiting_user),
        )
        self._conversations.touch(conv_id)
        self._db.commit()

        return ResumeResult(
            outcome=TurnOutcome.ANSWERED, text=result.bot_text,
            session=updated or session_view, message=msg,
        )

    def finalize_now(self, session_id: uuid.UUID, user) -> Optional[Any]:
        """Public entry point for POST /api/sessions/{id}/finalize -- a forced,
        idempotent end-story call (design doc §10.2). Returns None if no such
        session exists so the route can 404; otherwise reuses _finalize's
        existing claim/idempotency logic unchanged, so a mid-interview session
        finalizes early and a repeated call just returns the cached result."""
        session_view = self._sessions.get(session_id)
        if session_view is None:
            return None
        agent = self.agent_for_session(session_view, user)
        result = self._finalize_claiming(session_view, agent, user)
        # Only the call that actually finalised speaks. A repeat of this route
        # is a no-op that reads the same completed session back, and must not
        # ask the user "anything else?" a second time.
        if (
            result.claimed
            and result.session is not None
            and result.session.state == SessionState.completed.value
        ):
            self._record_session_follow_up(
                session_view.conversation_id, session_view.agent_id, user,
            )
        return result.session

    def _record_session_follow_up(
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

    def _finalize(self, session_view, agent, user):
        """Finalisation, as a plain session -- the shape every caller that does
        not care who won the claim wants. See _finalize_claiming."""
        return self._finalize_claiming(session_view, agent, user).session

    def _finalize_claiming(self, session_view, agent, user) -> _Finalization:
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
            return _Finalization(
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
        rest = self.rest_for(agent)
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
        return _Finalization(session=completed, claimed=True)
