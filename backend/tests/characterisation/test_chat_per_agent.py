"""Item 2 -- POST /api/chat, per agent, via explicit selection.

Asserts the response KEY SET and TYPES rather than the LLM text: the text is not
a contract, but the shape is. `agent_name` is asserted exactly, because
static/js/main.js:139 round-trips it as a selector.

Health & Wellness, Technical Support, and Research were removed from the live
application (src/config/agents/*.yaml deleted) -- General Support is the only
LLM-type named agent left. Research was also the ONLY agent configured with
`tools:`, so the tool-loop tests that used to run against it (bound-tool
ordering, the tool-calling loop, its 3-iteration bound, tool results fed back
to the model) have no remaining subject in the live app and are removed here,
not adapted -- there is currently no tool-using agent left to characterise.
"""

from __future__ import annotations

import pytest

from tests.characterisation.conftest import (
    AGENT_NAMES,
    TOOLLESS_AGENTS,
    chat,
)


def assert_matches_shape(body: dict, golden_shape: dict) -> None:
    """Assert a chat response against the goldened shape."""
    assert set(body) == set(golden_shape["top_level_keys"])
    assert isinstance(body["agent_name"], str)
    assert isinstance(body["response"], str)
    assert body["status"] == golden_shape["constants"]["status"]

    # Additive module 5.7 keys: always [] / null for every agent exercised in
    # this file (all agent_type: llm -- pin_session is false, so
    # SessionService.open_for never creates a session for them).
    assert body["options"] == []
    # Pinned as an EMPTY LIST, not as absent: the key is part of the contract
    # for every agent, and an LLM agent producing no documents must still send
    # it so a client never has to distinguish "no files" from "old server".
    assert body["attachments"] == []
    assert body["session"] is None

    flow = body["flow"]
    assert set(flow) == set(golden_shape["flow_keys"])
    assert isinstance(flow["current_index"], int)
    assert isinstance(flow["stops"], list)
    assert all(isinstance(stop, str) for stop in flow["stops"])
    assert isinstance(flow["title"], str)


@pytest.mark.parametrize("agent_name", TOOLLESS_AGENTS)
def test_toolless_agent_returns_the_documented_shape(
    client, script, golden, agent_name
):
    script.queue("A scripted reply.")

    status, body = chat(client, "hello", agent_name)

    assert status == 200
    assert_matches_shape(body, golden("chat_response_shape"))
    assert body["agent_name"] == agent_name
    assert body["response"] == "A scripted reply."


@pytest.mark.parametrize("agent_name", TOOLLESS_AGENTS)
def test_toolless_agent_makes_exactly_one_llm_call(client, script, agent_name):
    """Explicit selection bypasses the router entirely (app.py:79-83).

    One call, not two. Module 4.3 preserves this as routing gate 1.
    """
    script.queue("reply")

    chat(client, "hello", agent_name)

    assert len(script.calls) == 1


@pytest.mark.parametrize("agent_name", TOOLLESS_AGENTS)
def test_toolless_agent_sends_its_system_prompt(client, script, agent_name):
    """The agent's system_prompt leads the message list.

    Module 3.8 transcribes these prompts into YAML byte-for-byte, so pin that
    they are actually sent.
    """
    script.queue("reply")

    chat(client, "hello", agent_name)

    system = script.system_prompt(0)
    assert system
    assert "Assistant" in system


@pytest.mark.parametrize("agent_name", AGENT_NAMES)
def test_flow_records_the_responding_agent(client, script, agent_name):
    script.queue("reply")

    _, body = chat(client, "hello", agent_name)

    assert body["flow"]["stops"] == [agent_name]
    assert body["flow"]["current_index"] == 0
