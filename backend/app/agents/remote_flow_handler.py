"""RemoteFlowAgentHandler -- one turn, delegated to whichever external platform
this agent's configuration names.

Responsible for: the shape of a delegated turn, in terms the core understands.
Used by: HandlerFactory, for any spec with agent_type="remote_flow".

KNOWS NO PLATFORM. There is no branch on `remote.provider` here and there must
never be one: the provider is resolved once from the registry, and the three
things that differ between platforms -- how a remote session comes into being,
what authenticates the socket, whether anything is finalised -- are all behind
that object. There used to be a second, near-identical handler for a second
platform; the only differences between them were those three, and all three are
now provider methods.

DOES NOT FINALIZE. Finalisation runs under the `finalizing` state claim, which
only the transaction owner (the service layer) can make. This handler signals
`terminal=True` and stops.

DOES NOT SEND HISTORY. `spec.memory.strategy` is `none` for delegated flows: a
platform that reconstructs conversation state from its own records is corrupted
by being handed history. This handler never reads `ctx.history` at all, which is
what structurally guarantees that.
"""
import dataclasses

from app.agents.factory import HandlerDeps, register_handler
from app.agents.protocol import (
    AgentTurn,
    Attachment,
    Option,
    SessionDelta,
    SessionState,
    TurnContext,
)
from app.core import timing
from app.domain.agent_spec import RemoteFlowAgentSpec


@register_handler
class RemoteFlowAgentHandler:
    agent_type = "remote_flow"

    def __init__(self, spec: RemoteFlowAgentSpec, deps: HandlerDeps):
        if deps.providers is None:
            raise RuntimeError(
                f"agent {spec.key!r} is agent_type='remote_flow' but no provider "
                "registry was supplied to HandlerDeps"
            )
        self._spec = spec
        self._remote = spec.remote

        # RESOLVED ONCE, HERE, AND THAT IS THE WHOLE POINT.
        #
        # HandlerFactory caches handlers by `(spec.key, checksum)`, and a
        # tenant-scoped config is a whole agent_configs row with its own
        # checksum. So one handler instance corresponds to exactly one resolved
        # spec, and therefore to exactly one provider bound to exactly one
        # connection -- resolving it in the constructor makes it per-tenant by
        # construction rather than by remembering to pass a scope down every
        # call path.
        #
        # Raises ProviderNotEnabled / UnknownProvider / ProviderConfigError,
        # each of which the routers already map to an upstream error. That
        # replaced two hand-written "is this platform configured?" constructor
        # guards, one per platform.
        self._provider = deps.providers.get(spec.remote)

    def handle(self, ctx: TurnContext) -> AgentTurn:
        t0 = timing.start()

        if ctx.session is None:
            raise RuntimeError(
                f"agent {self._spec.key!r}: handle() called with no prior session row -- "
                "SessionService.open_for must run first for a pin_session agent"
            )

        is_first_turn = ctx.session.remote_session_id is None

        # The provider decides whether this creates a remote session or simply
        # reports the existing one, so "first turn" never becomes a branch here.
        # It re-supplies `remote_bot_route` every turn, straight off this
        # tenant's scoped config, so a configuration change takes effect on the
        # next turn without an environment lookup.
        # THREE SEQUENTIAL REMOTE CALLS, TIMED SEPARATELY. `latency_ms` below
        # covers all three, which made them indistinguishable: a turn recorded
        # as five seconds could have been a five-second reply, or a two-second
        # reply behind a three-second completion poll. Only the first is the
        # provider being slow; the second is work on the critical path that the
        # user's answer did not depend on.
        with timing.stage("remote_open_session"):
            init = self._provider.open_session(self._remote, ctx.session, ctx.user)
        sess = dataclasses.replace(
            ctx.session,
            remote_session_id=init.remote_session_id,
            remote_profile_id=init.remote_profile_id,
            remote_bot_route=init.remote_bot_route,
        )

        # Not wrapped in a stage of its own: the provider splits this into
        # `remote_acquire` and `remote_turn`, which is the distinction worth
        # having, and a third enclosing stage would double-count both.
        bot = self._provider.turn(
            self._remote, sess, ctx.text, ctx.user, first_turn=is_first_turn,
        )

        with timing.stage("remote_completion_poll"):
            check = self._provider.is_complete(self._remote, sess, ctx.user)

        return AgentTurn(
            text=bot.text,
            options=[Option(o.id, o.label, o.value) for o in bot.options],
            attachments=[
                Attachment(a.file_name, a.format, a.media_type, a.url)
                for a in bot.attachments
            ],
            session_delta=SessionDelta(
                # ALWAYS awaiting_user here, even when done=True. The
                # in_progress/awaiting_user -> finalizing transition belongs
                # exclusively to TurnFinalizer's own claim_finalizing() call. If
                # this delta set state=finalizing itself, handle_turn would apply
                # that BEFORE finalisation runs, so claim_finalizing() -- whose
                # claimable set is only {in_progress, awaiting_user} -- would
                # find the session already in 'finalizing' and fail to claim it.
                # The session would then be stuck there forever: no code path
                # ever revisits it, and the real end-of-flow call never fires.
                # `terminal=check.done` below is the only signal the orchestrator
                # needs.
                state=SessionState.awaiting_user,
                remote_session_id=sess.remote_session_id,
                remote_profile_id=sess.remote_profile_id,
                remote_flow=self._remote.flow_name,
                remote_bot_route=sess.remote_bot_route,
                step=bot.step,
                state_data=self._merged_state_data(sess, check.state_data),
            ),
            latency_ms=timing.elapsed_ms(t0),
            # `.done`, NEVER the object. `CompletionCheck` is a dataclass and so
            # always truthy; `terminal=check` would end every interview on its
            # first turn.
            terminal=check.done,
        )

    @staticmethod
    def _merged_state_data(sess, patch):
        """This session's `state_data` with the provider's patch laid over it.

        A MERGE, NOT AN ASSIGNMENT. `SessionService.apply` writes `state_data`
        wholesale when the delta carries one, so returning the patch alone would
        silently drop every other key the column holds. Returning None when there
        is no patch leaves the column untouched, which is the same rule every
        other optional field on `SessionDelta` follows.

        OPAQUE. The patch's contents are the provider's own cache format and are
        not read here -- `app.agents` may not know which platform it is talking
        to, let alone how that platform paginates.
        """
        if not patch:
            return None
        return {**(getattr(sess, "state_data", None) or {}), **patch}
