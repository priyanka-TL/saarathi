"""Unit tests for MitraFrameParser against recorded fixtures.

Every test loads a fixture that represents one real wire-format shape
observed in the Mitra source. No network, no mocking — pure function tests.

Test grouping:
  1. User-echo (§1.2)
  2. Bot chunks and final frames (§1.3 chunked delivery)
  3. extra_content shapes 1-3 (§1.3 option normalisation)
  4. System / error frames
  5. Legacy envelope types (Node bot reference variants)
  6. Tolerance — malformed input never raises
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.providers.ws_flow.frames import (
    Frame,
    FrameKind,
    ParsedOption,
    parse,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"


def _raw(name: str) -> str:
    """Load a fixture file as a raw JSON string."""
    return (FIXTURES / name).read_text()


def _payload(name: str) -> dict:
    return json.loads(_raw(name))


# ---------------------------------------------------------------------------
# 1. User-echo (§1.2)
# ---------------------------------------------------------------------------

class TestUserEcho:
    """§1.2 — async_consumer.py:88-96.

    Mitra echoes the user's own message back with source:user BEFORE
    dispatching the bot reply to Celery. The parser must label these
    USER_ECHO so the channel discards them without storing them as
    assistant messages.
    """

    def test_source_user_yields_user_echo_kind(self):
        frame = parse(_raw("user_echo.json"))
        assert frame.kind is FrameKind.USER_ECHO

    def test_user_echo_preserves_msg(self):
        frame = parse(_raw("user_echo.json"))
        assert frame.msg == "Tell me your name"

    def test_user_echo_has_no_options(self):
        frame = parse(_raw("user_echo.json"))
        assert frame.options == []

    def test_user_echo_has_no_error(self):
        frame = parse(_raw("user_echo.json"))
        assert frame.error == ""

    def test_user_echo_finish_reason_is_none(self):
        # A user-echo frame never signals end-of-turn
        frame = parse(_raw("user_echo.json"))
        assert frame.finish_reason is None

    def test_inline_user_echo(self):
        """Inline variant — same rule, direct construction."""
        raw = json.dumps({"text": {"msg": "Priya", "source": "user"}})
        frame = parse(raw)
        assert frame.kind is FrameKind.USER_ECHO
        assert frame.msg == "Priya"

    def test_user_echo_is_discardable(self):
        """The channel pattern: discard anything that is not BOT_CHUNK / BOT_FINAL."""
        frame = parse(_raw("user_echo.json"))
        assert frame.kind not in (FrameKind.BOT_CHUNK, FrameKind.BOT_FINAL)


# ---------------------------------------------------------------------------
# 2. Bot chunks and final frames (§1.3)
# ---------------------------------------------------------------------------

class TestBotChunks:
    """§1.3 — free_flow_service.py:131-152.

    Replies arrive as many BOT_CHUNK frames (finish_reason null) followed
    by one BOT_FINAL frame (finish_reason truthy). The channel accumulates
    msg across chunks; the parser only has to classify each frame correctly.
    """

    def test_chunk_kind(self):
        frame = parse(_raw("bot_chunk.json"))
        assert frame.kind is FrameKind.BOT_CHUNK

    def test_chunk_finish_reason_is_none(self):
        frame = parse(_raw("bot_chunk.json"))
        assert frame.finish_reason is None

    def test_chunk_source_is_bot(self):
        frame = parse(_raw("bot_chunk.json"))
        assert frame.source == "bot"

    def test_chunk_msg_content(self):
        frame = parse(_raw("bot_chunk.json"))
        assert frame.msg == "I am going to "

    def test_chunk_step(self):
        frame = parse(_raw("bot_chunk.json"))
        assert frame.step == 3

    def test_chunk_has_no_options(self):
        frame = parse(_raw("bot_chunk.json"))
        assert frame.options == []

    def test_final_no_options_kind(self):
        frame = parse(_raw("bot_final_no_options.json"))
        assert frame.kind is FrameKind.BOT_FINAL

    def test_final_finish_reason(self):
        frame = parse(_raw("bot_final_no_options.json"))
        assert frame.finish_reason == "stop"

    def test_final_step(self):
        frame = parse(_raw("bot_final_no_options.json"))
        assert frame.step == 7

    def test_empty_finish_reason_string_is_chunk(self):
        """An empty-string finish_reason is falsy → BOT_CHUNK, not BOT_FINAL."""
        raw = json.dumps({
            "text": {"msg": "fragment", "source": "bot", "finish_reason": ""}
        })
        frame = parse(raw)
        assert frame.kind is FrameKind.BOT_CHUNK
        assert frame.finish_reason is None

    def test_bytes_input_is_accepted(self):
        """parse() must accept bytes (as websocket-client delivers them)."""
        raw_bytes = _raw("bot_chunk.json").encode("utf-8")
        frame = parse(raw_bytes)
        assert frame.kind is FrameKind.BOT_CHUNK


# ---------------------------------------------------------------------------
# 3. extra_content shapes (§1.3)
# ---------------------------------------------------------------------------

class TestExtraContentShape1StateMachineOptions:
    """Shape 1: {question, options} — state machine with choice buttons."""

    def test_kind_is_bot_final(self):
        frame = parse(_raw("bot_state_machine_options.json"))
        assert frame.kind is FrameKind.BOT_FINAL

    def test_options_count(self):
        frame = parse(_raw("bot_state_machine_options.json"))
        assert len(frame.options) == 4

    def test_options_type(self):
        frame = parse(_raw("bot_state_machine_options.json"))
        for opt in frame.options:
            assert isinstance(opt, ParsedOption)

    def test_first_option_fields(self):
        frame = parse(_raw("bot_state_machine_options.json"))
        opt = frame.options[0]
        assert opt.id == "en"
        assert opt.label == "English"
        assert opt.value == "en"

    def test_all_four_languages_present(self):
        frame = parse(_raw("bot_state_machine_options.json"))
        ids = [o.id for o in frame.options]
        assert set(ids) == {"en", "hi", "kn", "te"}

    def test_options_with_missing_ids_get_ordinal_fallback(self):
        """Options lacking an 'id' field receive their list-index as id."""
        frame = parse(_raw("options_missing_ids.json"))
        assert len(frame.options) == 2
        assert frame.options[0].id == "0"
        assert frame.options[0].label == "Yes"
        assert frame.options[1].id == "1"
        assert frame.options[1].label == "No"


class TestExtraContentShape2StoryValidation:
    """Shape 2: {problem_statement, should_move_forward, validation}.

    This is a machine-to-machine signal, not a UI affordance. The parser
    yields an empty option list — no choice buttons should appear.
    """

    def test_kind_is_bot_final(self):
        frame = parse(_raw("bot_story_validation.json"))
        assert frame.kind is FrameKind.BOT_FINAL

    def test_options_is_empty(self):
        """should_move_forward is a machine signal, not a UI choice."""
        frame = parse(_raw("bot_story_validation.json"))
        assert frame.options == []

    def test_msg_is_preserved(self):
        frame = parse(_raw("bot_story_validation.json"))
        assert "proceed" in frame.msg.lower()

    def test_step_is_preserved(self):
        frame = parse(_raw("bot_story_validation.json"))
        assert frame.step == 5


class TestExtraContentShape3RagSources:
    """Shape 3: {sources: [...]} — free-flow RAG citations.

    Each source becomes a ParsedOption so the UI can render tappable links.
    """

    def test_kind_is_bot_final(self):
        frame = parse(_raw("bot_rag_sources.json"))
        assert frame.kind is FrameKind.BOT_FINAL

    def test_sources_become_options(self):
        frame = parse(_raw("bot_rag_sources.json"))
        assert len(frame.options) == 2

    def test_first_source_id_is_url(self):
        frame = parse(_raw("bot_rag_sources.json"))
        assert frame.options[0].id == "https://example.com/teaching-101"

    def test_first_source_label_is_title(self):
        frame = parse(_raw("bot_rag_sources.json"))
        assert frame.options[0].label == "Teaching 101"

    def test_first_source_value_is_url(self):
        frame = parse(_raw("bot_rag_sources.json"))
        assert frame.options[0].value == "https://example.com/teaching-101"

    def test_all_three_shapes_normalise_to_same_type(self):
        """All three shapes produce list[ParsedOption] — one type, not three."""
        frames = [
            parse(_raw("bot_state_machine_options.json")),
            parse(_raw("bot_story_validation.json")),
            parse(_raw("bot_rag_sources.json")),
        ]
        for f in frames:
            assert isinstance(f.options, list)
            for opt in f.options:
                assert isinstance(opt, ParsedOption)


# ---------------------------------------------------------------------------
# 4. System / error frames
# ---------------------------------------------------------------------------

class TestSystemFrames:

    def test_source_system_yields_system_kind(self):
        frame = parse(_raw("system_error.json"))
        assert frame.kind is FrameKind.SYSTEM

    def test_system_error_message(self):
        frame = parse(_raw("system_error.json"))
        assert frame.error == "Session timed out"

    def test_system_frame_has_no_options(self):
        frame = parse(_raw("system_error.json"))
        assert frame.options == []

    def test_system_frame_finish_reason_none(self):
        frame = parse(_raw("system_error.json"))
        assert frame.finish_reason is None

    def test_error_field_on_bot_frame_promotes_to_system(self):
        """A frame with source:bot but error set is treated as system/error."""
        raw = json.dumps({
            "text": {"msg": "", "source": "bot", "error": "upstream error", "finish_reason": None}
        })
        frame = parse(raw)
        assert frame.kind is FrameKind.SYSTEM
        assert frame.error == "upstream error"


# ---------------------------------------------------------------------------
# 5. Legacy envelope types
# ---------------------------------------------------------------------------

class TestLegacyEnvelopes:
    """Node bot reference client variants — no 'text' dict, top-level 'type'."""

    def test_session_end_is_system(self):
        frame = parse(_raw("legacy_session_end.json"))
        assert frame.kind is FrameKind.SYSTEM
        assert frame.source == "system"

    def test_session_end_msg(self):
        frame = parse(_raw("legacy_session_end.json"))
        assert "ended" in frame.msg.lower()

    def test_bot_response_is_bot_final(self):
        frame = parse(_raw("legacy_bot_response.json"))
        assert frame.kind is FrameKind.BOT_FINAL
        assert frame.finish_reason == "stop"
        assert frame.msg == "Hello! How can I help you today?"

    def test_options_envelope_produces_parsed_options(self):
        frame = parse(_raw("legacy_options.json"))
        assert frame.kind is FrameKind.BOT_FINAL
        assert len(frame.options) == 2
        assert frame.options[0].label == "Option A"
        assert frame.options[1].value == "b"

    def test_error_envelope_is_system_with_error(self):
        frame = parse(_raw("legacy_error.json"))
        assert frame.kind is FrameKind.SYSTEM
        assert "Authentication failed" in frame.error

    def test_unknown_type_yields_system_with_note(self):
        raw = json.dumps({"type": "heartbeat", "ts": 1234567890})
        frame = parse(raw)
        assert frame.kind is FrameKind.SYSTEM
        assert "heartbeat" in frame.error

    def test_text_as_string_not_dict(self):
        """Degenerate shape: {'text': 'raw string'} — treat as bot chunk."""
        raw = json.dumps({"text": "Hello world"})
        frame = parse(raw)
        assert frame.kind is FrameKind.BOT_CHUNK
        assert frame.msg == "Hello world"

    def test_text_as_string_with_finish_reason(self):
        raw = json.dumps({"text": "Goodbye", "finish_reason": "stop"})
        frame = parse(raw)
        assert frame.kind is FrameKind.BOT_FINAL
        assert frame.finish_reason == "stop"


# ---------------------------------------------------------------------------
# 6. Tolerance — malformed input never raises
# ---------------------------------------------------------------------------

class TestTolerance:
    """parse() must NEVER raise regardless of input.

    Acceptance criterion: malformed JSON yields an error Frame, not an
    exception. The channel logs these and continues.
    """

    @pytest.mark.parametrize("bad_input", [
        "",                          # empty string
        "   ",                       # whitespace only
        "not json at all",           # plain text
        "{unclosed",                 # truncated JSON
        "null",                      # JSON null — not a dict
        "[]",                        # JSON array — not a dict
        "42",                        # JSON number
        b"",                         # empty bytes
        b"\xff\xfe bad utf-8 \x00",  # bad bytes
        '{"text": [1, 2, 3]}',       # text is an array not a dict/str
    ])
    def test_malformed_input_yields_error_frame_not_exception(self, bad_input):
        frame = parse(bad_input)
        assert frame.kind is FrameKind.ERROR, (
            f"Expected ERROR frame for input {bad_input!r}, got {frame.kind}"
        )
        assert frame.error != ""
        assert isinstance(frame.options, list)  # always a list, never None

    def test_error_frame_has_empty_msg(self):
        frame = parse("not json")
        assert frame.msg == ""

    def test_extra_content_wrong_type_yields_empty_options(self):
        """extra_content that is a list instead of a dict → no crash, empty options."""
        raw = json.dumps({
            "text": {
                "msg": "hi", "source": "bot",
                "finish_reason": "stop",
                "extra_content": ["unexpected", "list"],
            }
        })
        frame = parse(raw)
        assert frame.kind is FrameKind.BOT_FINAL
        assert frame.options == []

    def test_step_is_non_integer_coerced(self):
        """Non-integer step coerces to 0 rather than raising."""
        raw = json.dumps({
            "text": {"msg": "hi", "source": "bot", "finish_reason": "stop", "step": "NaN"}
        })
        frame = parse(raw)
        assert frame.step == 0

    def test_missing_all_keys_in_text_is_tolerated(self):
        """A 'text' dict with no known keys still parses cleanly."""
        raw = json.dumps({"text": {"unknown_key": "value"}})
        frame = parse(raw)
        # source is empty → treated as bot turn
        assert frame.kind in (FrameKind.BOT_CHUNK, FrameKind.BOT_FINAL, FrameKind.SYSTEM)
        assert isinstance(frame.options, list)

    def test_no_text_and_no_type_is_error(self):
        """Dict with neither 'text' nor 'type' is unrecognised → ERROR."""
        raw = json.dumps({"completely": "alien", "shape": True})
        frame = parse(raw)
        assert frame.kind is FrameKind.ERROR


# ---------------------------------------------------------------------------
# 7. Defect 4 — internal LLM payload leaked into `msg`
#
# Reproduced verbatim from a live QA interview: the model answered with a LIST
# holding a response object AND a tool call. Mitra's guard
# (guided_guest_tool_call.py:63-68) only tests dict/str, so a list is neither a
# function call nor text and the raw object went out as the bot's reply.
# json.dumps put it on the wire as an ARRAY, and str()ing that printed
# "[{'response': '', 'name': 'get_state_information', ...}]" into the user's
# chat bubble.
# ---------------------------------------------------------------------------

# The exact payload observed, byte for byte.
LEAKED_TOOL_CALL = [
    {
        "response": "",
        "response_reason": (
            "User provided a clear reason for the problem. Proceeding to "
            "function call to get state information for the next state."
        ),
    },
    {
        "name": "get_state_information",
        "parameters": {
            "state_name": "MAIN_CHALLENGE",
            "reason": (
                "The user provided a clear reason for the problem, which is the "
                "transportation issue leading to less attendance. This reason "
                "logically explains the problem, so we are proceeding to the "
                "next state."
            ),
        },
    },
]


class TestLeakedControlPayload:

    def _parse_leak(self, payload=None):
        raw = json.dumps({
            "text": {
                "msg": LEAKED_TOOL_CALL if payload is None else payload,
                "source": "bot",
                "finish_reason": "stop",
                "step": 9,
            }
        })
        return parse(raw)

    def test_the_observed_payload_never_reaches_the_user_as_text(self):
        """THE regression test. Before this, msg was str(list) and the whole
        tool call rendered in the chat bubble."""
        frame = self._parse_leak()

        assert frame.control_payload is True
        assert frame.msg == "", f"leaked payload became user-visible text: {frame.msg!r}"
        for artefact in ("get_state_information", "response_reason", "MAIN_CHALLENGE", "{'"):
            assert artefact not in frame.msg

    def test_frame_is_still_a_well_formed_bot_final(self):
        """Suppressing the text must not corrupt the rest of the turn -- the
        channel still needs finish_reason to know the turn ended, or it would
        block until the idle-gap backstop on every occurrence."""
        frame = self._parse_leak()
        assert frame.kind is FrameKind.BOT_FINAL
        assert frame.finish_reason == "stop"
        assert frame.step == 9

    def test_embedded_response_text_is_recovered_when_present(self):
        """`response` is the key Mitra's own handlers treat as the user-facing
        reply, so when the model does fill it in, that IS the turn."""
        payload = [
            {"response": "Thanks. What happened next?", "response_reason": "advancing"},
            {"name": "get_state_information", "parameters": {"state_name": "NEXT"}},
        ]
        frame = self._parse_leak(payload)

        assert frame.msg == "Thanks. What happened next?"
        assert frame.control_payload is True

    def test_dict_shaped_leak_is_handled_too(self):
        """The list shape is what was observed, but a bare dict reaches the
        same wire path and must not str() either."""
        frame = self._parse_leak({"name": "get_state_information", "parameters": {"state_name": "X"}})
        assert frame.control_payload is True
        assert frame.msg == ""

    def test_ordinary_string_messages_are_untouched(self):
        """The guard keys off STRUCTURE, so nothing about normal text changes
        -- including text that quotes the tool name."""
        for text in ("What are the reasons for the problem?", "Use get_state_information to proceed", "[]", "{}"):
            raw = json.dumps({"text": {"msg": text, "source": "bot", "finish_reason": "stop"}})
            frame = parse(raw)
            assert frame.msg == text
            assert frame.control_payload is False

    def test_numeric_msg_keeps_its_lenient_coercion(self):
        raw = json.dumps({"text": {"msg": 42, "source": "bot", "finish_reason": "stop"}})
        frame = parse(raw)
        assert frame.msg == "42"
        assert frame.control_payload is False

    def test_deeply_nested_payload_does_not_raise_or_hang(self):
        """parse() never raises (module contract) and the walk is depth-capped,
        so a pathological payload from upstream cannot blow the stack."""
        nested = {"response": {}}
        cursor = nested
        for _ in range(200):
            cursor["response"] = {"response": {}}
            cursor = cursor["response"]

        frame = self._parse_leak(nested)
        assert frame.kind is FrameKind.BOT_FINAL
        assert frame.control_payload is True
        assert isinstance(frame.msg, str)

    def test_user_echo_of_a_leaked_payload_is_still_discarded(self):
        """A control payload on a user-echo frame must keep USER_ECHO kind, or
        the channel would stop discarding it (§1.2)."""
        raw = json.dumps({"text": {"msg": LEAKED_TOOL_CALL, "source": "user", "finish_reason": None}})
        frame = parse(raw)
        assert frame.kind is FrameKind.USER_ECHO
        assert frame.msg == ""
