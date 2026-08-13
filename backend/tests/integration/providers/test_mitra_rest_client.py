"""Contract tests for MitraRestClient against recorded JSON fixtures.

No live network calls are made. The `responses` library intercepts every
HTTP request at the transport level before it leaves the process. This
verifies:

  1. Correct HTTP method and URL for each endpoint
  2. Correct request headers (Origin present on every call)
  3. Correct request body shape (finalize: token in header, NOT body)
  4. Correct parsing of each response shape
  5. SSRF guard: non-https or non-allowlisted URL → ProviderSSRFError
  6. Redirect guard: 3xx → ProviderRedirectError
  7. HTTP error guard: non-2xx → ProviderHTTPError
  8. Empty results branches (is_session_completed, get_report)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import responses as resp_lib  # the `responses` library, not stdlib

from app.providers.mitra.rest import MitraRestClient
from app.providers.mitra.spec import MitraPaths
from app.providers.transport.http import RestTransport
from app.providers.errors import (
    ProviderHTTPError,
    ProviderRedirectError,
    ProviderSSRFError,
    ProviderError,
)

# ---------------------------------------------------------------------------
# Fixture loading helpers
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ---------------------------------------------------------------------------
# Shared client factory
# ---------------------------------------------------------------------------

BASE_URL = "https://mitra.example.com"
ORIGIN_URL = "https://mitra.example.com"
USER_AGENT = "Mozilla/5.0 TestAgent"


def _client(**kwargs) -> MitraRestClient:
    """A real client over a real transport.

    The transport is where the session, the fixed headers, the redirect refusal
    and the SSRF check now live -- shared by every provider rather than
    re-derived per platform -- so the tests below still exercise all of it, just
    through one seam instead of one copy per integration.
    """
    paths = kwargs.pop("paths", None) or MitraPaths()
    defaults = dict(
        provider="Mitra",
        base_url=BASE_URL,
        origin_url=ORIGIN_URL,
        user_agent=USER_AGENT,
        allowed_hosts=[],
        connect_timeout=5.0,
        read_timeout=10.0,
    )
    defaults.update(kwargs)
    return MitraRestClient(RestTransport(**defaults), paths)


# ---------------------------------------------------------------------------
# Header assertion helper
# ---------------------------------------------------------------------------

def _assert_origin_sent(call: resp_lib.calls.Call) -> None:
    """Every request must carry the Origin header."""
    assert "Origin" in call.request.headers, (
        "Origin header missing — Mitra gates admission on it (§13.2)"
    )
    assert call.request.headers["Origin"] == ORIGIN_URL


# ---------------------------------------------------------------------------
# 1. upsert_profile
# ---------------------------------------------------------------------------

@resp_lib.activate
def test_upsert_profile_returns_existing_profile_id():
    """The static-token POC always finds an existing profile.

    Mitra upserts: the same (email, company) pair returns the existing row's
    id. profile_upsert.json models this — there is no separate 'create' case
    in the POC because we always use the same hardcoded user. §1.7 trap 1.
    """
    fixture = _load("profile_upsert.json")
    resp_lib.add(
        resp_lib.POST,
        f"{BASE_URL}/api/profile/",
        json=fixture,
        status=200,
    )

    client = _client()
    profile_id = client.upsert_profile(
        email="1355@shikshalokam.org",
        latest_flow_used="guest-mi-story",
        company="shikshalokamstaging",
    )

    assert profile_id == str(fixture["id"])
    assert len(resp_lib.calls) == 1
    _assert_origin_sent(resp_lib.calls[0])


@resp_lib.activate
def test_upsert_profile_uses_profileid_fallback():
    """If 'id' is absent, fall back to 'profileid' (legacy response key)."""
    resp_lib.add(
        resp_lib.POST,
        f"{BASE_URL}/api/profile/",
        json={"profileid": 9999, "email": "test@example.com"},
        status=200,
    )

    client = _client()
    result = client.upsert_profile("test@example.com", "guest-mi-story", "testco")
    assert result == "9999"


@resp_lib.activate
def test_upsert_profile_sends_correct_payload():
    """Verify the exact JSON body sent to /api/profile/."""
    resp_lib.add(
        resp_lib.POST,
        f"{BASE_URL}/api/profile/",
        json=_load("profile_upsert.json"),
        status=200,
    )

    _client().upsert_profile(
        email="1355@shikshalokam.org",
        latest_flow_used="guest-mi-story",
        company="shikshalokamstaging",
    )

    sent_body = json.loads(resp_lib.calls[0].request.body)
    assert sent_body["email"] == "1355@shikshalokam.org"
    assert sent_body["latest_flow_used"] == "guest-mi-story"
    assert sent_body["company"] == "shikshalokamstaging"
    # THE REGRESSION PIN FOR THE STORY FLOW. `extra` defaults to None, and a
    # caller that passes nothing must produce the body this client has always
    # sent -- exactly these three keys. Mitra answers 200 to a body with extra
    # keys, so nothing downstream would catch a leak here.
    assert set(sent_body) == {"email", "latest_flow_used", "company"}


@resp_lib.activate
def test_upsert_profile_sends_the_extra_fields_without_letting_them_win():
    """`extra` carries who the caller is; the three keys above carry WHICH
    profile this is. Mitra resolves the row by (email, company), so a collision
    would move the write to a different profile -- and the response shape is
    identical either way, so it would never surface.

    Blank values are dropped rather than sent as "": Mitra runs a NON-partial
    serializer over an existing profile, so an omitted key keeps its stored
    value while an empty string overwrites a good one with a blank.
    """
    resp_lib.add(
        resp_lib.POST,
        f"{BASE_URL}/api/profile/",
        json=_load("profile_upsert.json"),
        status=200,
    )

    _client().upsert_profile(
        email="1355@shikshalokam.org",
        latest_flow_used="guest-discussion",
        company="shikshalokamstaging",
        extra={
            "first_name": "Asha Devi",
            "designation": "Head Teacher",
            "org_associated": "GHS Ramnagar",
            "location": "Bengaluru Rural, Karnataka",
            # Both dropped, for the two different reasons above.
            "email": "attacker@example.org",
            "caste": "   ",
        },
    )

    sent_body = json.loads(resp_lib.calls[0].request.body)
    assert sent_body == {
        "email": "1355@shikshalokam.org",
        "latest_flow_used": "guest-discussion",
        "company": "shikshalokamstaging",
        "first_name": "Asha Devi",
        "designation": "Head Teacher",
        "org_associated": "GHS Ramnagar",
        "location": "Bengaluru Rural, Karnataka",
    }


# ---------------------------------------------------------------------------
# 2. generate_session
# ---------------------------------------------------------------------------

@resp_lib.activate
def test_generate_session_returns_session_id():
    fixture = _load("generate_session.json")
    resp_lib.add(
        resp_lib.GET,
        f"{BASE_URL}/api/generate-session/",
        json=fixture,
        status=200,
    )

    session_id = _client().generate_session()

    assert session_id == fixture["sessionid"]
    assert len(resp_lib.calls) == 1
    _assert_origin_sent(resp_lib.calls[0])


# ---------------------------------------------------------------------------
# 3. is_session_completed
# ---------------------------------------------------------------------------

@resp_lib.activate
def test_is_session_completed_false_when_in_progress():
    """Per §1.3 / §1.8: finish_reason on WS is NOT the completion signal.
    Only results[-1].status == 'COMPLETED' counts.
    """
    resp_lib.add(
        resp_lib.GET,
        f"{BASE_URL}/api/companychat/",
        json=_load("companychat_in_progress.json"),
        status=200,
    )

    result = _client().is_session_completed("mitra-session-abc123")

    assert result is False
    # Verify correct query param was sent
    assert "session=mitra-session-abc123" in resp_lib.calls[0].request.url
    _assert_origin_sent(resp_lib.calls[0])


@resp_lib.activate
def test_is_session_completed_true_when_completed():
    resp_lib.add(
        resp_lib.GET,
        f"{BASE_URL}/api/companychat/",
        json=_load("companychat_completed.json"),
        status=200,
    )

    result = _client().is_session_completed("mitra-session-abc123")

    assert result is True


@resp_lib.activate
def test_is_session_completed_false_on_empty_results():
    """When results is empty, return False (not yet polled, not an error)."""
    resp_lib.add(
        resp_lib.GET,
        f"{BASE_URL}/api/companychat/",
        json={"count": 0, "results": []},
        status=200,
    )

    result = _client().is_session_completed("mitra-session-abc123")

    assert result is False


# ---------------------------------------------------------------------------
# 4. finalize — the critical v2 test
# ---------------------------------------------------------------------------

@resp_lib.activate
def test_finalize_sends_token_in_authorization_header_not_body():
    """§1.5 / §1.8 step 8: v2 endpoint reads token from Authorization header.

    v1 read it from the request body (still used by Node bot).
    This test is the primary acceptance criterion for the finalize method.
    """
    fixture = _load("end_story_v2.json")
    resp_lib.add(
        resp_lib.POST,
        f"{BASE_URL}/api/end-story/v2/",
        json=fixture,
        status=200,
    )

    story_id, content = _client().finalize(
        session_id="mitra-session-abc123",
        profile_id="1355",
        flow="guest-mi-story",
        language="en",
        token="test-bearer-token",
    )

    assert story_id == fixture["id"]
    assert content == fixture["content"]

    call = resp_lib.calls[0]
    _assert_origin_sent(call)

    # Token MUST be in the Authorization header
    assert call.request.headers.get("Authorization") == "Bearer test-bearer-token", (
        "finalize must send token in Authorization header (v2 endpoint, not v1 body)"
    )

    # Token must NOT be in the request body
    body = json.loads(call.request.body)
    assert "access_token" not in body, "token must not appear in request body (that is the v1 shape)"
    assert "token" not in body, "token must not appear in request body"

    # Correct body fields for v2
    assert body["session"] == "mitra-session-abc123"
    assert body["profile_id"] == "1355"
    assert body["stage"] == "COMPLETED"
    assert body["flow"] == "guest-mi-story"
    assert body["language"] == "en"


@resp_lib.activate
def test_finalize_posts_to_the_configured_path_not_a_hardcoded_v2():
    """spec.remote.finalize_path selects the endpoint.

    v1 and v2 resolve the story bot from different Mitra tables, so an agent
    whose flow has no Flow row must be able to target v1 by config. Before
    this, finalize() hardcoded v2 and the YAML field was dead config -- which
    is how record_stories ended up posting 'guest-mi-story' to the one
    endpoint that cannot resolve it (deterministic HTTP 500).
    """
    fixture = _load("end_story_v2.json")
    resp_lib.add(resp_lib.POST, f"{BASE_URL}/api/end-story/", json=fixture, status=200)

    story_id, _ = _client().finalize(
        session_id="mitra-session-abc123",
        profile_id="1355",
        flow="guest-mi-story",
        language="en",
        token="test-bearer-token",
        path="/api/end-story/",
    )

    assert story_id == fixture["id"]
    assert resp_lib.calls[0].request.url == f"{BASE_URL}/api/end-story/"


@resp_lib.activate
def test_finalize_v1_puts_token_in_body_and_not_in_the_header():
    """The v1 view reads request.data['access_token']; it never looks at the
    Authorization header. Selecting the path without moving the token would
    send an unauthenticated finalize that still returns 200 -- silently
    dropping the Elevate push -- so path and token placement move together.
    """
    resp_lib.add(
        resp_lib.POST, f"{BASE_URL}/api/end-story/",
        json=_load("end_story_v2.json"), status=200,
    )

    _client().finalize("sess", "prof", "guest-mi-story", "en", "tok", path="/api/end-story/")

    call = resp_lib.calls[0]
    _assert_origin_sent(call)
    assert json.loads(call.request.body)["access_token"] == "tok"
    assert "Authorization" not in call.request.headers


@resp_lib.activate
def test_finalize_defaults_to_v2_when_no_path_is_given():
    """Omitting path must keep the pre-existing v2 behaviour, header token
    included -- agents that don't set finalize_path are unaffected."""
    resp_lib.add(
        resp_lib.POST, f"{BASE_URL}/api/end-story/v2/",
        json=_load("end_story_v2.json"), status=200,
    )

    _client().finalize("sess", "prof", "guest-discussion", "en", "tok")

    call = resp_lib.calls[0]
    assert call.request.headers.get("Authorization") == "Bearer tok"
    assert "access_token" not in json.loads(call.request.body)


@resp_lib.activate
def test_finalize_as_guest_sends_a_null_access_token_and_no_header_on_v1():
    """THE regression pin for the blank Capture Discussion PDF.

    Mitra sets `auth = access_token is not None` and picks the PDF template's
    user_type (AUTH vs GUEST) from it. A guest flow finalised WITH a token
    therefore looks up a template nobody registered, get_html_from_template
    returns "", and save_project_story hands that to Gotenberg -- which renders
    an empty page and returns 200. Blank PDF, no error, every check green.

    This body is byte-identical to Mitra's own client for the flow
    (storyPostSessionService._callEndStory): access_token null, Origin only.
    """
    resp_lib.add(
        resp_lib.POST, f"{BASE_URL}/api/end-story/",
        json=_load("end_story_v2.json"), status=200,
    )

    _client().finalize(
        "sess", "prof", "guest-discussion", "en", "tok",
        path="/api/end-story/", as_guest=True,
    )

    call = resp_lib.calls[0]
    _assert_origin_sent(call)
    body = json.loads(call.request.body)
    assert body["access_token"] is None, (
        "an explicit null, not an omitted key -- this mirrors the Node client"
    )
    assert "Authorization" not in call.request.headers
    # Everything else is unchanged by as_guest.
    assert body["session"] == "sess"
    assert body["profile_id"] == "prof"
    assert body["stage"] == "COMPLETED"
    assert body["flow"] == "guest-discussion"
    assert body["language"] == "en"


@resp_lib.activate
def test_finalize_as_guest_sends_no_bearer_on_v2_either():
    """as_guest is about token presence, not about which endpoint. v2 reads the
    token from the header, so suppressing it there means sending no header at
    all -- otherwise flipping only finalize_path would silently re-authenticate
    a guest flow."""
    resp_lib.add(
        resp_lib.POST, f"{BASE_URL}/api/end-story/v2/",
        json=_load("end_story_v2.json"), status=200,
    )

    _client().finalize("sess", "prof", "guest-discussion", "en", "tok", as_guest=True)

    call = resp_lib.calls[0]
    assert "Authorization" not in call.request.headers
    assert "access_token" not in json.loads(call.request.body)


@resp_lib.activate
def test_finalize_raises_on_missing_id():
    """If Mitra omits 'id' from the response, raise ProviderError."""
    resp_lib.add(
        resp_lib.POST,
        f"{BASE_URL}/api/end-story/v2/",
        json={"status": "ok", "message": "Story created"},  # missing id
        status=200,
    )

    with pytest.raises(ProviderError, match="missing 'id'"):
        _client().finalize("sess", "prof", "guest-mi-story", "en", "tok")


# ---------------------------------------------------------------------------
# 5. get_report
# ---------------------------------------------------------------------------

@resp_lib.activate
def test_get_report_returns_pdf_url():
    fixture = _load("get_story_with_pdf.json")
    resp_lib.add(
        resp_lib.GET,
        f"{BASE_URL}/api/get-story/",
        json=fixture,
        status=200,
    )

    url = _client().get_report("mitra-session-abc123", media_type="application/pdf")

    assert url == "https://mitra.example.com/media/stories/story-uuid-789xyz.pdf"
    assert "session=mitra-session-abc123" in resp_lib.calls[0].request.url
    _assert_origin_sent(resp_lib.calls[0])


@resp_lib.activate
def test_get_report_returns_none_when_no_pdf_yet():
    """Empty story_media → None (report generation lags; caller should poll)."""
    resp_lib.add(
        resp_lib.GET,
        f"{BASE_URL}/api/get-story/",
        json=_load("get_story_no_pdf.json"),
        status=200,
    )

    url = _client().get_report("mitra-session-abc123", "application/pdf")

    assert url is None


@resp_lib.activate
def test_get_report_returns_none_on_empty_results():
    resp_lib.add(
        resp_lib.GET,
        f"{BASE_URL}/api/get-story/",
        json={"count": 0, "results": []},
        status=200,
    )

    assert _client().get_report("mitra-session-abc123", "application/pdf") is None


# ---------------------------------------------------------------------------
# 6. SSRF guard
# ---------------------------------------------------------------------------

@resp_lib.activate
def test_get_report_raises_ssrf_error_for_http_url():
    """A returned URL with scheme=http must be rejected (SSRF / downgrade)."""
    resp_lib.add(
        resp_lib.GET,
        f"{BASE_URL}/api/get-story/",
        json={
            "count": 1,
            "results": [{
                "story_media": [{
                    "media_type": "application/pdf",
                    "public_url": "http://mitra.example.com/evil.pdf",  # http, not https
                }]
            }]
        },
        status=200,
    )

    with pytest.raises(ProviderSSRFError):
        _client().get_report("mitra-session-abc123", "application/pdf")


@resp_lib.activate
def test_get_report_raises_ssrf_error_for_non_allowlisted_host():
    """A returned URL pointing at an unknown host must be rejected."""
    resp_lib.add(
        resp_lib.GET,
        f"{BASE_URL}/api/get-story/",
        json={
            "count": 1,
            "results": [{
                "story_media": [{
                    "media_type": "application/pdf",
                    "public_url": "https://evil-bucket.s3.amazonaws.com/exfil.pdf",
                }]
            }]
        },
        status=200,
    )

    with pytest.raises(ProviderSSRFError):
        _client().get_report("mitra-session-abc123", "application/pdf")


@resp_lib.activate
def test_get_report_allows_extra_allowed_host():
    """A CDN host explicitly in allowed_hosts is permitted."""
    cdn_host = "cdn.shikshalokam.org"
    resp_lib.add(
        resp_lib.GET,
        f"{BASE_URL}/api/get-story/",
        json={
            "count": 1,
            "results": [{
                "story_media": [{
                    "media_type": "application/pdf",
                    "public_url": f"https://{cdn_host}/stories/xyz.pdf",
                }]
            }]
        },
        status=200,
    )

    client = _client(allowed_hosts=[cdn_host])
    url = client.get_report("mitra-session-abc123", "application/pdf")

    assert url == f"https://{cdn_host}/stories/xyz.pdf"


# ---------------------------------------------------------------------------
# 7. Redirect guard
# ---------------------------------------------------------------------------

@resp_lib.activate
def test_redirect_raises_mitra_redirect_error():
    """Any 3xx is unexpected and must raise ProviderRedirectError.

    A redirect from Mitra's API indicates MITRA_BASE_URL is misconfigured
    (e.g. pointing at an old non-redirected URL). Raise loudly rather than
    silently following to an unknown destination.
    """
    resp_lib.add(
        resp_lib.GET,
        f"{BASE_URL}/api/generate-session/",
        status=301,
        headers={"Location": "https://mitra.example.com/api/generate-session/v2/"},
    )

    with pytest.raises(ProviderRedirectError) as exc_info:
        _client().generate_session()

    assert exc_info.value.status == 301


# ---------------------------------------------------------------------------
# 8. HTTP error guard
# ---------------------------------------------------------------------------

@resp_lib.activate
def test_http_error_raises_mitra_http_error():
    """Non-2xx response (e.g. 401, 500) raises ProviderHTTPError."""
    resp_lib.add(
        resp_lib.POST,
        f"{BASE_URL}/api/profile/",
        json={"detail": "Unauthorized"},
        status=401,
    )

    with pytest.raises(ProviderHTTPError) as exc_info:
        _client().upsert_profile("a@b.com", "guest-mi-story", "co")

    assert exc_info.value.status == 401
    # Error message must NOT contain the Origin value
    assert ORIGIN_URL not in str(exc_info.value)


@resp_lib.activate
def test_http_error_carries_mitras_own_error_message():
    """"returned HTTP 500" alone is unactionable -- it reads identically for a
    Mitra outage and for a flow Mitra cannot resolve, which is what sent the
    end-story investigation to the wrong layer repeatedly. Mitra's error
    envelope names the real cause; surface it.
    """
    resp_lib.add(
        resp_lib.POST,
        f"{BASE_URL}/api/end-story/v2/",
        json={
            "status": "error",
            "message": "",
            "error_message": "Flow not found with route: guest-mi-story",
            "error_type": "generic_error",
        },
        status=500,
    )

    with pytest.raises(ProviderHTTPError) as exc_info:
        _client().finalize("sess", "prof", "guest-mi-story", "en", "tok")

    assert "Flow not found with route: guest-mi-story" in str(exc_info.value)
    assert exc_info.value.detail is not None


@resp_lib.activate
def test_http_error_detail_is_dropped_when_it_reflects_the_origin_credential():
    """§13.2: the edge can echo the Origin header back inside an error body.
    Extraction is per-key and never the raw body, but the keys themselves are
    dropped too if the credential turns up in them."""
    resp_lib.add(
        resp_lib.GET,
        f"{BASE_URL}/api/generate-session/",
        json={"detail": f"Origin {ORIGIN_URL} is not allowed"},
        status=403,
    )

    with pytest.raises(ProviderHTTPError) as exc_info:
        _client().generate_session()

    assert ORIGIN_URL not in str(exc_info.value)
    assert exc_info.value.detail is None


@resp_lib.activate
def test_http_error_detail_is_none_for_a_non_json_body():
    """HTML error pages from a proxy must not end up in the exception."""
    resp_lib.add(
        resp_lib.GET,
        f"{BASE_URL}/api/generate-session/",
        body="<html><body>502 Bad Gateway</body></html>",
        status=502,
    )

    with pytest.raises(ProviderHTTPError) as exc_info:
        _client().generate_session()

    assert exc_info.value.detail is None
    assert "<html>" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# 9. repr must not leak Origin
# ---------------------------------------------------------------------------

def test_repr_does_not_expose_origin():
    """__repr__ must not leak the Origin credential into logs."""
    client = _client(origin_url="https://secret-origin.example.com")
    r = repr(client)
    assert "secret-origin.example.com" not in r, (
        "Origin credential must not appear in repr (it would leak into logs)"
    )
    assert "MitraRestClient" in r


# ---------------------------------------------------------------------------
# 10. recent_chat — the tail of CompanyChat, for lost-turn recovery
# ---------------------------------------------------------------------------

def _chat_row(row_id, sender_id, message, stage="IN_PROGRESS"):
    return {
        "id": row_id,
        "sender": {"id": sender_id},
        "receiver": {"id": 1 if sender_id != 1 else 380},
        "message": message,
        "translated_message": "",
        "stage": stage,
    }


@resp_lib.activate
def test_recent_chat_marks_direction_from_the_callers_profile_id():
    """A row is the USER's when sender.id is the caller's profile. Keying off a
    hardcoded AI profile id instead would silently invert on an environment
    where that row differs."""
    resp_lib.add(
        resp_lib.GET, f"{BASE_URL}/api/companychat/",
        json={"count": 2, "results": [_chat_row(1, 380, "hi")]},
    )
    resp_lib.add(
        resp_lib.GET, f"{BASE_URL}/api/companychat/",
        json={"count": 2, "results": [
            _chat_row(1, 380, "my answer"),
            _chat_row(2, 1, "the bot reply"),
        ]},
    )

    rows = _client().recent_chat("sess-1", profile_id="380")

    assert [r.from_user for r in rows] == [True, False]
    assert [r.message for r in rows] == ["my answer", "the bot reply"]


@resp_lib.activate
def test_recent_chat_offsets_to_the_true_tail_of_a_long_interview():
    """Mitra paginates at PAGE_SIZE=100 with LimitOffsetPagination, so a bare
    GET returns the OLDEST 100 rows. Reading results[-1] off that page stops
    being 'the latest message' the moment an interview passes 100 rows -- a
    60-turn story is 120+ rows, well past it.
    """
    resp_lib.add(
        resp_lib.GET, f"{BASE_URL}/api/companychat/",
        json={"count": 137, "results": [_chat_row(1, 380, "oldest")]},
    )
    resp_lib.add(
        resp_lib.GET, f"{BASE_URL}/api/companychat/",
        json={"count": 137, "results": [_chat_row(136, 380, "newest user"),
                                        _chat_row(137, 1, "newest bot")]},
    )

    rows = _client().recent_chat("sess-1", profile_id="380", tail=10)

    tail_request = resp_lib.calls[1].request
    assert "offset=127" in tail_request.url, (
        f"expected an offset to the tail (137-10), got {tail_request.url}"
    )
    assert "limit=10" in tail_request.url
    assert [r.id for r in rows] == [136, 137]


@resp_lib.activate
def test_recent_chat_returns_rows_oldest_first():
    """reconcile() scans a chronological list; an out-of-order page would make
    it pair the wrong reply with the wrong message."""
    resp_lib.add(
        resp_lib.GET, f"{BASE_URL}/api/companychat/",
        json={"count": 3, "results": [_chat_row(1, 380, "a")]},
    )
    resp_lib.add(
        resp_lib.GET, f"{BASE_URL}/api/companychat/",
        json={"count": 3, "results": [_chat_row(3, 1, "c"),
                                      _chat_row(1, 380, "a"),
                                      _chat_row(2, 1, "b")]},
    )

    assert [r.id for r in _client().recent_chat("s", "380")] == [1, 2, 3]


@resp_lib.activate
def test_recent_chat_on_an_empty_session_makes_no_second_call():
    resp_lib.add(
        resp_lib.GET, f"{BASE_URL}/api/companychat/",
        json={"count": 0, "results": []},
    )

    assert _client().recent_chat("sess-1", "380") == []
    assert len(resp_lib.calls) == 1


# ---------------------------------------------------------------------------
# is_session_completed reads the TRUE last row, not the last of page one
# ---------------------------------------------------------------------------


@resp_lib.activate
def test_is_session_completed_reads_past_the_first_page():
    """THE LATENT BUG THIS FIXES. A bare GET returns the first page, so
    `results[-1]` was the last row of page ONE. Past the page size the status
    read was an old row's, and a long interview could never be seen to complete:
    no PDF, session stuck open, nothing logged."""
    # 250 rows exist; page one ends on an IN_PROGRESS row, the real last row is
    # COMPLETED. The old code read page one and answered False forever.
    resp_lib.add(
        resp_lib.GET, f"{BASE_URL}/api/companychat/",
        json={"count": 250, "results": [{"id": 1, "status": "IN_PROGRESS"}]},
        status=200, match=[resp_lib.matchers.query_param_matcher(
            {"session": "s1", "limit": "1"})],
    )
    resp_lib.add(
        resp_lib.GET, f"{BASE_URL}/api/companychat/",
        json={"count": 250, "results": [{"id": 250, "status": "COMPLETED"}]},
        status=200, match=[resp_lib.matchers.query_param_matcher(
            {"session": "s1", "limit": "1", "offset": "249"})],
    )

    assert _client().is_session_completed("s1") is True


@resp_lib.activate
def test_is_session_completed_is_false_when_the_true_last_row_is_not_complete():
    resp_lib.add(
        resp_lib.GET, f"{BASE_URL}/api/companychat/",
        json={"count": 3, "results": [{"id": 1, "status": "COMPLETED"}]},
        status=200, match=[resp_lib.matchers.query_param_matcher(
            {"session": "s1", "limit": "1"})],
    )
    resp_lib.add(
        resp_lib.GET, f"{BASE_URL}/api/companychat/",
        json={"count": 3, "results": [{"id": 3, "status": "IN_PROGRESS"}]},
        status=200, match=[resp_lib.matchers.query_param_matcher(
            {"session": "s1", "limit": "1", "offset": "2"})],
    )

    assert _client().is_session_completed("s1") is False


@resp_lib.activate
def test_is_session_completed_short_circuits_on_an_empty_transcript():
    """count == 0 must not issue a second request with offset -1."""
    resp_lib.add(
        resp_lib.GET, f"{BASE_URL}/api/companychat/",
        json={"count": 0, "results": []}, status=200,
    )

    assert _client().is_session_completed("s1") is False
    assert len(resp_lib.calls) == 1
