"""RemoteFlowAgentHandler -- ties MitraRestClient, MitraChannel, and
MitraSessionManager together into an AgentHandler (design doc §7.5).

DOES NOT finalize. `end-story/v2` plus report retrieval must run under the
`finalizing` state claim (§4.7), which only the transaction owner (the
service layer) can make -- this handler only signals `terminal=True` via the
returned AgentTurn/SessionDelta.

DOES NOT send history. `spec.memory.strategy` is `none` for delegated flows:
Mitra reconstructs interview state from its own records
(ChatSession.current_step, CompanyChat rows); supplying history would
corrupt it. This handler never reads `ctx.history` at all, which is what
structurally guarantees that.
"""
import dataclasses

from app.agents.factory import HandlerDeps, register_handler
from app.agents.protocol import (
    AgentTurn,
    Option,
    SessionDelta,
    SessionState,
    TurnContext,
)
from app.core import timing
from app.domain.agent_spec import RemoteFlowAgentSpec
from app.integrations.mitra.connection import resolve_connection
from app.integrations.mitra.exceptions import MitraChannelClosed


@register_handler
class RemoteFlowAgentHandler:
    agent_type = "remote_flow"

    def __init__(self, spec: RemoteFlowAgentSpec, deps: HandlerDeps):
        if deps.mitra_clients is None or deps.mitra_sessions is None:
            raise RuntimeError(
                f"agent {spec.key!r} is agent_type='remote_flow' but Mitra is not configured "
                "(mitra_clients/mitra_sessions are None -- check MITRA_ENABLED and MITRA_BASE_URL)"
            )
        self._spec = spec
        self._remote = spec.remote
        self._sessions = deps.mitra_sessions

        # RESOLVED ONCE, HERE, AND THAT IS THE WHOLE POINT.
        #
        # HandlerFactory caches handlers by `(spec.key, checksum)`, and a
        # tenant-scoped config is a whole agent_configs row with its own
        # checksum. So one handler instance corresponds to exactly one resolved
        # spec, and therefore to exactly one connection -- resolving it in the
        # constructor makes it per-tenant by construction rather than by
        # remembering to pass a scope down every call path.
        self._conn = resolve_connection(deps.settings, spec.remote)
        self._rest = deps.mitra_clients.get(self._conn)

    def handle(self, ctx: TurnContext) -> AgentTurn:
        t0 = timing.start()

        if ctx.session is None:
            raise RuntimeError(
                f"agent {self._spec.key!r}: handle() called with no prior session row -- "
                "SessionService.open_for must run first for a pin_session agent"
            )

        is_first_turn = ctx.session.remote_session_id is None
        # Straight off the spec -- which is this tenant's scoped config, because
        # the handler was built from it. No environment lookup: that is what
        # used to make one process serve exactly one Mitra company.
        bot_route = self._remote.bot_route

        # ---- FIRST TURN: create the remote session ----
        if is_first_turn:
            profile_id = self._rest.upsert_profile(
                email=ctx.user.email,
                latest_flow_used=self._remote.flow_name,
                company=self._remote.company,
            )
            remote_sid = self._rest.generate_session()
            sess = dataclasses.replace(
                ctx.session,
                remote_session_id=remote_sid,
                remote_profile_id=profile_id,
                remote_bot_route=bot_route,
            )
        else:
            sess = dataclasses.replace(ctx.session, remote_bot_route=bot_route)

        timeout_s = (
            self._remote.turn.first_turn_timeout_ms if is_first_turn
            else self._remote.turn.turn_timeout_ms
        ) / 1000
        idle_gap_s = self._remote.turn.idle_gap_ms / 1000

        ch = self._sessions.acquire(self._remote, sess, self._conn)
        try:
            bot = ch.send_and_await_turn(ctx.text, timeout_s, idle_gap_s)
        except MitraChannelClosed:
            # Exactly ONE re-establishment attempt -- not a general retry.
            # Re-sending a turn Mitra already recorded would trigger its
            # consecutive-same-sender merge (common_chat_tasks.py:32-45) and
            # silently destroy an answer, so a second failure here propagates.
            ch = self._sessions.reacquire(self._remote, sess, self._conn)
            bot = ch.send_and_await_turn(ctx.text, timeout_s, idle_gap_s)

        done = (
            self._remote.completion_poll_every_turn
            and self._rest.is_session_completed(sess.remote_session_id)
        )

        return AgentTurn(
            text=bot.text,
            options=[Option(o.id, o.label, o.value) for o in bot.options],
            session_delta=SessionDelta(
                # ALWAYS awaiting_user here, even when done=True. The
                # in_progress/awaiting_user -> finalizing transition belongs
                # exclusively to OrchestrationService._finalize's own
                # claim_finalizing() call (design doc §4.7's idempotency
                # guard). If this delta set state=finalizing itself, step 11
                # in handle_turn() would apply that BEFORE _finalize() runs,
                # so claim_finalizing() -- whose claimable set is only
                # {in_progress, awaiting_user} -- would find the session
                # already in 'finalizing' and fail to claim it. The session
                # would then be stuck in 'finalizing' forever: no code path
                # ever revisits it, and Mitra's real end-story call never
                # fires. `terminal=done` below is the only signal
                # OrchestrationService needs to call _finalize() next.
                state=SessionState.awaiting_user,
                remote_session_id=sess.remote_session_id,
                remote_profile_id=sess.remote_profile_id,
                remote_flow=self._remote.flow_name,
                remote_bot_route=bot_route,
                step=bot.step,
            ),
            latency_ms=timing.elapsed_ms(t0),
            terminal=done,
        )

