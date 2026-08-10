"""The Saathi agent handler.

Responsible for: running one turn against a Saathi deployment -- profile, terms
of use, remote session, socket, turn.
Used by: HandlerFactory, for any spec with agent_type="saathi_flow".

DIFFERS FROM RemoteFlowAgentHandler IN THREE PLACES, and only three:

  1. The profile is READ, not upserted. Saathi derives it from the access token
     (`/api/shikshalokam/read-elevate-profile/`), so there is no (email,
     company) pair to upsert and no email-derivation trap.
  2. There is a TERMS-OF-USE GATE. A profile that has not accepted them cannot
     converse, so the first turn accepts on the user's behalf.
  3. Nothing is finalised. `create_story: "none"` -- TurnFinalizer's
     no-artifact path completes the session instead.

Everything else -- the channel pool, the one re-establishment attempt, the
completion poll -- is Mitra's, because it is the same Django application.
"""
from __future__ import annotations

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
from app.core.logger import get_logger
from app.domain.agent_spec import SaathiFlowAgentSpec
from app.integrations.mitra.connection import resolve_connection
from app.integrations.mitra.exceptions import MitraChannelClosed

logger = get_logger(__name__)


@register_handler
class SaathiFlowAgentHandler:
    agent_type = "saathi_flow"

    def __init__(self, spec: SaathiFlowAgentSpec, deps: HandlerDeps):
        if deps.saathi_tokens is None or deps.saathi_sessions is None:
            raise RuntimeError(
                f"agent {spec.key!r} is agent_type='saathi_flow' but Saathi is not "
                "configured (check SAATHI_ENABLED and the credentials for "
                "SAATHI_LOGIN_MECHANISM)"
            )
        self._spec = spec
        self._remote = spec.remote
        self._tokens = deps.saathi_tokens
        self._sessions = deps.saathi_sessions
        self._conn = resolve_connection(deps.settings, spec.remote)

        from app.integrations.saathi.rest_client import SaathiRestClient

        self._rest = SaathiRestClient(
            base_url=self._conn.base_url,
            origin_url=self._conn.origin_url,
            user_agent=self._conn.user_agent,
            tokens=self._tokens,
            connect_timeout=self._conn.connect_timeout_s,
            read_timeout=self._conn.read_timeout_s,
        )

    def handle(self, ctx: TurnContext) -> AgentTurn:
        t0 = timing.start()

        if ctx.session is None:
            raise RuntimeError(
                f"agent {self._spec.key!r}: handle() called with no prior session row -- "
                "SessionService.open_for must run first for a pin_session agent"
            )

        is_first_turn = ctx.session.remote_session_id is None
        bot_route = self._remote.bot_route

        if is_first_turn:
            # 1. The profile comes FROM THE TOKEN. Saathi's consumer overwrites
            #    whatever `profileid` the authenticate frame carries, so this is
            #    read for the terms gate and for recent_chat's direction
            #    detection -- not to tell Saathi who is connecting.
            profile_id, accepted_tnc = self._rest.read_profile()

            # 2. THE TERMS GATE. An unaccepted profile cannot converse, and the
            #    failure is not obvious from the bot's reply.
            if not accepted_tnc:
                self._rest.accept_tnc(profile_id)
                logger.info(
                    "saathi: accepted terms of use for profile",
                    extra={"profile_id": profile_id, "agent_key": self._spec.key},
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
            # Exactly ONE re-establishment attempt, matching the Mitra handler.
            # A closed socket is also how a rejected token presents, and
            # reconnecting re-reads the token provider -- so an expired session
            # heals here without the caller knowing.
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
