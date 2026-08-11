"""The shape every JSON-framed WebSocket conversational provider shares.

Responsible for: the turn loop, the channel lifecycle, completion polling and
read-only reconciliation -- everything that follows from the PROTOCOL rather
than from any one platform.
Used by: `app/providers/mitra/` and `app/providers/saathi/`, as peers. Neither
inherits from the other.

WHAT A SUBCLASS SUPPLIES, and nothing more:

  name / produces_artifacts / options_model   its identity and its config schema
  _build_rest()                               its own REST surface
  open_session()                              how a remote session comes into being
  _access_token()                             None for a guest socket, a JWT otherwise
  option_readers                              any choice shape unique to it

Everything else below is protocol behaviour. A platform that later stops running
this protocol stops inheriting from here; it does not drag the other one with it.

FINALISATION IS NOT IMPLEMENTED HERE. A provider that produces no artifact must
not silently no-op a finalize call -- `TurnFinalizer` decides not to call it, and
if it ever did, failing loudly is the honest outcome.
"""
from __future__ import annotations

from functools import partial
from typing import Any, ClassVar, Optional, Sequence, Tuple
from uuid import UUID

import websocket

from app.core.logger import get_logger
from app.providers.errors import ProviderChannelClosed, ProviderConfigError
from app.providers.protocol import FinalizeResult, ProviderTurn, SessionInit
from app.providers.recovery import Reconciliation, reconcile
from app.providers.transport.frames import Attachment
from app.providers.transport.http import url_is_permitted
from app.providers.transport.ws import WsChannel
from app.providers.ws_flow.frames import AttachmentReader, OptionReader, parse

logger = get_logger("ws_flow")

#: Sent alongside the User-Agent on the socket upgrade. Part of looking like the
#: browser client these servers were built for.
ACCEPT_LANGUAGE = "Accept-Language: en-GB,en-US;q=0.9,en;q=0.8"


class BaseWsFlowProvider:
    """A conversation held over one pooled, JSON-framed WebSocket."""

    # ---- declared by the subclass -------------------------------------
    name: ClassVar[str]
    options_model: ClassVar[type]

    # ---- protocol-family facts ----------------------------------------
    #: Sockets live in process memory, so any deployment enabling one of these
    #: is pinned to a single worker. app/core/runtime.py reads this.
    stateful_transport: ClassVar[bool] = True
    #: The REST surface can be asked what happened to a turn we stopped
    #: listening for, so a timeout is reconciled rather than surfaced.
    supports_recovery: ClassVar[bool] = True
    #: Overridden by a platform that can finalise into a downloadable artifact.
    produces_artifacts: ClassVar[bool] = False
    #: Choice shapes unique to the subclass's platform, tried before the shared
    #: ones. Empty here: the shared parser already covers this family.
    option_readers: ClassVar[Sequence[OptionReader]] = ()
    #: The same seam for downloadable documents. Empty here: the shared parser
    #: already understands `extra_content.download`, which is this family's
    #: shape rather than any one platform's.
    attachment_readers: ClassVar[Sequence[AttachmentReader]] = ()

    #: How a socket is created. Overridden ONLY by tests, which is why it is an
    #: instance attribute rather than a constructor argument threaded through
    #: the registry: the suite runs with `--disable-socket`, so a channel test
    #: has to substitute one, and the alternative was for every layer between
    #: here and the test to carry a parameter production never sets.
    ws_factory = websocket.WebSocket

    def __init__(self, conn, pool) -> None:
        self._conn = conn
        self._pool = pool
        #: `remote.options` as this provider's own validated model. The registry
        #: parsed it before the connection was built, so it is typed by here.
        self.options = conn.options
        self._rest = self._build_rest()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        # No credentials: the connection holds them and is repr-safe itself.
        return f"{type(self).__name__}(base_url={self._conn.base_url!r})"

    # ------------------------------------------------------------------
    # Subclass hooks
    # ------------------------------------------------------------------

    def _build_rest(self) -> Any:
        raise NotImplementedError

    def open_session(self, remote, session_view, user) -> SessionInit:
        raise NotImplementedError

    def _access_token(self) -> Optional[str]:
        """The token to put in the authenticate frame.

        None means a guest socket, which is not a degraded mode: a server that
        derives `auth = access_token is not None` picks a different template
        from it, so sending one where the platform expects a guest renders blank
        output rather than failing.
        """
        return None

    # ------------------------------------------------------------------
    # The channel
    # ------------------------------------------------------------------

    def _handshake_frame(self, remote, sess) -> dict:
        """The authenticate frame this family's consumer expects."""
        return {
            "type": "authenticate",
            "sessionid": sess.remote_session_id,
            "profileid": sess.remote_profile_id,
            "projectid": "",
            "taskid": None,
            "access_token": self._access_token(),
            "route": sess.language,
            "bot_route": sess.remote_bot_route,
            "flow_name": remote.flow_name,
            "address": {
                "ipCity": self.options.ip_city,
                "ipState": self.options.ip_state,
                "ipZipCode": self.options.ip_zip,
            },
        }

    def _turn_frame(self, text: str) -> dict:
        return {"type": "message", "text": text, "context": "", "asr_audio": None}

    def _new_channel(self, remote, sess) -> WsChannel:
        conn = self._conn
        return WsChannel(
            url=conn.stream_url,
            origin=conn.origin_url,
            headers=[f"User-Agent: {conn.user_agent}", ACCEPT_LANGUAGE],
            connect_timeout_s=conn.stream_connect_timeout_s,
            handshake_frame=self._handshake_frame(remote, sess),
            turn_frame=self._turn_frame,
            parse=partial(
                parse,
                option_readers=self.option_readers,
                attachment_readers=self.attachment_readers,
            ),
            settle_ms=self.options.handshake.settle_ms,
            ack_types=self.options.handshake.ack_types,
            conn_checksum=conn.checksum,
            thread_name=f"{self.name}-ws-{sess.id}",
            ws_factory=self.ws_factory,
        )

    # ------------------------------------------------------------------
    # RemoteProvider
    # ------------------------------------------------------------------

    def turn(self, remote, session_view, text: str, *, first_turn: bool) -> ProviderTurn:
        timeout_s = (
            remote.turn.first_turn_timeout_ms if first_turn
            else remote.turn.turn_timeout_ms
        ) / 1000
        idle_gap_s = remote.turn.idle_gap_ms / 1000

        def factory():
            return self._new_channel(remote, session_view)

        ch = self._pool.acquire(session_view, self._conn, factory)
        try:
            bot = ch.send_and_await_turn(text, timeout_s, idle_gap_s)
        except ProviderChannelClosed:
            # Exactly ONE re-establishment attempt -- not a general retry.
            # Re-sending a turn the platform already recorded would trigger its
            # consecutive-same-sender merge and silently destroy an answer, so a
            # second failure here propagates.
            #
            # For a per-user platform a closed socket is also how a rejected
            # token presents, and reconnecting re-reads the token provider -- so
            # an expired session heals here without the caller knowing.
            ch = self._pool.reacquire(session_view, self._conn, factory)
            bot = ch.send_and_await_turn(text, timeout_s, idle_gap_s)

        return ProviderTurn(
            text=bot.text,
            options=list(bot.options),
            attachments=self._permitted_attachments(bot.attachments),
            step=bot.step,
        )

    def _permitted_attachments(self, attachments) -> list:
        """Drop any download URL this connection is not allowed to surface.

        DROPPED, NOT RAISED. The same rule the REST transport enforces before
        FETCHING a URL (https + exact host allowlist) applies to one we are
        about to put in front of a user -- but the policy differs: the turn's
        reply text is still worth showing, so one bad link costs one button
        rather than the whole turn. That mirrors how a failed artifact fetch is
        already non-fatal at the end of an interview.

        The host is logged and the full URL is not, matching ProviderSSRFError's
        rule that an SSRF payload must not reach a log line through an error
        message.
        """
        if not attachments:
            return []

        allowed = self._conn.allowed_hosts
        kept, dropped = [], []
        for attachment in attachments:
            if url_is_permitted(attachment.url, allowed):
                kept.append(attachment)
            else:
                dropped.append(attachment)

        if dropped:
            from urllib.parse import urlparse

            logger.warning(
                "%s: dropping %d download link(s) whose URL is not permitted "
                "(https + remote.allowed_hosts). Add the host to this agent's "
                "remote.allowed_hosts if it is legitimate.",
                self.name, len(dropped),
                extra={
                    "provider": self.name,
                    "formats": sorted(a.format for a in dropped),
                    # HOST ONLY, never the URL.
                    "hosts": sorted(
                        {(urlparse(a.url).hostname or "?") for a in dropped}
                    ),
                    "allowed_hosts": sorted(allowed),
                },
            )
        return kept

    def is_complete(self, remote, session_view) -> bool:
        if not self.options.completion_poll_every_turn:
            return False
        if not session_view.remote_session_id:
            return False
        return bool(self._rest.is_session_completed(session_view.remote_session_id))

    def reconcile(self, remote, session_view, sent_text: str) -> Optional[Reconciliation]:
        """READ-ONLY. Never re-sends -- see app/providers/recovery.py."""
        if session_view is None:
            return None
        if not session_view.remote_session_id or not session_view.remote_profile_id:
            return None
        rows = self._rest.recent_chat(
            session_view.remote_session_id, session_view.remote_profile_id,
        )
        return reconcile(rows, sent_text)

    def close_channel(self, conversation_id: UUID) -> None:
        if self._pool is not None:
            self._pool.close(conversation_id)

    # ------------------------------------------------------------------
    # Finalisation -- overridden only by a platform that produces artifacts
    # ------------------------------------------------------------------

    def finalize(self, remote, session_view, user) -> FinalizeResult:
        raise ProviderConfigError(
            f"provider {self.name!r} produces no artifact and cannot finalise. "
            "Set remote.produces_artifact to false on this agent's configuration."
        )

    def fetch_artifact(self, remote, session_view) -> Optional[str]:
        return None

    def validate_config(self, remote, settings) -> Optional[Tuple[list, str]]:
        """Nothing beyond the options model to check by default.

        A platform whose configuration has a failure mode the schema cannot
        express overrides this -- see `app/providers/mitra/provider.py`.
        """
        return None
