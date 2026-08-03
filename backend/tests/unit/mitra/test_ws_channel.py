"""Unit tests for MitraChannel against a fake WebSocket double.

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

from app.integrations.mitra.exceptions import (
    MitraChannelClosed,
    MitraRemoteError,
    MitraTurnTimeout,
)
from app.integrations.mitra.connection import MitraConnection
from app.integrations.mitra.ws_channel import MitraChannel, UNREADABLE_TURN_MESSAGE


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

# How long the fake blocks in recv() when the script is empty -- long enough
# that it would fail any test relying on a short settle/timeout/idle-gap if
# MitraChannel incorrectly waited on recv() itself instead of its own queue
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
# I/O, so using it here also pins that MitraChannel reads the fields it is
# actually given (ws_url, origin_url, user_agent, ws_connect_timeout_s, ip_*).
def _conn(**overrides) -> MitraConnection:
    defaults = dict(
        base_url="https://mitra.example.com",
        ws_url="wss://mitra.example.com/ws/common/",
        user_agent="test-agent",
        ws_connect_timeout_s=5.0,
        origin_url="https://mitra.example.com",
    )
    defaults.update(overrides)
    return MitraConnection(**defaults)


def _make_channel(fake: _FakeWebSocket, spec: Optional[_Spec] = None, sess: Optional[_Session] = None):
    return MitraChannel(spec or _Spec(), sess or _Session(), _conn(), ws_factory=lambda: fake)


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

    with pytest.raises(MitraTurnTimeout):
        channel.send_and_await_turn("hello", timeout_s=0.1, idle_gap_s=0.05)


def test_system_error_frame_raises_remote_error():
    fake = _FakeWebSocket()
    fake.queue_raw(0.03, _system_error_frame("Session timed out"))
    channel = _make_channel(fake, spec=_Spec(handshake=_Handshake(settle_ms=20)))

    with pytest.raises(MitraRemoteError, match="Session timed out"):
        channel.send_and_await_turn("hello", timeout_s=2.0, idle_gap_s=0.5)


def test_channel_closed_raised_mid_turn():
    fake = _FakeWebSocket()
    fake.queue_raw(0.03, ConnectionError("connection reset"))
    channel = _make_channel(fake, spec=_Spec(handshake=_Handshake(settle_ms=20)))

    with pytest.raises(MitraChannelClosed):
        channel.send_and_await_turn("hello", timeout_s=2.0, idle_gap_s=0.5)
    assert not channel.alive


def test_channel_closed_raised_during_settling():
    fake = _FakeWebSocket()
    fake.queue_raw(0.0, ConnectionError("connection reset"))

    with pytest.raises(MitraChannelClosed):
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

    with pytest.raises(MitraTurnTimeout):
        channel.send_and_await_turn("hello", timeout_s=0.3, idle_gap_s=0.2)


# ---------------------------------------------------------------------------
# Acceptance 9: the idle gap is a gap BETWEEN fragments, not a deadline on the
# bot's first token.
#
# Reported live: a turn where Mitra answered in 8.4s raised MitraTurnTimeout
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
    with pytest.raises(MitraTurnTimeout):
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
