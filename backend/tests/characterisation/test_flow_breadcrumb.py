"""Item 7 -- the `flow` breadcrumb.

Exact reproduction of app.py:43-56.

VERIFIED, and worth knowing before module 2.5: the current client does NOT
consume this field. static/js/main.js reads only `status`, `agent_name`,
`response` and `error` (main.js:279-287) -- there is no `renderFlow`, and
index.html has no breadcrumb container. `flow` is computed on every turn and
discarded.

It is pinned anyway, for two reasons:

  * it is part of the published response contract, and module 2.5 must derive
    it from the messages table rather than from process globals while producing
    identical output;
  * the truncation and de-duplication rules encode real product decisions that
    would otherwise be lost when the globals are deleted.

The comments below therefore say what the SERVER does, not what the client
renders.
"""

from __future__ import annotations

import pytest

from tests.characterisation.conftest import DEFAULT_AGENT, chat

TRUNCATION_LIMIT = 60
TRUNCATED_PREFIX = 57


def test_short_title_is_stored_whole(client, script):
    script.queue("reply")

    _, body = chat(client, "short question", DEFAULT_AGENT)

    assert body["flow"]["title"] == "short question"


@pytest.mark.parametrize("length", [1, 30, 59, 60])
def test_title_at_or_below_the_limit_is_not_truncated(client, script, length):
    """app.py:47 -- `len(msg) <= 60` keeps the message whole. 60 is the boundary."""
    message = "x" * length
    script.queue("reply")

    _, body = chat(client, message, DEFAULT_AGENT)

    assert body["flow"]["title"] == message
    assert len(body["flow"]["title"]) == length


@pytest.mark.parametrize("length", [61, 80, 200])
def test_title_above_the_limit_is_truncated_to_57_plus_ellipsis(
    client, script, length
):
    """app.py:47 -- `msg[:57] + "..."`, giving a total of exactly 60 characters."""
    message = "y" * length
    script.queue("reply")

    _, body = chat(client, message, DEFAULT_AGENT)

    title = body["flow"]["title"]
    assert title == "y" * TRUNCATED_PREFIX + "..."
    assert len(title) == TRUNCATION_LIMIT


def test_title_is_set_once_and_never_replaced(client, script):
    """app.py:45-47 -- guarded by `if flow_title is None`.

    A later, longer message must NOT overwrite the title.
    """
    script.queue("first")
    _, first = chat(client, "the first question", DEFAULT_AGENT)

    script.queue("second")
    _, second = chat(client, "a completely different and much longer question", DEFAULT_AGENT)

    assert first["flow"]["title"] == "the first question"
    assert second["flow"]["title"] == "the first question"


def test_consecutive_same_agent_turns_do_not_duplicate_a_stop(client, script):
    """app.py:48-49 -- appends only when the agent differs from the last stop."""
    for _ in range(3):
        script.queue("reply")
        _, body = chat(client, "another question", DEFAULT_AGENT)

    assert body["flow"]["stops"] == [DEFAULT_AGENT]
    assert body["flow"]["current_index"] == 0


def test_switching_agents_appends_a_stop(client, script, second_llm_agent):
    second_name, _key = second_llm_agent
    script.queue("a")
    chat(client, "one", DEFAULT_AGENT)
    script.queue("b")
    _, body = chat(client, "two", second_name)

    assert body["flow"]["stops"] == [DEFAULT_AGENT, second_name]
    assert body["flow"]["current_index"] == 1


def test_returning_to_an_earlier_agent_appends_again(client, script, second_llm_agent):
    """Dedup is against the LAST stop only, not the whole list -- so an agent can
    legitimately appear more than once in the breadcrumb."""
    second_name, _key = second_llm_agent
    script.queue("a")
    chat(client, "one", DEFAULT_AGENT)
    script.queue("b")
    chat(client, "two", second_name)
    script.queue("c")
    _, body = chat(client, "three", DEFAULT_AGENT)

    assert body["flow"]["stops"] == [DEFAULT_AGENT, second_name, DEFAULT_AGENT]
    assert body["flow"]["current_index"] == 2


def test_interleaved_repeats_collapse_only_when_adjacent(client, script, second_llm_agent):
    second_name, _key = second_llm_agent
    sequence = [DEFAULT_AGENT, DEFAULT_AGENT, second_name, second_name, DEFAULT_AGENT]
    for agent in sequence:
        script.queue("reply")
        _, body = chat(client, "q", agent)

    assert body["flow"]["stops"] == [DEFAULT_AGENT, second_name, DEFAULT_AGENT]


def test_current_index_always_trails_the_stops_length(client, script, second_llm_agent):
    second_name, _key = second_llm_agent
    for agent in [DEFAULT_AGENT, second_name, DEFAULT_AGENT]:
        script.queue("reply")
        _, body = chat(client, "q", agent)
        flow = body["flow"]
        assert flow["current_index"] == len(flow["stops"]) - 1


def test_router_selected_agent_is_recorded_in_the_breadcrumb(client, script, second_llm_agent):
    """The stop records who ANSWERED, not what the client asked for."""
    import json
    second_name, second_key = second_llm_agent
    script.queue(json.dumps({"agent_key": second_key, "confidence": 0.9}))  # classification
    script.queue("reply")

    _, body = chat(client, "route me", "Saarthi")

    assert body["flow"]["stops"] == [second_name]
