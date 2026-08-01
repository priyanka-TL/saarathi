"""Item 3 -- the router path.

Reached by `agent_name="Saarthi"` or by omitting `agent_name` entirely
(app.py:79 -- both take the same branch).

Gate 4: The RouterService emits a JSON prompt asking for an agent_key.
The scripted model must return a JSON-parseable string for routing to work.
"""

from __future__ import annotations

import json

import pytest

from tests.characterisation.conftest import AGENT_NAMES, DEFAULT_AGENT, chat

# Map display names to their agent keys (as registered in the YAML fixtures).
_NAME_TO_KEY = {
    "General Support Agent": "general_support",
}


def _classify_json(key: str, confidence: float = 0.9) -> str:
    """Return a JSON classification string the RouterService will accept."""
    return json.dumps({"agent_key": key, "confidence": confidence})


@pytest.mark.parametrize("agent_name", AGENT_NAMES)
def test_classification_selects_the_named_agent(client, script, agent_name):
    key = _NAME_TO_KEY[agent_name]
    script.queue(_classify_json(key))  # classification
    script.queue("agent reply")        # the agent itself

    status, body = chat(client, "route me", "Saarthi")

    assert status == 200
    assert body["agent_name"] == agent_name
    assert body["response"] == "agent reply"
    assert len(script.calls) == 2, "one classification call, then one agent call"


def test_omitting_agent_name_takes_the_router_path(client, script):
    """A falsy agent_name is treated exactly like 'Saarthi'."""
    script.queue(_classify_json("general_support"))
    script.queue("reply")

    status, body = chat(client, "route me")  # no agent_name at all

    assert status == 200
    assert body["agent_name"] == DEFAULT_AGENT
    assert len(script.calls) == 2


def test_router_prompt_is_built_from_the_live_registry(client, script):
    """The prompt is assembled from every agent's name AND description,
    so registering an agent updates routing with no router edit.

    This is the single best idea in the codebase. Module 4.3 preserves it.
    """
    script.queue(_classify_json("general_support"))
    script.queue("reply")

    chat(client, "route me", "Saarthi")

    router_prompt = script.system_prompt(0)
    # The prompt is built from the WHOLE live registry, not just the
    # LLM-type agents in AGENT_NAMES -- Record Stories / Capture Discussions
    # are router_selectable too.
    for name in AGENT_NAMES + ["Record Stories", "Capture Discussions"]:
        assert name in router_prompt, f"{name!r} missing from the router prompt"
    assert "Handles general inquiries, business hours" in router_prompt
    assert "captures a practitioner's improvement story" in router_prompt


def test_router_prompt_names_the_fallback_agent(client, script):
    """The prompt includes a fallback instruction using the default agent's key."""
    script.queue(_classify_json("general_support"))
    script.queue("reply")

    chat(client, "route me", "Saarthi")

    # RouterService uses agent_key for the fallback instruction
    assert "general_support" in script.system_prompt(0)
    assert "If nothing fits" in script.system_prompt(0)


def test_router_is_history_aware(client, script):
    """Gate 4: the router receives prior conversation turns as context.

    RouterService._router_messages() appends the last 6 history messages,
    so on turn 2 the router sees turn 1's message too.
    """
    script.queue(_classify_json("general_support"))
    script.queue("first reply")
    chat(client, "my first message", "Saarthi")

    script.queue(_classify_json("general_support"))
    script.queue("second reply")
    chat(client, "Priya", "Saarthi")

    router_second_call = script.calls[2]
    human_messages = [m for m in router_second_call if m.type == "human"]
    # Should see BOTH messages in context
    contents = [m.content for m in human_messages]
    assert "Priya" in contents
    assert any("my first message" in c for c in contents), (
        "router is now history-aware -- prior messages appear in context"
    )


def test_unmatched_classification_falls_back_to_the_default_agent(client, script):
    """Routing NEVER fails -- unrecognised key falls through to default."""
    script.queue(_classify_json("no_such_key"))
    script.queue("fallback reply")

    status, body = chat(client, "route me", "Saarthi")

    assert status == 200
    assert body["agent_name"] == DEFAULT_AGENT


def test_empty_classification_falls_back_to_the_default_agent(client, script):
    """Empty or non-JSON output falls through to gate 5 (default)."""
    script.queue("")
    script.queue("fallback reply")

    status, body = chat(client, "route me", "Saarthi")

    assert status == 200
    assert body["agent_name"] == DEFAULT_AGENT


def test_classification_exception_falls_back_to_the_default_agent(client, script):
    """An LLM exception during routing falls through to gate 5 (default)."""
    script.queue_error(RuntimeError("classification exploded"))
    script.queue("fallback reply")

    status, body = chat(client, "route me", "Saarthi")

    assert status == 200
    assert body["agent_name"] == DEFAULT_AGENT


def test_exact_key_matching_resolves_correctly(client, script, second_llm_agent):
    """RouterService uses get_by_key_exact() -- the JSON key must match exactly.

    The old orchestrator.py:62-65 used substring matching, which caused
    agent-name collisions. The new router never matches by substring.
    """
    name, key = second_llm_agent
    script.queue(_classify_json(key))
    script.queue("reply")

    _, body = chat(client, "my app crashed", "Saarthi")

    assert body["agent_name"] == name


def test_key_matching_is_case_normalised(client, script, second_llm_agent):
    """get_by_key_exact() strips and lowercases the key before lookup."""
    name, key = second_llm_agent
    script.queue(_classify_json(key.upper(), 0.9))
    script.queue("reply")

    _, body = chat(client, "my app crashed", "Saarthi")

    assert body["agent_name"] == name
