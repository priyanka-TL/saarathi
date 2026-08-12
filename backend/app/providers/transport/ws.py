"""One conversation's JSON-framed WebSocket.

Responsible for: the handshake, sending a turn, and awaiting the reply frames.
Used by: ChannelPool, which holds one channel per conversation.

A daemon reader thread feeds a bounded Queue, so `send_and_await_turn` blocks on
the queue rather than on the socket. That is why this must run on a real thread
and why the app has no `async def` endpoints.

NOTHING HERE KNOWS A PLATFORM. The handshake payload, the turn payload and the
frame parser are all supplied by the provider; what stays here is the behaviour
that is expensive to rediscover and silent when got wrong:

  * the settle window, for a server that never acknowledges a handshake;
  * discarding the user-echo the server sends straight back;
  * accumulating chunks until `finish_reason`;
  * the idle-gap backstop, and the rule that it only starts once the bot has
    actually begun speaking;
  * treating a leaked internal payload as "heard, but unusable" rather than as
    a timeout.

Each of those is annotated below with the failure it prevents.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from typing import Callable, List, Mapping, Optional, Sequence

import websocket

from app.core.logger import get_logger
from app.providers.errors import (
    ProviderChannelClosed,
    ProviderConcurrentTurnError,
    ProviderRemoteError,
    ProviderTurnTimeout,
)
from app.providers.transport.frames import Attachment, BotTurn, Frame, ParsedOption

logger = get_logger("provider_ws_channel")

# Shown when a turn contained nothing but a leaked internal payload. This string
# is user-visible, so it is fixed here rather than left to a provider.
UNREADABLE_TURN_MESSAGE = (
    "Sorry, I didn't quite catch that. Could you say it again?"
)

#: How finely the settle window is polled. Small enough that a socket dying
#: mid-handshake is noticed promptly, large enough not to spin.
POLL_SLICE_S = 0.05


class WsChannel:
    """A pooled, single-turn-at-a-time WebSocket for one conversation.

    :param handshake_frame: the first payload sent after connecting. The
        provider builds it; this class only serialises and sends it.
    :param turn_frame: builds the payload for one user turn.
    :param parse: turns a raw inbound message into a normalised Frame. MUST NOT
        raise -- an exception in the reader thread kills the socket.
    :param ack_types: frame `source` values that count as a handshake ack.
    :param conn_checksum: the connection this socket was opened against. The
        pool compares it on every acquire, so a scope whose configuration
        changed reconnects on the next turn rather than whenever the reaper
        happens to get to it.
    """

    def __init__(
        self,
        *,
        url: str,
        origin: str,
        headers: Sequence[str],
        connect_timeout_s: float,
        handshake_frame: Mapping[str, object],
        turn_frame: Callable[[str], Mapping[str, object]],
        parse: Callable[[object], Frame],
        settle_ms: int,
        ack_types: Sequence[str],
        conn_checksum: str,
        thread_name: str = "provider-ws",
        ws_factory: Callable[[], "websocket.WebSocket"] = websocket.WebSocket,
    ) -> None:
        self._q: "queue.Queue[Frame]" = queue.Queue(maxsize=512)
        self._pending: Optional[Frame] = None  # settling pushback slot
        self._closed = threading.Event()
        self._close_reason = ""
        self._turn_lock = threading.Lock()

        self._parse = parse
        self._turn_frame = turn_frame
        self.conn_checksum = conn_checksum

        self._ws = ws_factory()
        self._ws.connect(
            url,
            origin=origin,  # providers gate admission on Origin
            header=list(headers),
            timeout=connect_timeout_s,
            enable_multithread=True,
        )

        self._reader = threading.Thread(
            target=self._read_loop, daemon=True, name=thread_name,
        )
        self._reader.start()

        self._authenticate(handshake_frame, settle_ms, ack_types)

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
                    self._q.put(self._parse(raw), timeout=1)
                except queue.Full:
                    # Sustained consumer stall past the bounded queue size --
                    # treat as fatal rather than blocking the reader forever.
                    self._close_reason = "frame queue full; consumer stalled"
                    break
        except Exception as e:  # noqa: BLE001
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
        """Drop leftover frames (stale echoes) sitting in the queue before a new
        turn starts. Deliberately does NOT touch self._pending -- a bot frame
        pushed back during settling still belongs to the upcoming turn."""
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break

    # ------------------------------------------------------------------
    # Establishment
    # ------------------------------------------------------------------

    def _authenticate(
        self, handshake_frame: Mapping[str, object], settle_ms: int,
        ack_types: Sequence[str],
    ) -> None:
        self._ws.send(json.dumps(dict(handshake_frame)))

        # The servers this serves never ack. Wait for one if it ever appears
        # (defensive, for a future release), otherwise just settle.
        deadline = time.monotonic() + settle_ms / 1000
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
                    raise ProviderChannelClosed(self._close_reason)
                return
            if self._closed.is_set():
                raise ProviderChannelClosed(self._close_reason)
            f = self._try_get(timeout=min(remaining, POLL_SLICE_S))
            if f is None:
                continue
            if f.source in ack_types:
                return
            if f.source == "bot":
                self._pushback(f)  # the bot opened first -- keep it for the turn
                return

    # ------------------------------------------------------------------
    # Turns
    # ------------------------------------------------------------------

    def send_and_await_turn(self, text: str, timeout_s: float, idle_gap_s: float) -> BotTurn:
        if not self._turn_lock.acquire(blocking=False):
            raise ProviderConcurrentTurnError()
        try:
            self._drain_stale()  # leftover echoes

            self._ws.send(json.dumps(dict(self._turn_frame(text))))

            chunks: List[str] = []
            options: List[ParsedOption] = []
            attachments: List[Attachment] = []
            step: Optional[int] = None
            saw_control_payload = False
            heard_from_bot = False
            deadline = time.monotonic() + timeout_s
            last_rx = time.monotonic()

            while True:
                if self._closed.is_set():
                    raise ProviderChannelClosed(self._close_reason)

                # THE IDLE GAP ONLY APPLIES ONCE THE BOT HAS STARTED SPEAKING.
                #
                # It is a "fragments stopped arriving, flush what we have"
                # backstop -- a gap BETWEEN fragments. Applying it from send time
                # instead turned it into a hard deadline on the FIRST token, far
                # stricter than the configured turn timeout: with
                # idle_gap_ms=8000 / turn_timeout_ms=45000, a reply that took
                # 8.4s raised a turn timeout at 19% of the real budget. Verified
                # against the provider's own transcript rows -- the turn had
                # actually succeeded upstream; only this side stopped listening,
                # and the user's Retry then re-sent the answer into the NEXT
                # question. Most turns land in 2-6s, which is why it presented as
                # intermittent.
                #
                # Note `last_rx` is only advanced for source == "bot" below, so
                # the user-echo the server sends back immediately does not extend
                # the window -- the old expression really was measuring from send
                # time.
                remaining = deadline - time.monotonic()
                if heard_from_bot:
                    remaining = min(remaining, last_rx + idle_gap_s - time.monotonic())

                if remaining <= 0:
                    # saw_control_payload: we DID hear from the provider, its
                    # frame was just unusable. Falling through to the re-prompt
                    # below is honest; "the request timed out" is not.
                    if chunks or saw_control_payload:
                        break  # idle-gap (or timeout) flush
                    raise ProviderTurnTimeout(step=step)

                f = self._try_get(timeout=remaining)
                if f is None:
                    continue
                if f.source == "user":
                    continue  # ECHO -- DISCARD
                if f.source == "system" and f.error:
                    raise ProviderRemoteError(f.msg or f.error)
                if f.source != "bot":
                    continue

                last_rx = time.monotonic()
                heard_from_bot = True
                if f.control_payload:
                    # The provider leaked an internal LLM object instead of a
                    # reply. The parser has already stripped it down to whatever
                    # was user-facing; log it, because it means a turn was
                    # dropped upstream and only this side can see it.
                    saw_control_payload = True
                    logger.warning(
                        "provider sent an internal control payload as bot text "
                        "(step=%s, recovered=%r)",
                        f.step, f.msg[:120],
                    )
                if f.msg:
                    chunks.append(f.msg)  # ACCUMULATE
                if f.step is not None:
                    step = f.step
                if f.options:
                    options = f.options
                if f.attachments:
                    # LAST FRAME WINS, exactly as options do above. A turn
                    # offers one set of documents; a later frame restating them
                    # is a correction, not an addition, and accumulating would
                    # render the same file twice on a chunked turn.
                    attachments = f.attachments
                if f.finish_reason:
                    break  # END OF TURN

            text_out = "".join(chunks)
            if not text_out and saw_control_payload:
                # The leaked payload was ALL the turn contained, so there is
                # nothing to show. An empty bubble is worse than saying so, and
                # the provider does not advance its step on this path -- the next
                # answer is processed against the same step, so re-prompting
                # genuinely recovers the interview.
                text_out = UNREADABLE_TURN_MESSAGE

            return BotTurn(
                text=text_out, options=options, attachments=attachments, step=step,
            )
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
        except Exception:  # noqa: BLE001
            pass
        if not quiet:
            logger.warning(
                "provider channel closed", extra={"close_reason": self._close_reason},
            )
