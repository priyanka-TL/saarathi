"""Unit tests for WsChannel against a fake WebSocket double.

No real network. Settle/timeout/idle-gap values are kept small (tens to
hundreds of milliseconds) so the suite stays fast while still exercising
real wall-clock timing logic, not mocked-out time.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import List, Optional, Union

import pytest

from app.providers.errors import (
    ProviderChannelClosed,
    ProviderRemoteError,
    ProviderTurnTimeout,
)
from app.providers.connection import RemoteConnection
from app.providers.transport.frames import TurnEnd
from app.providers.transport.ws import WsChannel, UNREADABLE_TURN_MESSAGE
from app.providers.ws_flow.base import BaseWsFlowProvider


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

# How long the fake blocks in recv() when the script is empty -- long enough
# that it would fail any test relying on a short settle/timeout/idle-gap if
# WsChannel incorrectly waited on recv() itself instead of its own queue
# timeout, but short enough not to leave the suite hanging.
_BLOCK_FOREVER_S = 5.0


class _FakeWebSocket:
    def __init__(self):
        self.sent: List[str] = []
        self.connect_args = None
        self.closed = False
        self._script: List[tuple] = []  # (delay_s, payload: str | Exception)

    def queue_raw(self, delay_s: float, payload: Union[str, Exception]) -> None:
        self._script.append((delay_s, payload))

    def connect(self, url, **kwargs):
        self.connect_args = (url, kwargs)

    def send(self, text: str) -> None:
        self.sent.append(text)

    def recv(self) -> str:
        if not self._script:
            time.sleep(_BLOCK_FOREVER_S)
            return ""
        delay_s, payload = self._script.pop(0)
        time.sleep(delay_s)
        if isinstance(payload, Exception):
            raise payload
        return payload

    def close(self) -> None:
        self.closed = True


def _text_frame(msg: str, source: str, finish_reason=None, **extra) -> str:
    body = {"msg": msg, "source": source, "finish_reason": finish_reason}
    body.update(extra)
    return json.dumps({"text": body})


def _system_error_frame(error: str) -> str:
    return json.dumps({"text": {"msg": "", "source": "system", "error": error, "finish_reason": None}})


@dataclass
class _Handshake:
    settle_ms: int = 50
    ack_types: List[str] = field(default_factory=lambda: ["authenticated", "auth_success"])
    timeout_ms: int = 5000


@dataclass
class _Spec:
    flow_name: str = "guest-mi-story"
    handshake: _Handshake = field(default_factory=_Handshake)


@dataclass
class _Session:
    id: str = "sess-1"
    remote_session_id: str = "remote-sess-1"
    remote_profile_id: str = "profile-1"
    language: str = "en"
    remote_bot_route: str = "test-route"


# The REAL value object rather than a stand-in: it is a pure dataclass with no
# I/O, so using it here also pins that the channel reads the fields it is
# actually given (stream_url, origin_url, user_agent, stream_connect_timeout_s).
def _conn(**overrides) -> RemoteConnection:
    defaults = dict(
        provider="testprovider",
        base_url="https://remote.example.com",
        stream_url="wss://remote.example.com/ws/common/",
        user_agent="test-agent",
        stream_connect_timeout_s=5.0,
        origin_url="https://remote.example.com",
    )
    defaults.update(overrides)
    return RemoteConnection(**defaults)


class _Options:
    """The parts of a provider's options block the base class reads."""

    ip_city = ""
    ip_state = ""
    ip_zip = ""

    def __init__(self, handshake=None):
        self.handshake = handshake or _Handshake()


class _StubProvider(BaseWsFlowProvider):
    """A provider with no REST surface, used only to build a channel.

    Built through the REAL base class rather than by calling WsChannel
    directly, so `test_authenticate_frame_shape` below pins the frame
    production actually sends -- the handshake payload is the base class's, and
    a test that assembled its own would stop noticing if the two drifted.
    """

    name = "testprovider"
    options_model = object

    def __init__(self, conn, options, fake):
        self.ws_factory = lambda: fake
        self._conn = conn
        self._pool = None
        self.options = options
        self._rest = None


def _make_channel(fake: _FakeWebSocket, spec: Optional[_Spec] = None,
                  sess: Optional[_Session] = None):
    spec = spec or _Spec()
    provider = _StubProvider(_conn(), _Options(spec.handshake), fake)
    # user=None: _StubProvider never overrides _access_token, so the base
    # class's guest default (None) applies regardless of what's passed here.
    return provider._new_channel(spec, sess or _Session(), None)


# ---------------------------------------------------------------------------
# Acceptance 1: establishment completes in ~settle_ms without an ack
# ---------------------------------------------------------------------------


def test_establishment_completes_in_about_settle_ms_without_ack():
    fake = _FakeWebSocket()  # empty script -> recv() blocks _BLOCK_FOREVER_S
    spec = _Spec(handshake=_Handshake(settle_ms=50))

    t0 = time.monotonic()
    channel = _make_channel(fake, spec=spec)
    elapsed = time.monotonic() - t0

    assert elapsed < 0.3, f"establishment took {elapsed:.3f}s, expected ~0.05s"
    assert elapsed >= 0.04
    assert channel.alive


def test_authenticate_frame_shape():
    fake = _FakeWebSocket()
    sess = _Session(remote_session_id="rs-1", remote_profile_id="pf-1", language="hi", remote_bot_route="br-1")
    spec = _Spec(flow_name="guest-discussion", handshake=_Handshake(settle_ms=20))
    _make_channel(fake, spec=spec, sess=sess)

    assert len(fake.sent) == 1
    frame = json.loads(fake.sent[0])
    assert frame == {
        "type": "authenticate",
        "sessionid": "rs-1",
        "profileid": "pf-1",
        "projectid": "",
        "taskid": None,
        "access_token": None,
        "route": "hi",
        "bot_route": "br-1",
        "flow_name": "guest-discussion",
        "address": {"ipCity": "", "ipState": "", "ipZipCode": ""},
    }


# ---------------------------------------------------------------------------
# Acceptance 2: echo frames never reach the accumulated text
# ---------------------------------------------------------------------------


def test_echo_frames_never_reach_accumulated_text():
    fake = _FakeWebSocket()
    # Queued BEFORE construction: the reader thread's recv() loop runs
    # continuously regardless of what phase the main thread is in, so any
    # frame meant to arrive AFTER settling needs a delay comfortably past
    # settle_ms, or it either gets consumed as a (spurious) settling pushback
    # or sits in the fake's script queue behind an earlier already-blocked
    # recv() call.
    fake.queue_raw(0.03, _text_frame("Tell me your name", source="user"))
    fake.queue_raw(0.0, _text_frame("Nice to meet you!", source="bot", finish_reason="stop"))
    channel = _make_channel(fake, spec=_Spec(handshake=_Handshake(settle_ms=20)))

    turn = channel.send_and_await_turn("Priya", timeout_s=2.0, idle_gap_s=0.5)
    assert "Tell me your name" not in turn.text
    assert turn.text == "Nice to meet you!"


# ---------------------------------------------------------------------------
# Acceptance 3: a chunked reply is accumulated in full
# ---------------------------------------------------------------------------


def test_chunked_reply_accumulated_in_full():
    fake = _FakeWebSocket()
    fake.queue_raw(0.03, _text_frame("I am going to ", source="bot", finish_reason=None, step=1))
    fake.queue_raw(0.0, _text_frame("tell you a story ", source="bot", finish_reason=None, step=2))
    fake.queue_raw(0.0, _text_frame("about a fox.", source="bot", finish_reason="stop", step=3))
    channel = _make_channel(fake, spec=_Spec(handshake=_Handshake(settle_ms=20)))

    turn = channel.send_and_await_turn("go on", timeout_s=2.0, idle_gap_s=0.5)
    assert turn.text == "I am going to tell you a story about a fox."
    assert turn.step == 3


# ---------------------------------------------------------------------------
# Acceptance 4: idle gap flushes partial content rather than hanging
# ---------------------------------------------------------------------------


def test_idle_gap_flushes_partial_content_rather_than_hanging():
    fake = _FakeWebSocket()
    fake.queue_raw(0.03, _text_frame("partial answer", source="bot", finish_reason=None))
    # No further frames queued -> recv() blocks _BLOCK_FOREVER_S (5s).
    channel = _make_channel(fake, spec=_Spec(handshake=_Handshake(settle_ms=20)))

    t0 = time.monotonic()
    turn = channel.send_and_await_turn("go on", timeout_s=10.0, idle_gap_s=0.1)
    elapsed = time.monotonic() - t0

    assert turn.text == "partial answer"
    assert elapsed < 1.0, f"idle-gap flush took {elapsed:.3f}s, expected ~0.1s not the full 10s timeout"


# ---------------------------------------------------------------------------
# Pushback during settling
# ---------------------------------------------------------------------------


def test_bot_frame_during_settling_is_pushed_back_and_consumed_by_first_turn():
    fake = _FakeWebSocket()
    fake.queue_raw(0.01, _text_frame("Welcome!", source="bot", finish_reason="stop"))

    channel = _make_channel(fake, spec=_Spec(handshake=_Handshake(settle_ms=200)))
    # No further frames queued for the first turn -- the pushed-back frame
    # from settling must be what answers it.
    turn = channel.send_and_await_turn("hi", timeout_s=2.0, idle_gap_s=0.3)
    assert turn.text == "Welcome!"


# ---------------------------------------------------------------------------
# Timeouts and errors
# ---------------------------------------------------------------------------


def test_turn_timeout_raised_when_nothing_accumulated():
    fake = _FakeWebSocket()  # empty script, recv() blocks
    channel = _make_channel(fake, spec=_Spec(handshake=_Handshake(settle_ms=20)))

    with pytest.raises(ProviderTurnTimeout):
        channel.send_and_await_turn("hello", timeout_s=0.1, idle_gap_s=0.05)


def test_system_error_frame_raises_remote_error():
    fake = _FakeWebSocket()
    fake.queue_raw(0.03, _system_error_frame("Session timed out"))
    channel = _make_channel(fake, spec=_Spec(handshake=_Handshake(settle_ms=20)))

    with pytest.raises(ProviderRemoteError, match="Session timed out"):
        channel.send_and_await_turn("hello", timeout_s=2.0, idle_gap_s=0.5)


def test_channel_closed_raised_mid_turn():
    fake = _FakeWebSocket()
    fake.queue_raw(0.03, ConnectionError("connection reset"))
    channel = _make_channel(fake, spec=_Spec(handshake=_Handshake(settle_ms=20)))

    with pytest.raises(ProviderChannelClosed):
        channel.send_and_await_turn("hello", timeout_s=2.0, idle_gap_s=0.5)
    assert not channel.alive


def test_channel_closed_raised_during_settling():
    fake = _FakeWebSocket()
    fake.queue_raw(0.0, ConnectionError("connection reset"))

    with pytest.raises(ProviderChannelClosed):
        _make_channel(fake, spec=_Spec(handshake=_Handshake(settle_ms=200)))


# ---------------------------------------------------------------------------
# Reader thread / lifecycle
# ---------------------------------------------------------------------------


def test_reader_thread_is_daemon():
    fake = _FakeWebSocket()
    channel = _make_channel(fake, spec=_Spec(handshake=_Handshake(settle_ms=20)))
    assert channel._reader.daemon is True


def test_alive_and_close():
    fake = _FakeWebSocket()
    channel = _make_channel(fake, spec=_Spec(handshake=_Handshake(settle_ms=20)))
    assert channel.alive is True

    channel.close(quiet=True)
    assert channel.alive is False
    assert fake.closed is True

    # Calling close() again must not raise.
    channel.close(quiet=True)


# ---------------------------------------------------------------------------
# Acceptance 8: a turn containing only a leaked internal payload
#
# frame_parser §Defect 4 strips the payload, which can leave the turn with no
# text at all. Rendering that as an empty bubble is not acceptable either, so
# the channel substitutes a re-prompt. Mitra does NOT advance
# chat_session.current_step on this path, so the user's next answer is
# processed against the same step and the interview actually recovers.
# ---------------------------------------------------------------------------

_LEAKED_TOOL_CALL = [
    {"response": "", "response_reason": "Proceeding to function call."},
    {"name": "get_state_information", "parameters": {"state_name": "MAIN_CHALLENGE"}},
]


def test_leaked_payload_turn_reprompts_instead_of_dumping_json():
    fake = _FakeWebSocket()
    fake.queue_raw(0.03, _text_frame(_LEAKED_TOOL_CALL, source="bot", finish_reason="stop", step=9))
    channel = _make_channel(fake, spec=_Spec(handshake=_Handshake(settle_ms=20)))

    turn = channel.send_and_await_turn("Transportation issue", timeout_s=2.0, idle_gap_s=0.5)

    assert turn.text == UNREADABLE_TURN_MESSAGE
    assert "get_state_information" not in turn.text
    assert "{'" not in turn.text
    assert turn.step == 9


def test_leaked_payload_alongside_real_text_keeps_only_the_real_text():
    """No fallback when the turn did produce something to say -- the re-prompt
    must not append itself to a perfectly good reply."""
    fake = _FakeWebSocket()
    fake.queue_raw(0.03, _text_frame(_LEAKED_TOOL_CALL, source="bot", finish_reason=None, step=9))
    fake.queue_raw(0.0, _text_frame("What was the main challenge?", source="bot", finish_reason="stop", step=10))
    channel = _make_channel(fake, spec=_Spec(handshake=_Handshake(settle_ms=20)))

    turn = channel.send_and_await_turn("Transportation issue", timeout_s=2.0, idle_gap_s=0.5)

    assert turn.text == "What was the main challenge?"
    assert UNREADABLE_TURN_MESSAGE not in turn.text


def test_recovered_response_text_is_used_verbatim_without_the_fallback():
    fake = _FakeWebSocket()
    payload = [
        {"response": "Got it. And who else was involved?"},
        {"name": "get_state_information", "parameters": {"state_name": "NEXT"}},
    ]
    fake.queue_raw(0.03, _text_frame(payload, source="bot", finish_reason="stop", step=4))
    channel = _make_channel(fake, spec=_Spec(handshake=_Handshake(settle_ms=20)))

    turn = channel.send_and_await_turn("ok", timeout_s=2.0, idle_gap_s=0.5)

    assert turn.text == "Got it. And who else was involved?"


def test_ordinary_turn_never_gets_the_fallback():
    """Guard against the fallback leaking into the normal path."""
    fake = _FakeWebSocket()
    fake.queue_raw(0.03, _text_frame("What are the reasons for the problem?", source="bot", finish_reason="stop", step=8))
    channel = _make_channel(fake, spec=_Spec(handshake=_Handshake(settle_ms=20)))

    turn = channel.send_and_await_turn("hi", timeout_s=2.0, idle_gap_s=0.5)

    assert turn.text == "What are the reasons for the problem?"


def test_silence_still_times_out_rather_than_reprompting():
    """A turn with no frames at all is a TIMEOUT, not an unreadable payload --
    the two must not be conflated, or a dead upstream would look like a normal
    re-prompt and the session would never surface the failure."""
    fake = _FakeWebSocket()  # empty script -> recv() blocks
    channel = _make_channel(fake, spec=_Spec(handshake=_Handshake(settle_ms=20)))

    with pytest.raises(ProviderTurnTimeout):
        channel.send_and_await_turn("hello", timeout_s=0.3, idle_gap_s=0.2)


# ---------------------------------------------------------------------------
# Acceptance 9: the idle gap is a gap BETWEEN fragments, not a deadline on the
# bot's first token.
#
# Reported live: a turn where Mitra answered in 8.4s raised ProviderTurnTimeout
# even though turn_timeout_ms was 45000. record_stories.yaml runs a 45s turn
# timeout against an 8s idle gap, so measuring the gap from send time made the
# effective timeout 5.6x stricter than the configured one. Confirmed against
# Mitra's CompanyChat rows: the turn had succeeded upstream.
#
# Ratios below mirror the real YAML (45:8) so the relationship under test is
# the shipped one, just scaled to keep the suite fast.
# ---------------------------------------------------------------------------

_YAML_TIMEOUT_TO_GAP = 45.0 / 8.0


def test_first_reply_slower_than_the_idle_gap_still_succeeds():
    """THE regression test for "The request timed out. Your session is still
    active." on a turn Mitra actually answered."""
    idle_gap = 0.30
    timeout = idle_gap * _YAML_TIMEOUT_TO_GAP          # 1.69s
    reply_after = idle_gap * 2                          # 2x the gap, 18% of the timeout

    fake = _FakeWebSocket()
    fake.queue_raw(reply_after, _text_frame(
        "What was the main problem you noticed?", source="bot", finish_reason="stop", step=8,
    ))
    channel = _make_channel(fake, spec=_Spec(handshake=_Handshake(settle_ms=20)))

    turn = channel.send_and_await_turn(
        "The improvement was implemented in Melur village, Madurai district, Tamil Nadu.",
        timeout_s=timeout, idle_gap_s=idle_gap,
    )

    assert turn.text == "What was the main problem you noticed?"
    assert turn.step == 8


def test_idle_gap_still_flushes_once_fragments_stop_arriving():
    """The backstop must keep working AFTER the bot starts speaking -- that is
    its actual purpose (§1.3). Losing it would make every truncated stream hang
    for the full turn timeout."""
    idle_gap = 0.25
    timeout = idle_gap * _YAML_TIMEOUT_TO_GAP           # 1.41s -- far longer

    fake = _FakeWebSocket()
    # Two chunks, then silence: no finish_reason ever arrives.
    fake.queue_raw(0.05, _text_frame("I am going to ", source="bot", finish_reason=None, step=3))
    fake.queue_raw(0.02, _text_frame("tell you a story.", source="bot", finish_reason=None, step=3))
    channel = _make_channel(fake, spec=_Spec(handshake=_Handshake(settle_ms=20)))

    t0 = time.monotonic()
    turn = channel.send_and_await_turn("go on", timeout_s=timeout, idle_gap_s=idle_gap)
    elapsed = time.monotonic() - t0

    assert turn.text == "I am going to tell you a story."
    assert elapsed < timeout * 0.75, (
        f"flushed after {elapsed:.2f}s -- the idle gap should have ended the turn "
        f"well before the {timeout:.2f}s turn timeout"
    )


def test_a_silent_upstream_still_times_out_but_only_at_the_turn_timeout():
    """Genuine silence must still raise -- and must now wait the FULL turn
    timeout rather than giving up at the idle gap."""
    idle_gap = 0.15
    timeout = 0.6

    fake = _FakeWebSocket()  # empty script -> recv() blocks
    channel = _make_channel(fake, spec=_Spec(handshake=_Handshake(settle_ms=20)))

    t0 = time.monotonic()
    with pytest.raises(ProviderTurnTimeout):
        channel.send_and_await_turn("hello", timeout_s=timeout, idle_gap_s=idle_gap)
    elapsed = time.monotonic() - t0

    assert elapsed >= timeout * 0.9, (
        f"gave up after {elapsed:.2f}s but the turn timeout is {timeout:.2f}s -- "
        "the idle gap is being applied before the bot has said anything"
    )


def test_user_echo_does_not_start_the_idle_gap_clock():
    """Mitra echoes the user's own message back immediately (§1.2). It is not
    the bot speaking, so it must not open the idle-gap window -- otherwise the
    echo would re-introduce the same premature timeout."""
    idle_gap = 0.25
    timeout = idle_gap * _YAML_TIMEOUT_TO_GAP

    fake = _FakeWebSocket()
    fake.queue_raw(0.02, _text_frame("my answer", source="user", finish_reason=None))
    fake.queue_raw(idle_gap * 2, _text_frame("Understood.", source="bot", finish_reason="stop", step=5))
    channel = _make_channel(fake, spec=_Spec(handshake=_Handshake(settle_ms=20)))

    turn = channel.send_and_await_turn("my answer", timeout_s=timeout, idle_gap_s=idle_gap)

    assert turn.text == "Understood."


def test_control_payload_then_silence_reprompts_instead_of_reporting_a_timeout():
    """We heard from Mitra; its frame was just unusable (§Defect 4). Reporting
    "the request timed out" would be inaccurate."""
    fake = _FakeWebSocket()
    fake.queue_raw(0.03, _text_frame(_LEAKED_TOOL_CALL, source="bot", finish_reason=None, step=9))
    channel = _make_channel(fake, spec=_Spec(handshake=_Handshake(settle_ms=20)))

    turn = channel.send_and_await_turn("Transportation issue", timeout_s=1.2, idle_gap_s=0.2)

    assert turn.text == UNREADABLE_TURN_MESSAGE


# ---------------------------------------------------------------------------
# Downloadable documents across a chunked turn
# ---------------------------------------------------------------------------

_DOWNLOAD = {
    "pdf_url": "https://static.test/a/1786-plan.pdf",
    "docx_url": "https://static.test/a/1786-plan.docx",
    "file_name": "plan",
}


def test_documents_on_the_final_frame_reach_the_turn():
    fake = _FakeWebSocket()
    fake.queue_raw(0.03, _text_frame("Your plan ", source="bot", finish_reason=None, step=1))
    fake.queue_raw(0.0, _text_frame(
        "is ready.", source="bot", finish_reason="stop", step=2,
        extra_content={"download": _DOWNLOAD},
    ))
    channel = _make_channel(fake, spec=_Spec(handshake=_Handshake(settle_ms=20)))

    turn = channel.send_and_await_turn("go on", timeout_s=2.0, idle_gap_s=0.5)

    assert turn.text == "Your plan is ready."
    assert [a.format for a in turn.attachments] == ["docx", "pdf"]


def test_documents_on_a_MID_turn_chunk_are_not_lost():
    """They are carried on whichever frame the platform chose, which is not
    necessarily the last one."""
    fake = _FakeWebSocket()
    fake.queue_raw(0.03, _text_frame(
        "Your plan ", source="bot", finish_reason=None, step=1,
        extra_content={"download": _DOWNLOAD},
    ))
    fake.queue_raw(0.0, _text_frame("is ready.", source="bot", finish_reason="stop", step=2))
    channel = _make_channel(fake, spec=_Spec(handshake=_Handshake(settle_ms=20)))

    turn = channel.send_and_await_turn("go on", timeout_s=2.0, idle_gap_s=0.5)

    assert turn.text == "Your plan is ready."
    assert len(turn.attachments) == 2


def test_a_later_frame_replaces_rather_than_appends():
    """LAST FRAME WINS, matching how options already behave. Accumulating would
    render the same document twice on a chunked turn."""
    fake = _FakeWebSocket()
    fake.queue_raw(0.03, _text_frame(
        "one ", source="bot", finish_reason=None, step=1,
        extra_content={"download": {"pdf_url": "https://static.test/a/first.pdf"}},
    ))
    fake.queue_raw(0.0, _text_frame(
        "two", source="bot", finish_reason="stop", step=2,
        extra_content={"download": {"pdf_url": "https://static.test/a/second.pdf"}},
    ))
    channel = _make_channel(fake, spec=_Spec(handshake=_Handshake(settle_ms=20)))

    turn = channel.send_and_await_turn("go on", timeout_s=2.0, idle_gap_s=0.5)

    assert [a.url for a in turn.attachments] == ["https://static.test/a/second.pdf"]


def test_a_turn_with_no_documents_reports_none():
    fake = _FakeWebSocket()
    fake.queue_raw(0.03, _text_frame("just talking", source="bot", finish_reason="stop", step=1))
    channel = _make_channel(fake, spec=_Spec(handshake=_Handshake(settle_ms=20)))

    turn = channel.send_and_await_turn("hi", timeout_s=2.0, idle_gap_s=0.5)

    assert turn.attachments == []


# ---------------------------------------------------------------------------
# How the turn ENDED, which is the difference between a slow provider and a
# provider that never says it has finished.
#
# Both endings produce identical text, so nothing above this point can tell
# them apart -- and they call for opposite fixes. A `finish_reason` turn is as
# fast as the provider is; an `idle_gap` turn spent the whole configured gap
# adding nothing, every single time, and no provider-side speedup removes it.
# ---------------------------------------------------------------------------


def test_a_turn_the_provider_finished_is_recorded_as_finish_reason():
    fake = _FakeWebSocket()
    fake.queue_raw(0.03, _text_frame("I am going to ", source="bot", finish_reason=None, step=1))
    fake.queue_raw(0.0, _text_frame("tell you a story ", source="bot", finish_reason=None, step=2))
    fake.queue_raw(0.0, _text_frame("about a fox.", source="bot", finish_reason="stop", step=3))
    channel = _make_channel(fake, spec=_Spec(handshake=_Handshake(settle_ms=20)))

    turn = channel.send_and_await_turn("go on", timeout_s=2.0, idle_gap_s=0.5)

    assert turn.end_reason is TurnEnd.FINISH_REASON
    assert turn.fragment_count == 3
    assert turn.first_frame_ms is not None and turn.last_frame_ms is not None
    assert turn.first_frame_ms <= turn.last_frame_ms


def test_a_turn_flushed_by_the_backstop_is_recorded_as_idle_gap():
    """The measurement that makes the 8-second backstop visible.

    The provider answered promptly and then went quiet without ever saying it
    was done. `last_frame_ms` is what proves the wait was wasted: the reply was
    complete long before the channel stopped listening for more of it.
    """
    fake = _FakeWebSocket()
    fake.queue_raw(0.03, _text_frame("partial answer", source="bot", finish_reason=None))
    channel = _make_channel(fake, spec=_Spec(handshake=_Handshake(settle_ms=20)))

    t0 = time.monotonic()
    turn = channel.send_and_await_turn("go on", timeout_s=10.0, idle_gap_s=0.3)
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    assert turn.text == "partial answer"
    assert turn.end_reason is TurnEnd.IDLE_GAP
    assert turn.fragment_count == 1
    # The whole point: the answer was in hand ~300ms before the turn returned.
    assert turn.last_frame_ms is not None
    assert elapsed_ms - turn.last_frame_ms >= 250


def test_a_turn_that_exhausts_the_whole_budget_is_recorded_as_turn_timeout():
    """Distinguished from an idle-gap flush, because the remedies differ: this
    one is a genuinely slow provider, not a missing end-of-turn signal."""
    fake = _FakeWebSocket()
    fake.queue_raw(0.03, _text_frame("still thinking", source="bot", finish_reason=None))
    channel = _make_channel(fake, spec=_Spec(handshake=_Handshake(settle_ms=20)))

    # idle_gap LONGER than the turn budget, so the overall deadline binds first.
    turn = channel.send_and_await_turn("go on", timeout_s=0.3, idle_gap_s=5.0)

    assert turn.text == "still thinking"
    assert turn.end_reason is TurnEnd.TURN_TIMEOUT


def test_a_turn_that_never_arrives_still_raises_rather_than_reporting_an_ending():
    """Unchanged behaviour, pinned: measurement must not have turned a timeout
    into an empty but 'successful' turn."""
    fake = _FakeWebSocket()
    channel = _make_channel(fake, spec=_Spec(handshake=_Handshake(settle_ms=20)))

    with pytest.raises(ProviderTurnTimeout):
        channel.send_and_await_turn("hello", timeout_s=0.2, idle_gap_s=0.1)
