"""Pins the `routing.intent` grid migration 0024 seeds.

The grid is what lets the router recognise a request phrased in a way no fixed
keyword list contains -- "I want to start a discussion" matches none of the six
seeded discussion keywords. Getting it wrong is quiet in both directions: too
narrow and the LLM classifier comes back on every turn, too broad and an
ordinary interview answer yields the user out of a conversation.

These read the migration's own `apply()` rather than restating its values, for
the reason 0019 gives: a pin that restates an edit can agree with itself while
disagreeing with the database.
"""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from app.domain.agent_spec import AgentSpec
from app.services.intent_match import intent_match, keywords_match, tokens

_VERSIONS = Path(__file__).parents[2] / "migrations" / "versions"
_adapter = TypeAdapter(AgentSpec)


def _load_migration(name: str, path: Path):
    """Imported as a file rather than a module: `migrations/versions` is not a
    package and alembic revision filenames are not importable identifiers."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_intent = _load_migration("_intent_0024", _VERSIONS / "0024_seed_routing_intent.py")


def _grid(agent_key: str):
    return _intent.INTENT_BY_AGENT[agent_key]


# ---------------------------------------------------------------------------
# The edit itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("agent_key", ["record_stories", "capture_discussion"])
def test_apply_adds_a_grid_and_touches_nothing_else(agent_key):
    before = {"key": agent_key, "routing": {"keywords": ["x"], "priority": 90}}
    after = _intent.apply(copy.deepcopy(before), agent_key)

    assert after["routing"]["intent"] == _grid(agent_key)
    # Every other routing field survives untouched -- this migration adds, it
    # does not rewrite.
    assert after["routing"]["keywords"] == ["x"]
    assert after["routing"]["priority"] == 90
    assert after["key"] == agent_key


def test_apply_is_a_no_op_for_an_agent_with_no_grid():
    """`saathi` and `general_support` are not yield TARGETS, so a grid on
    either would never be read on that path."""
    before = {"key": "saathi", "routing": {"keywords": ["saathi"]}}
    assert _intent.apply(copy.deepcopy(before), "saathi") == before


def test_apply_is_idempotent():
    """A re-run must not churn a config version. `upgrade()` detects that by
    comparing the returned config to the row's, so the no-op has to be exact."""
    config = {"key": "record_stories", "routing": {"keywords": []}}
    once = _intent.apply(copy.deepcopy(config), "record_stories")
    twice = _intent.apply(copy.deepcopy(once), "record_stories")
    assert twice == once


def test_apply_does_not_overwrite_an_operators_own_grid():
    mine = {"verbs": ["mine"], "nouns": ["thing"], "max_distance": 2}
    config = {"key": "record_stories", "routing": {"intent": mine}}
    assert _intent.apply(copy.deepcopy(config), "record_stories")["routing"]["intent"] == mine


# ---------------------------------------------------------------------------
# The result validates as a real spec
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("agent_key", ["record_stories", "capture_discussion"])
def test_the_edited_config_validates_through_the_real_adapter(agent_key):
    """`RoutingSpec` sets extra="forbid", so a misspelt key inside the grid
    would make the whole agent unloadable -- AgentRegistry.reload() skips an
    agent whose config does not validate, and it vanishes from routing and the
    sidebar with only a log line."""
    raw = {
        "schema_version": 1,
        "key": agent_key,
        "name": "X",
        "description": "d",
        "agent_type": "llm",
        "prompt": "p",
        "model": {"provider": "openrouter", "name": "m", "max_tokens": 1024},
        "routing": {"keywords": []},
    }
    spec = _adapter.validate_python(_intent.apply(raw, agent_key))
    assert spec.routing.intent is not None
    assert spec.routing.intent.nouns == _grid(agent_key)["nouns"]
    assert spec.routing.intent.max_distance == 4, "the default is deliberate"


# ---------------------------------------------------------------------------
# What the seeded values actually match -- the reason for the change
# ---------------------------------------------------------------------------

DISCUSSION_KEYWORDS = [
    "capture discussion", "capture a discussion", "record discussion",
    "meeting notes", "community discussion", "chaupal",
]


@pytest.mark.parametrize("message", [
    "I want to start a discussion",
    "let's begin a discussion about the dropouts",
    "can you record the meeting",
    "I want to summarise our meeting",
])
def test_the_discussion_grid_matches_phrasings_no_keyword_covers(message):
    grid = _grid("capture_discussion")
    assert keywords_match(message, DISCUSSION_KEYWORDS) is False, \
        "premise: no seeded keyword covers this phrasing"
    assert intent_match(
        tokens(message), grid["verbs"], grid["nouns"], grid["max_distance"],
    ) is True


@pytest.mark.parametrize("message", [
    "I want to start a story",
    "help me write my story",
    "I'd like to share my experience from last term",
])
def test_the_story_grid_matches_phrasings_no_keyword_covers(message):
    grid = _grid("record_stories")
    assert intent_match(
        tokens(message), grid["verbs"], grid["nouns"], grid["max_distance"],
    ) is True


@pytest.mark.parametrize("answer", [
    # Ordinary interview answers. Each of these used to cost an LLM call on
    # every Saathi turn; a grid loose enough to match one would be worse than
    # the cost it removes -- it would yield the user out mid-interview.
    "yes",
    "40 students",
    "Kolar district",
    "attendance dropped after the monsoon",
    "we discussed it at length last week",
    "the story here is that nobody came",
    "there was a meeting but nothing changed",
])
@pytest.mark.parametrize("agent_key", ["record_stories", "capture_discussion"])
def test_neither_grid_matches_an_ordinary_interview_answer(answer, agent_key):
    grid = _grid(agent_key)
    assert intent_match(
        tokens(answer), grid["verbs"], grid["nouns"], grid["max_distance"],
    ) is False


def test_the_discussion_nouns_exclude_notes():
    """"notes" is an ordinary word in an interview answer ("I took notes"), and
    the nouns are ALSO what the ambiguity gate tests -- so including it would
    re-open the per-turn LLM call this change exists to remove. The phrase
    "meeting notes" is already a keyword and covers the real request."""
    assert "notes" not in _grid("capture_discussion")["nouns"]
