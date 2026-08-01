"""Exit-keyword matching -- the guard on RouterService Gate 1 / Gate 2.

TWO distinct regressions are pinned here.

1. `_COMMAND_PUNCTUATION` was referenced by `_normalise_command` but defined
   nowhere in the module, so `_is_exit` raised NameError. Gate 2 runs it on
   every turn of a pinned conversation where the client sent no `agent_key` --
   i.e. every turn after a browser refresh or a resume from history -- so
   /api/chat returned HTTP 500 INTERNAL for the whole rest of the interview.

2. Before that, `_is_exit` was a SUBSTRING test, and the bare RoutingSpec
   defaults are ["/exit", "cancel", "stop"]. Ordinary interview answers
   silently aborted the interview: the session was abandoned, the conversation
   unpinned, and the default agent answered plausibly, so the user had no idea
   their discussion would never produce a report. The three FALSE_POSITIVES
   below are real messages taken from the live database (conversation
   836a4305-cfa7-4cbd-8917-ee686ec31511 and its siblings, all abandoned with
   error='user_exit' at step 1).
"""
from __future__ import annotations

import pytest

from app.services.router_service import RouterService

DEFAULT_EXIT_KEYWORDS = ["/exit", "cancel", "stop"]
PHRASE_EXIT_KEYWORDS = ["/exit", "cancel interview", "stop interview", "end discussion"]

# Real interview answers that must NEVER be read as an exit command.
FALSE_POSITIVES = [
    "Children stopped coming to school after the monsoon",
    "The meeting was cancelled last week",
    "We discussed the bus stop near the school",
    "we had to stop the dropouts",
    "Please don't cancel the programme",
]


def test_is_exit_does_not_raise():
    """The NameError regression: this call alone was a 500 on every Gate 2 turn."""
    assert RouterService._is_exit("cancel", DEFAULT_EXIT_KEYWORDS) is True


@pytest.mark.parametrize("text", FALSE_POSITIVES)
def test_ordinary_answers_do_not_abandon_the_interview(text):
    assert RouterService._is_exit(text, DEFAULT_EXIT_KEYWORDS) is False


@pytest.mark.parametrize("text", ["cancel", "  cancel  ", "Cancel.", "STOP", "/exit", " /exit! "])
def test_bare_commands_still_exit(text):
    assert RouterService._is_exit(text, DEFAULT_EXIT_KEYWORDS) is True


@pytest.mark.parametrize("text", ["cancel interview", "Stop Interview.", "end discussion", "/exit"])
def test_phrase_keywords_match_exactly(text):
    assert RouterService._is_exit(text, PHRASE_EXIT_KEYWORDS) is True


@pytest.mark.parametrize("text", ["stop", "cancel", "the discussion had to stop"])
def test_phrase_keywords_ignore_the_bare_words(text):
    """capture_discussion moved to phrase keywords precisely so a one-word
    answer ("stop") to "any other challenge?" cannot end the interview."""
    assert RouterService._is_exit(text, PHRASE_EXIT_KEYWORDS) is False


@pytest.mark.parametrize("text", ["", "   ", None])
def test_empty_text_never_exits(text):
    assert RouterService._is_exit(text, DEFAULT_EXIT_KEYWORDS) is False


def test_blank_keywords_are_ignored():
    """A stray empty string in the YAML list must not make every message an
    exit command."""
    assert RouterService._is_exit("hello", ["", "   ", None]) is False
