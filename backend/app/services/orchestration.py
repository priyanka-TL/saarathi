"""The turn pipeline: one user message in, one agent reply out.

Responsible for: ordering the thirteen steps of a turn -- claim the lock, store
the message, route, resolve scope, enforce limits, open the session, call the
handler, persist, finalise.
Used by: the chat and sessions routers, via Depends(get_orchestrator).

Finalisation lives in turn_finalization.py, the advisory lock in turn_lock.py
and rate limiting in turn_limits.py; this module keeps the sequence.

THE STEP ORDER IN _handle_turn_locked IS THE CONTRACT. Its numbered comments
record why each step sits where it does -- several encode constraints whose
failure mode is silent.
"""
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, List, Any

from app.agents.protocol import TurnContext, AgentSessionView, SessionDelta, SessionState
# ConcurrentTurnError / TurnLimitExceeded are re-exported: the chat router
# imports them from this module (`from app.services.orchestration import
# ConcurrentTurnError`), which is an established path.
from app.exceptions.domain import (  # noqa: F401  (re-exported)
    ConcurrentTurnError,
    TurnLimitExceeded,
)
from app.domain.agent_spec import DEFAULT_REPORT_MEDIA_TYPE
from app.domain.scope import scope_for_user
from app.domain.sessions import AgentSessionDTO
from app.repositories.audit import AuditLogRepository
from app.repositories.conversations import ConversationRepository
from app.repositories.messages import MessageRepository
from app.repositories.sessions import AgentSessionRepository
from app.repositories.tool_executions import ToolExecutionRepository
from app.services.router_service import RouterService, RouteDecision
from app.services.session_service import SessionService
from app.services.agent_registry import AgentRegistry
from app.services.turn_limits import RateLimits
from app.services.turn_lock import ConversationTurnLock
from app.services.turn_finalization import (
    SESSION_FOLLOW_UP,  # noqa: F401  (re-exported -- see the note below)
    TurnFinalizer,
)
from app.agents.factory import HandlerFactory
from app.providers.errors import ProviderError, ProviderTurnTimeout
from app.providers.recovery import Reconciliation, TurnOutcome
from app.core import timing
from app.core.logger import get_logger

logger = get_logger("orchestration")

TITLE_MAX_LEN = 60
TITLE_TRUNCATE_AT = 57

# SESSION_FOLLOW_UP is declared in app/services/turn_finalization.py and
# RE-EXPORTED from here on purpose: tests/guards/test_sync_contract.py imports
# it from this module to pin it byte-identical against the frontend's
# COPY.sessionFollowUp, and tests/integration/test_orchestration_finalize.py
# does the same. Removing the re-export breaks that pin.


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
    interview that has not completed and a provider that was briefly unreachable.
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


class OrchestrationService:
    def __init__(
        self,
        session,
        registry: AgentRegistry,
        handler_factory: HandlerFactory,
        llm_factory: Any,
        router_service: Optional[RouterService] = None,
        providers: Optional[Any] = None,
        settings: Optional[Any] = None,
    ):
        self._db = session
        self._registry = registry
        self._handlers = handler_factory
        self._llm_factory = llm_factory
        # A ProviderRegistry. The finalisation paths must reach the remote
        # platform through the SAME connection the turn used, and that
        # connection is per agent and per tenant -- so what is held here is the
        # thing that can resolve one, never a resolved client. See provider_for().
        self._providers = providers
        self._settings = settings

        self._conversations = ConversationRepository(session)
        self._messages = MessageRepository(session)
        self._sessions = SessionService(session)
        self._sessions_repo = AgentSessionRepository(session)
        self._audit = AuditLogRepository(session)
        self._tools_repo = ToolExecutionRepository(session)

        self._router = router_service or RouterService(session, registry, llm_factory)

        self._rate_limits = RateLimits(session, self._sessions_repo)
        self._turn_lock = ConversationTurnLock(session)

        # Finalisation and lost-turn recovery, in their own module.
        #
        # `rest_for` is passed as a BOUND METHOD, not a client: which client
        # finalises a story is a per-agent, per-tenant decision, and the one
        # that finalises must be built from the same connection the interview
        # ran over. Passing the method keeps that single resolution rule here.
        self._finalizer = TurnFinalizer(
            sessions=self._sessions,
            messages=self._messages,
            conversations=self._conversations,
            audit=self._audit,
            provider_for=self.provider_for,
        )

    # ------------------------------------------------------------------
    # Scope resolution for the paths that do NOT go through handle_turn
    # ------------------------------------------------------------------
    #
    # `AgentRegistry.get_by_id` answers from the DEFAULT-SCOPE snapshot -- it
    # is the process-wide cache of ('default','default') configs. handle_turn
    # has always corrected for that with resolve_for_scope(); resume, finalize
    # and the report route did not, so a tenant's own finalize_path,
    # report_media_type and (since remote.company / remote.bot_route moved into
    # the spec) its remote company were silently ignored on exactly the paths
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

    def provider_for(self, agent):
        """The provider for this (already scope-resolved) agent, or None.

        A provider carries a base URL, timeouts, endpoint paths and credentials,
        so the one that finalises a session must be the one built from the same
        connection the conversation ran over.

        THIS USED TO IGNORE THE AGENT'S PROVIDER ENTIRELY. It always built one
        named platform's REST client, so an agent on the other platform was
        handed the wrong client -- safe only by accident, because both callers
        happened to be gated before they could reach it.

        Returns None rather than raising for an agent with no `remote` block (an
        `llm` agent) or when no registry was supplied: every caller here treats
        "no provider" as "nothing to do", which is what keeps finalisation and
        recovery from having to know what kind of agent they were handed.
        """
        if self._providers is None or agent is None:
            return None
        remote = getattr(agent.spec, "remote", None)
        if remote is None:
            return None
        return self._providers.get(remote)

    def report_for(self, dto, user) -> "SessionReport":
        """The finalised interview report for a session, if it is ready yet.

        Three cases, and only the first two produce a URL:

        1. the session already carries a `report_url` -- serve it, no network;
        2. the interview is COMPLETED and the provider has the document -- fetch the
           URL and serve that;
        3. anything else -- `report_url` is None and the client should poll.

        The agent is SCOPE-RESOLVED, like every other read of an agent off a
        session. `get_by_id` alone answers from the default-scope snapshot, so a
        tenant that had customised `report_media_type` -- or that points at its
        own deployment -- would have had its report fetched with the default scope's
        settings.

        A ProviderError is swallowed into case 3 rather than propagating, which
        is deliberate and unlike every other session route: a report that is not
        ready yet and a provider that is briefly unreachable are the same thing
        from the client's point of view, and both are fixed by polling again.
        """
        agent = self.agent_for_session(dto, user)
        remote = getattr(agent.spec, "remote", None) if agent is not None else None
        media_type = (
            remote.report_media_type if remote is not None
            else DEFAULT_REPORT_MEDIA_TYPE
        )

        if dto.report_url:
            return SessionReport(dto.report_url, media_type, dto.result_ref)

        if dto.state == "completed" and remote is not None and dto.remote_session_id:
            try:
                provider = self.provider_for(agent)
                url = (
                    provider.fetch_artifact(remote, _to_session_view(dto))
                    if provider is not None else None
                )
            except ProviderError:
                url = None
            if url:
                return SessionReport(url, media_type, dto.result_ref)

        return SessionReport(None, media_type, dto.result_ref)

    # ------------------------------------------------------------------
    # Turn context assembly
    # ------------------------------------------------------------------

    def _turn_context(
        self, ctx_in: TurnInput, conv, *, history, session,
    ) -> TurnContext:
        """A TurnContext for this turn, differing only in history and session.

        The turn is built twice on purpose -- once for routing, which has no
        session yet and reads history against the default agent's window, and
        once for the handler, which has both. Everything else is identical, and
        was previously written out in full at each site.

        `locale` is read defensively because several call paths reach here with
        duck-typed identity objects that have no `locale`. `getattr` with a
        default, NOT `... or "en"`: an explicitly falsy locale must pass through
        unchanged, exactly as the `hasattr` check it replaces did.
        """
        return TurnContext(
            request_id=ctx_in.request_id,
            conversation_id=conv.id,
            user=ctx_in.user,
            text=ctx_in.text,
            option_id=ctx_in.option_id,
            history=history,
            session=session,
            locale=getattr(ctx_in.user, "locale", "en"),
        )

    def _router_history(self, conv) -> List[Any]:
        """Recent history for the ROUTER, read against the default agent's memory.

        Which agent will serve this turn is precisely what routing has not
        decided yet, so the default agent's window is the only one available.
        Empty when there is no default agent at all.
        """
        default = self._registry.default()
        if default is None:
            return []
        return self._messages.recent(conv.id, default.spec.memory)

    def handle_turn(self, ctx_in: TurnInput) -> TurnResult:
        # 1. get_or_create conv
        with timing.stage("conv_lookup"):
            conv = self._conversations.get_or_create(ctx_in.conversation_id, ctx_in.user)

        # 1b. CLAIM THE TURN, covering the handler call too. Step 2's row lock
        # is released by the commit at step 8, BEFORE the handler runs, so two
        # concurrent posts would both reach the platform -- answer destruction.
        # Claimed before the user message is written, so a refused turn leaves
        # nothing behind.
        with timing.stage("turn_lock"):
            locked = self._try_lock_conversation(conv.id)
        if not locked:
            raise ConcurrentTurnError(conv.id)
        try:
            return self._handle_turn_locked(ctx_in, conv)
        finally:
            self._unlock_conversation(conv.id)

    def _record_user_message(self, ctx_in: TurnInput, conv) -> None:
        """Steps 2, 3 and 3b: allocate a seq, store the message, title the
        conversation.

        The row lock in next_seq_for_update also serialises double-submits.

        THE TITLE IS WRITTEN HERE, NOT AT STEP 13, which only runs on a
        successful turn -- a turn failing after this commit otherwise left the
        conversation showing "New conversation / No messages yet" despite the
        user having said something. touch() COALESCEs, so the first message
        still wins.

        AN AUTOSTART TURN TIMESTAMPS BUT DOES NOT TITLE: its text is the UI's
        canned opener, which named every discussion identically.
        """
        seq = self._conversations.next_seq_for_update(conv.id)

        self._messages.insert(
            conv.id, seq, role="user", content=ctx_in.text,
            selected_option_id=ctx_in.option_id, request_id=ctx_in.request_id,
            actor=ctx_in.user.user_id,
        )

        if ctx_in.autostart:
            self._conversations.touch(conv.id)
            return

        self._conversations.touch(conv.id, title_from=_conversation_title(ctx_in.text))
        # If an autostart turn left a placeholder title behind, this is the
        # first thing the user has actually said -- promote it. touch()'s
        # COALESCE cannot do this on its own, and without it every discussion
        # would keep the generated name forever.
        self._conversations.replace_placeholder_title(
            conv.id, _conversation_title(ctx_in.text),
        )

    def _handle_turn_locked(self, ctx_in: TurnInput, conv) -> TurnResult:
        # 2/3/3b. seq under a row lock, the user message, and the title.
        with timing.stage("record_user_message"):
            self._record_user_message(ctx_in, conv)

        # 4. route (v2: 5 gates)
        #
        # History-aware so Gate 4 can use it, and read against the DEFAULT
        # agent's memory spec -- which agent will serve the turn is exactly what
        # routing has not decided yet, so the default's window is the only one
        # available. No session: routing is what determines the session.
        # `router_history` is its own stage because it is a SECOND history read
        # in the same turn -- step 7 reads it again against the serving agent's
        # window -- and the question of whether that duplication costs anything
        # should be answerable from a log line rather than argued about.
        with timing.stage("router_history"):
            router_history = self._router_history(conv)
        ctx_partial = self._turn_context(
            ctx_in, conv,
            history=router_history,
            session=None,
        )
        # Covers all five gates, so it includes the pin lookup a pinned turn
        # does. `router_llm` inside it is the part that only Gate 4 pays.
        with timing.stage("router"):
            decision = self._router.select(conv, ctx_partial, explicit_key=ctx_in.agent_key)
        agent = decision.agent

        # 4b. Resolve the agent for THIS caller's tenant/organization.
        # ONE PLACE, deliberately: `agent.spec` is read a dozen times below and
        # `agent.checksum` keys the handler cache, so resolving once here makes
        # all of it tenant-correct and leaves a single line to audit when asking
        # "can one tenant be served another's configuration?". A no-op, and one
        # indexed lookup, for a tenant that has customised nothing.
        turn_tenant_id, turn_organization_id = scope_for_user(ctx_in.user)
        with timing.stage("agent_resolve"):
            agent = self._registry.resolve_for_scope(
                self._db, agent, turn_tenant_id, turn_organization_id,
            )

        # 5. enforce limits
        with timing.stage("rate_limits"):
            self._rate_limits.check(conv.id, ctx_in.user, agent.spec.limits)

        # 6. Open the session -- which IS the pin. The unique index makes that
        #    row the single answer to "which agent is driving this
        #    conversation", so Gate 2 finds it next turn with no LLM call.
        #
        #    on_displace fires when the open session belongs to a DIFFERENT
        #    agent: it is abandoned, and its socket must go with it or the
        #    pool hands the new agent a channel still authenticated against the
        #    old remote session. That is how one conversation spans agents.
        with timing.stage("session_open"):
            session_view = self._sessions.open_for(
                conv.id, agent, on_displace=self._close_remote_channel,
                actor=ctx_in.user.user_id,
            )

        # 7. assemble history
        with timing.stage("agent_history"):
            history = self._messages.recent(conv.id, agent.spec.memory)

        # 8. COMMIT <- releases the lock
        with timing.stage("commit_pre_handler"):
            self._db.commit()

        # 9. handler.handle
        handler = self._handlers.build(agent.spec, agent.checksum)

        ctx = self._turn_context(
            ctx_in, conv,
            history=history,
            session=_to_session_view(session_view) if session_view else None,
        )


        try:
            # The TOTAL for the agent's own work. Its breakdown -- remote_turn,
            # remote_completion_poll, llm -- is recorded inside the handler, so
            # the difference between this and the sum of those is the handler's
            # own overhead, and should be near zero.
            with timing.stage("handler"):
                turn = handler.handle(ctx)
        except ProviderTurnTimeout as exc:
            # A timeout does NOT mean the turn failed -- it means we stopped
            # listening. Ask the provider what actually happened before surfacing an
            # error, because nothing else will: the late frame is discarded by
            # _drain_stale() on the next turn, so an unrecovered reply desyncs
            # the interview even if the user does nothing at all.
            with timing.stage("timeout_recovery"):
                turn = self._recover_timed_out_turn(
                    agent, session_view, ctx_in.text, exc, ctx_in.user,
                )
            if turn is None:
                raise

        # 10. apply session delta; finalise if terminal
        if turn.session_delta and session_view:
            session_view = self._sessions.apply(session_view, turn.session_delta)
        finalization = None
        if turn.terminal and session_view:
            # Only on the last turn of an interview, but it is a remote round
            # trip plus a report fetch, so the turn a user perceives as "the one
            # that hung" is often this one rather than a slow reply.
            with timing.stage("finalize"):
                finalization = self._finalize_claiming(session_view, agent, ctx_in.user)
            session_view = finalization.session

        # 11. persist the agent message
        options_dict = [o.__dict__ for o in turn.options] if turn.options else None
        # Stored so the documents come back when the conversation is reopened
        # from the sidebar or the page is reloaded -- GET .../messages replays
        # from the row, and anything not on it is unreachable afterwards.
        attachments_dict = [a.__dict__ for a in turn.attachments] if turn.attachments else None
        
        persist = timing.Stopwatch()
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
            attachments=attachments_dict,
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
        #      commit at step 13, and finalisation is a remote round trip.
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
        # Steps 11 through 13 as one number: the assistant row, the follow-up,
        # the tool traces and the closing commit. Measured with a Stopwatch
        # rather than a `with` block because it spans three numbered steps that
        # must not be re-indented -- the ordering comments between them are
        # load-bearing.
        timing.record("persist", persist.ms)

        timings = timing.current()

        # ONE line per completed turn, all fields, no interpolation -- the
        # pipeline previously logged only its failures, so "which agent, what
        # model, how long" was answerable only from the database.
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
                # The breakdown behind `latency_ms`. Nested rather than
                # flattened into a field each: JSONFormatter promotes every
                # `extra` key to the top level, and a stage set that varies by
                # agent type would give every turn a different log SCHEMA.
                #
                # Empty dicts when nothing recorded -- a handler exercised
                # outside a request has no accumulator, and the keys should
                # still be present so a consumer never has to test for them.
                "stages": timings.stages if timings else {},
                "counts": timings.counts if timings else {},
                "ws": timings.details if timings else {},
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
        
    # The advisory turn lock lives in app/services/turn_lock.py. These
    # delegations remain because handle_turn reads better naming the two halves
    # of the claim, and tests/guards/test_turn_concurrency.py drives them.

    def _try_lock_conversation(self, conversation_id: uuid.UUID) -> bool:
        return self._turn_lock.acquire(conversation_id)

    def _unlock_conversation(self, conversation_id: uuid.UUID) -> None:
        self._turn_lock.release(conversation_id)

    def _close_remote_channel(self, conversation_id: uuid.UUID) -> None:
        """Drop this conversation's pooled transport, in EVERY provider's pool.

        Best-effort: the DB change that prompted it is already
        committed-or-committing, and a socket that fails to close is reaped
        anyway. The registry swallows per-pool failures for the same reason.

        ACROSS ALL PROVIDERS, not one named platform's pool. This fires when a
        conversation switches agents, and the agent it switches AWAY from may
        belong to either platform -- so closing only one pool left the other's
        socket alive and still authenticated against the abandoned session.
        """
        if self._providers is None:
            return
        self._providers.close_conversation(conversation_id)

    # ------------------------------------------------------------------
    # Lost-turn recovery (see app/providers/recovery.py)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Finalisation and lost-turn recovery
    #
    # Both live in app/services/turn_finalization.py. These thin delegations
    # remain because the turn pipeline calls them mid-sequence and the names are
    # referenced from the numbered steps above.
    # ------------------------------------------------------------------

    def _reconcile(self, agent, session_view, sent_text: str, user) -> Optional[Reconciliation]:
        return self._finalizer.reconcile_turn(agent, session_view, sent_text, user)

    def _recover_timed_out_turn(self, agent, session_view, sent_text, exc, user):
        return self._finalizer.recover_timed_out_turn(agent, session_view, sent_text, exc, user)

    def _record_session_follow_up(
        self, conversation_id, agent_id, user, request_id=None,
    ) -> None:
        self._finalizer.record_session_follow_up(
            conversation_id, agent_id, user, request_id,
        )

    def _finalize(self, session_view, agent, user):
        return self._finalizer.finalize(session_view, agent, user)

    def _finalize_claiming(self, session_view, agent, user):
        return self._finalizer.finalize_claiming(session_view, agent, user)

    def resume_turn(self, session_id: uuid.UUID, user) -> Optional["ResumeResult"]:
        """Public entry point for POST /api/sessions/{id}/resume.

        The manual counterpart to _recover_timed_out_turn, for when automatic
        recovery came back PENDING (the platform was still generating) and the
        client is polling. Read-only: it never re-sends the user's turn.
        Returns None if no such session exists so the route can 404.
        """
        session_view = self._sessions.get(session_id)
        if session_view is None:
            return None

        agent = self.agent_for_session(session_view, user)
        last_user_text = self._messages.last_user_content(session_view.conversation_id)
        if agent is None or not last_user_text:
            return ResumeResult(outcome=TurnOutcome.NOT_DELIVERED, session=session_view)

        result = self._reconcile(agent, session_view, last_user_text, user)
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

