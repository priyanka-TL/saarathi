"""Contract tests for SaathiRestClient's completion poll.

WHY THIS FILE EXISTS. `SaathiRestClient.is_session_completed` was unpinned at
the REST level -- the only coverage of the poll lived in the Mitra tests, so
Saathi's copy of the same algorithm could diverge silently. It is the SAME
algorithm on purpose (both platforms are one Django app behind two routes) but
deliberately NOT shared code: `.importlinter` forbids either platform package
from importing the other, so the two copies must be held to the same behaviour
by tests rather than by an import.

`saathi` is also the slowest remote agent on record, and this poll was ~350 ms
of every one of its turns.

THE ONE DIFFERENCE FROM MITRA'S: every request carries the per-user credential
header. That is asserted here, on both calls of the two-call path, because a
credential dropped on the second call would fail only for a user whose cache is
stale -- the hardest kind of bug to find in production.

No live network calls: `responses` intercepts at the transport level.
"""
from __future__ import annotations

import responses as resp_lib

from app.providers.saathi.rest import AUTH_HEADER, SaathiRestClient
from app.providers.saathi.spec import SaathiPaths
from app.providers.transport.http import RestTransport

BASE_URL = "https://saathi.example.com"
CHAT_URL = f"{BASE_URL}/api/companychat/"
TOKEN = "user-token-abc"


def _client() -> SaathiRestClient:
    return SaathiRestClient(
        RestTransport(
            provider="Saathi",
            base_url=BASE_URL,
            origin_url=BASE_URL,
            user_agent="Mozilla/5.0 TestAgent",
            allowed_hosts=[],
            connect_timeout=5.0,
            read_timeout=10.0,
        ),
        SaathiPaths(),
    )


def _assert_authed(call) -> None:
    assert call.request.headers.get(AUTH_HEADER) == TOKEN, (
        f"{AUTH_HEADER} missing -- Saathi's REST surface is per-user, unlike "
        "Mitra's, and an unauthenticated poll cannot see the session at all"
    )


# ---------------------------------------------------------------------------
# The cold path: no cached count, two calls, unchanged from before
# ---------------------------------------------------------------------------


@resp_lib.activate
def test_a_cold_poll_learns_the_count_then_seeks_to_the_true_tail():
    """`offset=0` with a full page would drag the whole transcript back, which
    measured WORSE than two small calls -- so with no cache this still asks for
    `count` cheaply and seeks, exactly as it always did."""
    resp_lib.add(
        resp_lib.GET, CHAT_URL,
        json={"count": 3, "results": [{"id": 1, "status": "sent"}]}, status=200,
        match=[resp_lib.matchers.query_param_matcher({"session": "s1", "limit": "1"})],
    )
    resp_lib.add(
        resp_lib.GET, CHAT_URL,
        json={"count": 3, "results": [{"id": 3, "status": "COMPLETED"}]}, status=200,
        match=[resp_lib.matchers.query_param_matcher(
            {"session": "s1", "limit": "100", "offset": "2"})],
    )

    result = _client().is_session_completed("s1", TOKEN)

    assert result.done is True
    assert result.count == 3
    for call in resp_lib.calls:
        _assert_authed(call)


@resp_lib.activate
def test_the_true_last_row_decides_not_the_last_row_of_page_one():
    """THE LATENT BUG THIS SHARES WITH MITRA'S. A bare GET returns the FIRST
    page, so `results[-1]` was an old row once a conversation passed the page
    size -- and the session could then never be seen to complete."""
    resp_lib.add(
        resp_lib.GET, CHAT_URL,
        json={"count": 250, "results": [{"id": 1, "status": "COMPLETED"}]}, status=200,
        match=[resp_lib.matchers.query_param_matcher({"session": "s1", "limit": "1"})],
    )
    resp_lib.add(
        resp_lib.GET, CHAT_URL,
        json={"count": 250, "results": [{"id": 250, "status": "in_progress"}]},
        status=200,
        match=[resp_lib.matchers.query_param_matcher(
            {"session": "s1", "limit": "100", "offset": "249"})],
    )

    assert _client().is_session_completed("s1", TOKEN).done is False


@resp_lib.activate
def test_an_empty_transcript_short_circuits_without_a_negative_offset():
    resp_lib.add(
        resp_lib.GET, CHAT_URL, json={"count": 0, "results": []}, status=200,
    )

    result = _client().is_session_completed("s1", TOKEN)

    assert result.done is False
    assert result.count == 0
    assert len(resp_lib.calls) == 1


# ---------------------------------------------------------------------------
# The cached count: two round trips per turn become one
# ---------------------------------------------------------------------------


@resp_lib.activate
def test_a_warm_cache_reaches_the_tail_in_ONE_request():
    """THE SAVING. The first call existed only to learn `count`; last turn's
    count replaces it. The seek still lands on the true tail because a FULL PAGE
    sweeps up whatever the platform appended while we were away."""
    resp_lib.add(
        resp_lib.GET, CHAT_URL,
        json={"count": 20, "results": [
            {"id": 18, "status": "sent"},
            {"id": 19, "status": "sent"},
            {"id": 20, "status": "COMPLETED"},
        ]},
        status=200,
        match=[resp_lib.matchers.query_param_matcher(
            {"session": "s1", "limit": "100", "offset": "17"})],
    )

    result = _client().is_session_completed("s1", TOKEN, known_count=18)

    assert result.done is True
    assert result.count == 20
    assert len(resp_lib.calls) == 1
    _assert_authed(resp_lib.calls[0])


@resp_lib.activate
def test_a_stale_cache_re_seeks_rather_than_answering_wrongly():
    """SELF-VERIFYING. `offset + len(results) != count` proves the tail was NOT
    reached, so this re-seeks with the fresh count. A stale cache costs a round
    trip; it must never cost a wrong answer -- a wrong `False` leaves the session
    open forever."""
    resp_lib.add(
        resp_lib.GET, CHAT_URL,
        json={"count": 300, "results": [
            {"id": i, "status": "sent"} for i in range(5, 105)
        ]},
        status=200,
        match=[resp_lib.matchers.query_param_matcher(
            {"session": "s1", "limit": "100", "offset": "4"})],
    )
    resp_lib.add(
        resp_lib.GET, CHAT_URL,
        json={"count": 300, "results": [{"id": 300, "status": "COMPLETED"}]},
        status=200,
        match=[resp_lib.matchers.query_param_matcher(
            {"session": "s1", "limit": "100", "offset": "299"})],
    )

    result = _client().is_session_completed("s1", TOKEN, known_count=5)

    assert result.done is True
    assert result.count == 300
    assert len(resp_lib.calls) == 2
    # BOTH calls, not just the first: a credential dropped on the re-seek would
    # fail only for users whose cache happens to be far behind.
    for call in resp_lib.calls:
        _assert_authed(call)


@resp_lib.activate
def test_a_cache_larger_than_the_transcript_still_reads_the_true_tail():
    """The dangerous direction: an over-large count seeks PAST the tail and gets
    nothing back, which must re-seek rather than answer False."""
    resp_lib.add(
        resp_lib.GET, CHAT_URL,
        json={"count": 4, "results": []}, status=200,
        match=[resp_lib.matchers.query_param_matcher(
            {"session": "s1", "limit": "100", "offset": "99"})],
    )
    resp_lib.add(
        resp_lib.GET, CHAT_URL,
        json={"count": 4, "results": [{"id": 4, "status": "COMPLETED"}]}, status=200,
        match=[resp_lib.matchers.query_param_matcher(
            {"session": "s1", "limit": "100", "offset": "3"})],
    )

    assert _client().is_session_completed("s1", TOKEN, known_count=100).done is True


@resp_lib.activate
def test_a_cached_count_of_zero_takes_the_cold_path():
    """Zero means "nothing recorded yet", not "seek to row -1"."""
    resp_lib.add(
        resp_lib.GET, CHAT_URL,
        json={"count": 2, "results": [{"id": 1, "status": "sent"}]}, status=200,
        match=[resp_lib.matchers.query_param_matcher({"session": "s1", "limit": "1"})],
    )
    resp_lib.add(
        resp_lib.GET, CHAT_URL,
        json={"count": 2, "results": [{"id": 2, "status": "COMPLETED"}]}, status=200,
        match=[resp_lib.matchers.query_param_matcher(
            {"session": "s1", "limit": "100", "offset": "1"})],
    )

    assert _client().is_session_completed("s1", TOKEN, known_count=0).done is True
