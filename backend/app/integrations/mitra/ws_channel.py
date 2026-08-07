"""MitraChannel -- one socket, one daemon reader thread, one bounded frame queue.

Implements the connection lifecycle described in the design doc (§7.3,
§1.1): connect with the Origin/User-Agent Mitra gates on, send the
`authenticate` frame, then settle for `handshake.settle_ms` WITHOUT waiting
for an acknowledgement -- verified (§1.1) that Mitra never sends one on any
path. `send_and_await_turn` accumulates chunked bot replies (§1.3), discards
the user-echo frame Mitra sends back before dispatching the real reply
(§1.2), and falls back to whatever was accumulated so far when fragments
stop arriving (the idle-gap backstop), rather than hanging until the full
turn timeout.

Deliberately NOT part of this module: MitraSessionManager (the
conversation_id -> MitraChannel LRU map, design doc §7.4). That's a
separate class with its own re-authentication/eviction concerns.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import websocket

from app.integrations.mitra.exceptions import (
    MitraChannelClosed,
    MitraRemoteError,
    MitraTurnTimeout,
    MitraConcurrentTurnError,
)
from app.integrations.mitra.frame_parser import Frame, ParsedOption, parse
from app.core.logger import get_logger

logger = get_logger("mitra_ws_channel")

# Shown when a turn contained nothing but a leaked internal payload. Mitra has
# its own wording for the equivalent situation (guided_guest_tool_call.py:59,
# "I am sorry, I could not understood completely...") -- same intent, fixed
# grammar, since this string is user-visible.
UNREADABLE_TURN_MESSAGE = (
    "Sorry, I didn't quite catch that. Could you say it again?"
)


@dataclass(frozen=True)
class BotTurn:
    """The result of one send_and_await_turn() call."""
    text: str
    options: List[ParsedOption] = field(default_factory=list)
    step: Optional[int] = None


class MitraChannel:
    def __init__(
        self,
        spec,           # app.domain.agent_spec.RemoteSpec
        sess,           # app.agents.protocol.AgentSessionView
        conn,           # app.integrations.mitra.connection.MitraConnection
        ws_factory: Callable[[], "websocket.WebSocket"] = websocket.WebSocket,
    ):
        self._q: "queue.Queue[Frame]" = queue.Queue(maxsize=512)
        self._pending: Optional[Frame] = None  # settling pushback slot, §7.3
        self._closed = threading.Event()
        self._close_reason = ""
        self._turn_lock = threading.Lock()

        # The connection this socket was opened against. MitraSessionManager
        # compares it on every acquire: a pooled channel whose scope has since
        # been reconfigured is treated as dead and reconnected, which is what
        # makes a config change take effect without a restart.
        self.conn_checksum: str = conn.checksum

        self._ws = ws_factory()
        self._ws.connect(
            conn.ws_url,
            origin=conn.origin_url,  # Mitra gates on Origin -- §13.2
            header=[
                f"User-Agent: {conn.user_agent}",
                "Accept-Language: en-GB,en-US;q=0.9,en;q=0.8",
            ],
            timeout=conn.ws_connect_timeout_s,
            enable_multithread=True,
        )

        self._reader = threading.Thread(
            target=self._read_loop, daemon=True, name=f"mitra-ws-{sess.id}"
        )
        self._reader.start()

        self._authenticate(spec, sess, conn)

    # ------------------------------------------------------------------
    # Reader thread
    # ------------------------------------------------------------------

    def _read_loop(self) -> None:
        try:
            while True:
                raw = self._ws.recv()
                if raw == "":
                    break
                try:
                    self._q.put(parse(raw), timeout=1)
                except queue.Full:
                    # Sustained consumer stall past the bounded queue size --
                    # treat as fatal rather than blocking the reader forever.
                    self._close_reason = "frame queue full; consumer stalled"
                    break
        except Exception as e:
            self._close_reason = str(e)
        finally:
            self._closed.set()

    def _try_get(self, timeout: float) -> Optional[Frame]:
        if self._pending is not None:
            f, self._pending = self._pending, None
            return f
        if timeout <= 0:
            return None
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def _pushback(self, frame: Frame) -> None:
        self._pending = frame

    def _drain_stale(self) -> None:
        """Drop leftover frames (stale echoes) sitting in the queue before a
        new turn starts. Deliberately does NOT touch self._pending -- a bot
        frame pushed back during settling still belongs to the upcoming
        (possibly first) turn."""
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break

    # ------------------------------------------------------------------
    # Establishment
    # ------------------------------------------------------------------

    def _authenticate(self, spec, sess, conn) -> None:
        self._ws.send(json.dumps({
            "type": "authenticate",
            "sessionid": sess.remote_session_id,
            "profileid": sess.remote_profile_id,
            "projectid": "",
            "taskid": None,
            "access_token": None,
            "route": sess.language,
            "bot_route": sess.remote_bot_route,
            "flow_name": spec.flow_name,
            "address": {
                "ipCity": conn.ip_city,
                "ipState": conn.ip_state,
                "ipZipCode": conn.ip_zip,
            },
        }))

        # §1.1 -- Mitra never acks. Wait for one if it ever appears
        # (defensive, for a future Mitra release), otherwise just settle.
        deadline = time.monotonic() + spec.handshake.settle_ms / 1000
        # Poll in slices rather than blocking for the whole settle window, so a
        # socket that dies mid-handshake is noticed within ~50ms instead of only
        # at the next loop iteration.
        POLL_SLICE_S = 0.05
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # RE-CHECK before declaring the handshake settled. The closed
                # check below only ran while time remained, so a connection that
                # dropped during settling took this branch and the channel was
                # handed back as if authenticated -- the first
                # send_and_await_turn then wrote to a dead socket. The reader
                # thread sets _closed asynchronously, so which of the two
                # branches saw it first was pure timing; the test for this was
                # correspondingly flaky rather than reliably red.
                if self._closed.is_set():
                    raise MitraChannelClosed(self._close_reason)
                return
            if self._closed.is_set():
                raise MitraChannelClosed(self._close_reason)
            f = self._try_get(timeout=min(remaining, POLL_SLICE_S))
            if f is None:
                continue
            if f.source in spec.handshake.ack_types:
                return
            if f.source == "bot":
                self._pushback(f)  # the bot opened first -- keep it for the turn
                return

    # ------------------------------------------------------------------
    # Turns
    # ------------------------------------------------------------------

    def send_and_await_turn(self, text: str, timeout_s: float, idle_gap_s: float) -> BotTurn:
        if not self._turn_lock.acquire(blocking=False):
            raise MitraConcurrentTurnError()
        try:
            self._drain_stale()  # leftover echoes, §1.2

            self._ws.send(json.dumps({"type": "message", "text": text, "context": "", "asr_audio": None}))

            chunks: List[str] = []
            options: List[ParsedOption] = []
            step: Optional[int] = None
            saw_control_payload = False
            heard_from_bot = False
            deadline = time.monotonic() + timeout_s
            last_rx = time.monotonic()

            while True:
                if self._closed.is_set():
                    raise MitraChannelClosed(self._close_reason)

                # THE IDLE GAP ONLY APPLIES ONCE THE BOT HAS STARTED SPEAKING.
                #
                # It is a "fragments stopped arriving, flush what we have"
                # backstop (§1.3) -- a gap BETWEEN fragments. Applying it from
                # send time instead turned it into a hard deadline on Mitra's
                # FIRST token, far stricter than the configured turn timeout:
                # with idle_gap_ms=8000 / turn_timeout_ms=45000, a reply that
                # took 8.4s raised MitraTurnTimeout at 19% of the real budget.
                # Verified against Mitra's own CompanyChat rows -- the turn had
                # actually succeeded upstream; only this side stopped listening,
                # and the user's Retry then re-sent the answer into the NEXT
                # question. Most turns land in 2-6s, which is why it presented
                # as intermittent.
                #
                # Note `last_rx` is only advanced for source == "bot" below, so
                # the user-echo Mitra sends back immediately (§1.2) does not
                # extend the window -- the old expression really was measuring
                # from send time.
                remaining = deadline - time.monotonic()
                if heard_from_bot:
                    remaining = min(remaining, last_rx + idle_gap_s - time.monotonic())

                if remaining <= 0:
                    # saw_control_payload: we DID hear from Mitra, its frame was
                    # just unusable (§Defect 4). Falling through to the
                    # re-prompt below is honest; "the request timed out" is not.
                    if chunks or saw_control_payload:
                        break  # idle-gap (or timeout) flush
                    raise MitraTurnTimeout(step=step)

                f = self._try_get(timeout=remaining)
                if f is None:
                    continue
                if f.source == "user":
                    continue  # §1.2 ECHO -- DISCARD
                if f.source == "system" and f.error:
                    raise MitraRemoteError(f.msg or f.error)
                if f.source != "bot":
                    continue

                last_rx = time.monotonic()
                heard_from_bot = True
                if f.control_payload:
                    # Mitra leaked an internal LLM object instead of a reply
                    # (frame_parser §Defect 4). The parser has already stripped
                    # it down to whatever was user-facing; log it, because it
                    # means Mitra dropped a turn and only this side can see it.
                    saw_control_payload = True
                    logger.warning(
                        "Mitra sent an internal control payload as bot text "
                        "(step=%s, recovered=%r) -- see frame_parser Defect 4",
                        f.step, f.msg[:120],
                    )
                if f.msg:
                    chunks.append(f.msg)  # §1.3 ACCUMULATE
                if f.step is not None:
                    step = f.step
                if f.options:
                    options = f.options
                if f.finish_reason:
                    break  # §1.3 END OF TURN

            text_out = "".join(chunks)
            if not text_out and saw_control_payload:
                # The leaked payload was ALL the turn contained, so there is
                # nothing to show. An empty bubble is worse than saying so, and
                # Mitra does not advance current_step on this path -- the next
                # answer is processed against the same step, so re-prompting
                # genuinely recovers the interview.
                text_out = UNREADABLE_TURN_MESSAGE

            return BotTurn(text=text_out, options=options, step=step)
        finally:
            self._turn_lock.release()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def alive(self) -> bool:
        return not self._closed.is_set()

    def close(self, quiet: bool = False) -> None:
        self._closed.set()
        try:
            self._ws.close()
        except Exception:
            pass
        if not quiet:
            logger.warning(
                "MitraChannel closed", extra={"close_reason": self._close_reason},
            )
