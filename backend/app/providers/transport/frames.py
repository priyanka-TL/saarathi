"""The normalised shape of one inbound streaming frame.

Responsible for: the vocabulary `WsChannel` acts on, independent of any
provider's wire JSON.
Used by: transport/ws.py consumes these; a provider's own parser produces them.

The split is deliberate. `WsChannel` needs to know that a frame is an echo, a
chunk, or the end of a turn -- it must not know which JSON keys carried that.
A provider supplies a `parse` callable that returns one of these; everything
downstream is shared.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List, Optional


class FrameKind(Enum):
    """What the channel should do with a frame.

      BOT_CHUNK  -- accumulate msg into the current turn
      BOT_FINAL  -- last chunk of the current turn (finish_reason is truthy)
      USER_ECHO  -- the provider echoing the user's own message back; DISCARD
      SYSTEM     -- system/control frame (session end, provider-side error)
      ERROR      -- parse failure; log and discard
    """

    BOT_CHUNK = auto()
    BOT_FINAL = auto()
    USER_ECHO = auto()
    SYSTEM = auto()
    ERROR = auto()


@dataclass(frozen=True)
class ParsedOption:
    """One choice button offered alongside a bot turn."""

    id: str
    label: str
    value: str


@dataclass(frozen=True)
class Attachment:
    """One downloadable document offered alongside a bot turn.

    NOT AN OPTION, and the distinction is load-bearing. An option is
    click-to-reply: the SPA echoes its label as a user message and posts its
    value back as the next turn. Putting a URL there would send the URL to the
    bot as user input. An attachment is an `<a href>` and nothing else.

    One entry PER FILE, not per document: a document offered as both PDF and
    DOCX produces two, sharing a `file_name` and differing in `format`. That
    keeps "only one format available" from being a special case anywhere
    downstream -- it is simply a shorter list.

    ``file_name`` is the document's own name WITHOUT an extension, as the
    platform reports it. It is not display text (the reply already says what
    the document is); it names the file a browser saves.
    """

    file_name: str
    format: str
    media_type: str
    url: str


@dataclass(frozen=True)
class Frame:
    """One normalised inbound frame.

    ``kind``            -- what the channel should do with this frame
    ``source``          -- raw source discriminator (for logging / ack matching)
    ``msg``             -- text fragment (empty string if none)
    ``step``            -- step number reported by the provider
    ``finish_reason``   -- truthy when this is the last chunk of a bot turn
    ``options``         -- normalised choice buttons (may be empty)
    ``attachments``     -- downloadable documents (may be empty)
    ``error``           -- human-readable parse/system error
    ``control_payload`` -- msg carried an internal object, not user-facing text.
                        ``msg`` holds whatever was recoverable, or "".
    """

    kind: FrameKind
    source: str = ""
    msg: str = ""
    step: int = 0
    finish_reason: Optional[str] = None
    options: List[ParsedOption] = field(default_factory=list)
    attachments: List[Attachment] = field(default_factory=list)
    error: str = ""
    control_payload: bool = False


class TurnEnd(str, Enum):
    """WHY a turn stopped being awaited. See `BotTurn.end_reason`.

    A `str` mixin so it lands in a log line and a JSON field as its own value
    rather than as `TurnEnd.IDLE_GAP`.

      FINISH_REASON -- the provider said it was done. The only clean ending.
      IDLE_GAP      -- fragments simply stopped arriving and the idle-gap
                       backstop flushed what had accumulated. The turn's text is
                       usable, but EVERY such turn paid the full gap in wall
                       clock for nothing, so a high rate here is a defect in the
                       provider's end-of-turn signalling, not a slow model.
      TURN_TIMEOUT  -- the whole turn budget ran out with content in hand.
    """

    FINISH_REASON = "finish_reason"
    IDLE_GAP = "idle_gap"
    TURN_TIMEOUT = "turn_timeout"


@dataclass(frozen=True)
class BotTurn:
    """The result of one `WsChannel.send_and_await_turn` call.

    The four measurement fields exist because the turn's total duration alone
    could not distinguish "the provider thought for nine seconds" from "the
    provider answered in one second and we then sat out an eight-second idle
    gap". Those call for opposite fixes, and the second is invisible without
    `end_reason`.
    """

    text: str
    options: List[ParsedOption] = field(default_factory=list)
    attachments: List[Attachment] = field(default_factory=list)
    step: Optional[int] = None

    #: Defaulted so a construction that does not measure -- a test fake, a
    #: provider that never adopts this transport -- stays valid.
    end_reason: Optional[TurnEnd] = None
    #: Milliseconds from sending the turn to the FIRST bot frame. The provider's
    #: real think time; the closest thing to a time-to-first-token we can see.
    first_frame_ms: Optional[int] = None
    #: Milliseconds from sending the turn to the LAST bot frame. The difference
    #: between this and the turn's total is time spent waiting for nothing.
    last_frame_ms: Optional[int] = None
    #: How many bot frames the reply arrived in. 1 means the provider is not
    #: streaming to us, which is what makes an idle-gap ending likely.
    fragment_count: int = 0

    #: The provider's own `finish_reason` STRING, from the frame that ended the
    #: turn. None when the turn ended some other way.
    #:
    #: WHY CARRY A VALUE WE DO NOT BRANCH ON. `WsChannel` tests this field for
    #: truthiness only, so every distinct value the platform might send has so far
    #: been collapsed to "the turn ended" and thrown away. That matters because
    #: the per-turn completion poll -- two HTTP round trips, every turn, on the
    #: critical path -- exists precisely to discover something the socket may
    #: already be saying: `ws_flow/frames.py` lists `session_end` among the
    #: envelope types this family knows, and the legacy parser hardcodes
    #: `finish_reason="stop"` for one shape. If the platform distinguishes
    #: end-of-TURN from end-of-SESSION here, the poll is redundant and can go.
    #:
    #: So this is recorded, not acted on. Deciding from it would be guessing;
    #: observing it for a few days is not.
    finish_reason: Optional[str] = None
