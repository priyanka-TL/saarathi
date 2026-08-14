"""The deterministic router matchers.

Every case here is a routing decision made WITHOUT a model. The precision cases
are the important half: a false match on the yield path abandons a conversation
the user was still in the middle of, which they cannot undo, while a missed one
costs them a rephrase. See app/services/intent_match.py.
"""
from __future__ import annotations

import pytest

from app.services.intent_match import (
    intent_match,
    keywords_match,
    mentions_any,
    phrase_match,
    subject_tokens,
    tokens,
)

# The real seeded keyword lists (migration 0010), so these tests fail if the
# shipped configuration drifts away from what they claim to prove.
STORY_KEYWORDS = [
    "record a story", "capture story", "share my story", "tell my story",
    "listening at scale", "story capture", "my story",
]
DISCUSSION_KEYWORDS = [
    "capture discussion", "capture a discussion", "record discussion",
    "meeting notes", "community discussion", "chaupal",
]


# ---------------------------------------------------------------------------
# tokens
# ---------------------------------------------------------------------------

def test_tokens_lowercases_and_drops_punctuation():
    assert tokens("  Capture, a STORY! ") == ["capture", "a", "story"]


def test_tokens_keeps_apostrophes_inside_a_word():
    """"let's" must be ONE token. Split into {let, s}, the stray "s" would
    occupy a filler slot it has no right to and loosen every gap by one."""
    assert tokens("let's record") == ["let's", "record"]


def test_tokens_handles_devanagari():
    """These agents serve en/hi/kn/te. A tokeniser that stripped Indic script
    would return no tokens, every match would miss, and every Hindi turn would
    fall through to the LLM -- the exact cost this module removes."""
    assert tokens("मैं कहानी रिकॉर्ड") == ["मैं", "कहानी", "रिकॉर्ड"]


@pytest.mark.parametrize("empty", [None, "", "   ", "!!!"])
def test_tokens_of_nothing_is_empty(empty):
    assert tokens(empty) == []


# ---------------------------------------------------------------------------
# phrase_match -- RECALL: the cases the old substring test got wrong
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("message", [
    "I wanted to capture a story",       # THE reported sentence
    "capture the story",
    "capture our new story",
    "I want to capture my story now",
])
def test_an_inserted_filler_word_no_longer_breaks_the_match(message):
    """`"capture story" in "capture a story"` is False, which is why the LLM
    classifier had to exist. It does not any more."""
    assert phrase_match(tokens(message), tokens("capture story")) is True


def test_the_reported_sentence_now_matches_the_real_seeded_keywords():
    """End to end against the shipped list, not a hand-made one."""
    assert keywords_match("I wanted to capture a story", STORY_KEYWORDS) is True


def test_record_the_discussion_matches_record_discussion():
    assert keywords_match("let's record the discussion", DISCUSSION_KEYWORDS) is True


def test_an_exact_phrase_still_matches():
    """The old behaviour is a strict subset of the new one -- a keyword that
    matched as a substring must still match."""
    assert keywords_match("I want to record a story today", STORY_KEYWORDS) is True


# ---------------------------------------------------------------------------
# phrase_match -- PRECISION: the cases that must NOT match
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("message", [
    # The one that matters most: a legitimate Saathi interview answer.
    "capture what the head teacher said about the story",
    "the story of what the teacher told us",
    "we discussed how to capture the attention of the story tellers",
])
def test_a_content_word_in_the_gap_blocks_the_match(message):
    """THE SAFETY PROPERTY. An unbounded gap would match all of these and yield
    the user out of a conversation they were still answering."""
    assert phrase_match(tokens(message), tokens("capture story")) is False


def test_the_gap_is_bounded_even_when_every_word_is_filler():
    """Three fillers exceeds _MAX_GAP. Two is the limit."""
    assert phrase_match(tokens("capture a new story"), tokens("capture story")) is True
    assert phrase_match(
        tokens("capture this one another story"), tokens("capture story"),
    ) is False


def test_order_is_required():
    assert phrase_match(tokens("story capture"), tokens("capture story")) is False


def test_an_unrelated_sentence_containing_a_keyword_word_does_not_match():
    """"stop" was already known to be dangerous here (see _is_exit); "story"
    and "discussion" are ordinary words in an interview answer."""
    assert keywords_match("I want to stop the dropouts", STORY_KEYWORDS) is False
    assert keywords_match("attendance dropped after the monsoon", STORY_KEYWORDS) is False


@pytest.mark.parametrize("answer", ["yes", "40 students", "Kolar district", "no", ""])
def test_ordinary_interview_answers_match_nothing(answer):
    assert keywords_match(answer, STORY_KEYWORDS) is False
    assert keywords_match(answer, DISCUSSION_KEYWORDS) is False


def test_a_blank_keyword_never_matches_everything():
    """A stray "" in a config's keyword list would otherwise route the whole
    application to one agent."""
    assert keywords_match("anything at all", ["", "   "]) is False


def test_every_start_position_is_tried():
    """A greedy scan committing to the FIRST occurrence of the leading token
    would reject this: the first "record" is followed by the content word
    "notes", while the second one matches."""
    assert phrase_match(
        tokens("record my notes then record the discussion"),
        tokens("record discussion"),
    ) is True


def test_a_noun_phrase_using_the_verb_as_a_noun_does_not_match():
    """"the record of the discussion" is the minutes, not a request to record
    one. "of" is a content word, so the gap rule rejects it -- which is the
    correct outcome and not a limitation."""
    assert phrase_match(
        tokens("show me the record of the discussion"), tokens("record discussion"),
    ) is False


# ---------------------------------------------------------------------------
# intent_match -- the verb x noun grid
# ---------------------------------------------------------------------------

STORY_VERBS = ["record", "capture", "share", "tell", "write", "start", "begin"]
STORY_NOUNS = ["story", "experience"]
DISCUSSION_VERBS = ["record", "capture", "start", "begin", "summarise"]
DISCUSSION_NOUNS = ["discussion", "meeting", "chaupal", "minutes"]


def test_start_a_discussion_matches_the_grid_but_no_seeded_keyword():
    """The phrasing that motivated the grid: no keyword list contains it."""
    assert keywords_match("I want to start a discussion", DISCUSSION_KEYWORDS) is False
    assert intent_match(
        tokens("I want to start a discussion"),
        DISCUSSION_VERBS, DISCUSSION_NOUNS, 4,
    ) is True


def test_share_my_experience_matches():
    assert intent_match(
        tokens("I'd like to share my experience"), STORY_VERBS, STORY_NOUNS, 4,
    ) is True


def test_the_verb_must_come_before_the_noun():
    """"the story I want to record" reads as an answer, not a request, and an
    unordered rule cannot tell the two apart."""
    assert intent_match(
        tokens("the story I want to record"), STORY_VERBS, STORY_NOUNS, 4,
    ) is False


def test_the_noun_must_be_within_max_distance():
    assert intent_match(
        tokens("record a really long story"), STORY_VERBS, STORY_NOUNS, 4,
    ) is True
    assert intent_match(
        tokens("record what the teacher told us about the story"),
        STORY_VERBS, STORY_NOUNS, 4,
    ) is False


def test_a_noun_alone_does_not_match():
    assert intent_match(
        tokens("this is quite a story"), STORY_VERBS, STORY_NOUNS, 4,
    ) is False


def test_a_verb_alone_does_not_match():
    assert intent_match(
        tokens("we started recording attendance"), STORY_VERBS, STORY_NOUNS, 4,
    ) is False


def test_an_empty_grid_never_matches():
    assert intent_match(tokens("record a story"), [], STORY_NOUNS, 4) is False
    assert intent_match(tokens("record a story"), STORY_VERBS, [], 4) is False


# ---------------------------------------------------------------------------
# mentions_any / subject_tokens -- the ambiguity gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("answer", [
    "yes", "40 students", "Kolar district",
    "attendance dropped after the monsoon", "no, not really",
])
def test_the_gate_is_closed_for_ordinary_answers(answer):
    """These are the bulk of every conversation, and each used to cost a full
    OpenRouter round trip to establish it was not a request to leave."""
    assert mentions_any(tokens(answer), STORY_NOUNS + DISCUSSION_NOUNS) is False


def test_the_gate_is_open_for_an_ambiguous_mention():
    """Mentions the subject without asking for the agent -- exactly the case the
    classifier is retained for."""
    assert mentions_any(
        tokens("that reminds me of a story from last year"), STORY_NOUNS,
    ) is True


def test_subject_tokens_strips_filler():
    """Leaving "a" in would open the gate on virtually every English sentence,
    restoring the per-turn LLM call."""
    subjects = subject_tokens(["capture a discussion", "meeting notes"])
    assert subjects == {"capture", "discussion", "meeting", "notes"}
    assert "a" not in subjects


def test_subject_tokens_keeps_an_agent_without_a_grid_reachable():
    """An agent with no `routing.intent` still has to open the gate for a
    message about its subject, or it becomes unreachable by classifier."""
    assert mentions_any(
        tokens("tell me about the chaupal"), subject_tokens(DISCUSSION_KEYWORDS),
    ) is True
