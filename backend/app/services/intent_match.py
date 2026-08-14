"""Deterministic intent matching for the router.

Responsible for: deciding, without a model, whether a message asks for a
particular agent.
Used by: RouterService -- Gate 3's keyword pre-route and Gate 2's yield check.

WHY THIS EXISTS. `RouterService._kw_match` was `kw.lower() in text.lower()`, a
raw substring test over fixed phrases. A single inserted article defeated it --
"capture story" is not in "capture a story" -- so an ordinary way of asking for
an agent fell through to the LLM classifier. On `saathi`, which sets
`routing.yields_to_keyword`, that classifier ran on EVERY pinned turn, including
ordinary interview answers ("yes", "40 students") that could never have been a
request to leave. One LLM round trip, serialised in front of the agent's own
turn, to answer "no".

THE PRECISION RULE IS THE WHOLE DESIGN. A wrong match is not a cosmetic error:
on the yield path it abandons the conversation the user was in the middle of,
which is irreversible from their side, while a missed match is trivially
recovered by rephrasing. So both matchers below are deliberately biased all the
way to precision, exactly as `RouterService._is_exit` is and for the same
reason. `_FILLER` being closed-class is what enforces that -- see `phrase_match`.

STDLIB ONLY, and no import from anywhere else in `app`: this is pure text
predicate logic, it is called from a hot path, and keeping it dependency-free is
what lets it be unit-tested exhaustively without a database, a registry or a
model.
"""
from __future__ import annotations

import unicodedata
from typing import Iterable, List, Optional, Sequence

#: Words permitted to sit INSIDE a keyword phrase without breaking the match.
#:
#: STRICTLY CLOSED-CLASS, and that is the safety property this whole module
#: rests on. Every entry is a determiner, possessive or quantifier -- a word
#: that can be inserted into a phrase without changing what the phrase asks
#: for. Adding a content word here is what would let
#:
#:     "capture what the head teacher said about the story"
#:
#: match the keyword "capture story", yielding a user out of a conversation they
#: were still answering. If a phrasing is not matching and the fix looks like
#: "just add that word to _FILLER", the correct fix is almost always an entry in
#: the agent's `routing.intent` grid instead.
_FILLER = frozenset({
    "a", "an", "the",
    "my", "our", "your", "their", "its",
    "this", "that", "these", "those",
    "new", "one", "some", "another", "any",
})

#: How many FILLER tokens may sit between two consecutive keyword tokens.
#:
#: Two covers every natural insertion observed ("capture a story", "capture our
#: new story"); three would start admitting phrases whose middle carries meaning
#: even when each word happens to be closed-class.
_MAX_GAP = 2

#: Unicode categories whose characters are part of a word.
#:
#: `M` (combining marks) IS THE ONE THAT MATTERS AND IS EASY TO MISS. A regex
#: `\w` does not include it, so Devanagari splits at every matra -- "मैं"
#: tokenises as three tokens, not one, and Hindi, Kannada and Telugu messages
#: match nothing at all. These agents declare `supported_languages:
#: [en, hi, kn, te]`, and a matcher that silently returns garbage tokens for
#: three of the four would fall open into the LLM on every non-English turn,
#: which is precisely the cost this module exists to remove.
#:
#: Categorising per character rather than reaching for a regex is deliberate:
#: it is correct for every script without anyone having to maintain a list of
#: code-point ranges, and a chat message is short enough that the cost is
#: irrelevant.
_WORD_CATEGORIES = ("L", "N", "M")

#: Kept INSIDE a word so "let's" is one token. Split into {let, s}, the stray
#: "s" would occupy a gap slot in `phrase_match` that it has no right to.
_APOSTROPHES = "'’"


def tokens(text: Optional[str]) -> List[str]:
    """`text` as lowercased word tokens, punctuation dropped.

    The SAME function is applied to messages and to keywords, which is the only
    reason the two can be compared token-for-token at all: a keyword written
    "capture-story" or "Capture Story" has to tokenise the way the message does
    or the comparison is decided by how someone typed the config.
    """
    if not text:
        return []

    out: List[str] = []
    buf: List[str] = []

    def flush() -> None:
        if buf:
            word = "".join(buf).lower().strip(_APOSTROPHES)
            if word:
                out.append(word)
            buf.clear()

    for ch in text:
        # An apostrophe only continues a word already in progress, so a quoted
        # phrase does not glue its opening quote onto the first word.
        if ch in _APOSTROPHES and buf:
            buf.append(ch)
        elif unicodedata.category(ch)[0] in _WORD_CATEGORIES:
            buf.append(ch)
        else:
            flush()
    flush()
    return out


def phrase_match(msg_tokens: Sequence[str], kw_tokens: Sequence[str]) -> bool:
    """Whether `kw_tokens` appear in `msg_tokens` in order, with filler-only gaps.

    The rule, precisely: every keyword token must be present, in the keyword's
    own order, and between two consecutive keyword tokens there may be at most
    `_MAX_GAP` tokens, EVERY one of which is in `_FILLER`.

        "capture a story"           ~ "capture story"   -> True  (gap: {a})
        "capture our new story"     ~ "capture story"   -> True  (gap: {our,new})
        "capture the whole story"   ~ "capture story"   -> False ("whole" is content)
        "story capture"             ~ "capture story"   -> False (wrong order)

    EVERY START POSITION IS TRIED, not just the first. A greedy scan that
    committed to the first occurrence of the leading token would reject

        "record the record of the discussion"  ~  "record discussion"

    because the first "record" is followed by content words, while the second one
    matches. Rejecting that is not merely a missed match -- it is a missed match
    that looks arbitrary, which is worse to debug than a rule that is simply
    strict.

    An empty keyword never matches. That is deliberate rather than degenerate: a
    blank string in a config's `keywords` list would otherwise match every
    message and route the whole application to one agent.
    """
    if not kw_tokens or not msg_tokens:
        return False

    head = kw_tokens[0]
    for start in range(len(msg_tokens)):
        if msg_tokens[start] != head:
            continue
        if _match_from(msg_tokens, kw_tokens, start):
            return True
    return False


def _match_from(msg_tokens: Sequence[str], kw_tokens: Sequence[str], start: int) -> bool:
    """Whether the keyword matches with its first token pinned at `start`."""
    pos = start + 1
    for kw_token in kw_tokens[1:]:
        found = -1
        # `pos + _MAX_GAP` is the last index the next keyword token may occupy:
        # anything further away means more than _MAX_GAP tokens were skipped.
        for candidate in range(pos, min(pos + _MAX_GAP + 1, len(msg_tokens))):
            if msg_tokens[candidate] == kw_token:
                found = candidate
                break
            if msg_tokens[candidate] not in _FILLER:
                # A content word inside the gap ends this attempt outright --
                # skipping it and carrying on is exactly the looseness that
                # would match "capture what the teacher said about the story".
                return False
        if found < 0:
            return False
        pos = found + 1
    return True


def keywords_match(text: Optional[str], keywords: Iterable[str]) -> bool:
    """Whether any of `keywords` matches `text` under the rule above.

    The message is tokenised ONCE and reused across every keyword, which matters:
    this runs for every visible agent on every turn that reaches Gate 3.
    """
    msg_tokens = tokens(text)
    if not msg_tokens:
        return False
    return any(phrase_match(msg_tokens, tokens(kw)) for kw in keywords)


def intent_match(
    msg_tokens: Sequence[str],
    verbs: Iterable[str],
    nouns: Iterable[str],
    max_distance: int,
) -> bool:
    """Whether the message pairs one of `verbs` with one of `nouns`, in that order.

    WHY A GRID AND NOT MORE KEYWORDS. A phrase list cannot enumerate how people
    actually ask. "I want to start a discussion" matches none of the seeded
    discussion keywords, and no realistic amount of adding phrases fixes the
    general case -- there are too many verbs and too many ways to space them
    from the noun. A verb x noun grid covers the cross product with two short
    lists.

    ORDER IS REQUIRED, and it is not a detail. "the story I want to record"
    would match an unordered rule, but so would "record what happened, it is
    quite a story" -- an answer, not a request. Demanding verb-before-noun
    within a short window is what keeps the grid from being looser than the
    phrase matcher it supplements.

    THE GAP IS NOT FILLER-CONSTRAINED HERE, unlike `phrase_match`, because the
    words between a verb and its object are open-class by nature ("record a
    short story", "capture the head teacher's story"). `max_distance` is what
    bounds it instead, which is why it is per-agent config rather than a
    constant -- an agent whose nouns are common words wants it tighter.
    """
    if not msg_tokens:
        return False

    noun_set = {n for n in (t.lower() for t in nouns) if n}
    verb_set = {v for v in (t.lower() for t in verbs) if v}
    if not noun_set or not verb_set:
        return False

    for index, token in enumerate(msg_tokens):
        if token not in verb_set:
            continue
        # The verb's own position is excluded; the noun must come AFTER it.
        window = msg_tokens[index + 1: index + 1 + max_distance]
        if any(candidate in noun_set for candidate in window):
            return True
    return False


def subject_tokens(keywords: Iterable[str]) -> set:
    """The content words of a keyword list -- what those keywords are ABOUT.

    Feeds the ambiguity gate for an agent that declares no `routing.intent`
    grid: its keywords are the only statement of its subject matter available.

    FILLER IS STRIPPED, and that is the entire point of routing this through a
    function rather than doing it at the call site. "capture a discussion"
    contributes {capture, discussion}; leaving "a" in would make the gate open
    on virtually every sentence in English, which would restore the per-turn LLM
    call this module exists to remove.
    """
    return {
        token
        for kw in keywords
        for token in tokens(kw)
        if token not in _FILLER
    }


def mentions_any(msg_tokens: Sequence[str], nouns: Iterable[str]) -> bool:
    """Whether the message contains any of `nouns` at all.

    The AMBIGUITY GATE. A message that names none of the candidate agents'
    subjects cannot be a request for one of them, so there is nothing for a
    classifier to decide and the LLM call is skipped. This is what removes the
    per-turn cost from ordinary interview answers -- "yes", "40 students",
    "attendance dropped after the monsoon" -- which are the bulk of every
    conversation.

    DELIBERATELY LOOSER THAN THE MATCHERS ABOVE. Its job is the opposite of
    theirs: they decide whether to ACT, so they must be precise; this decides
    whether it is worth ASKING, so it must be generous. A false positive here
    costs one LLM call -- exactly what happens today -- while a false negative
    silently declines to consider a yield. When in doubt this must say yes.
    """
    if not msg_tokens:
        return False
    present = set(msg_tokens)
    return any(n.lower() in present for n in nouns if n)
