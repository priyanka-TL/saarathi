"""What the Mitra provider decides for itself.

THIS IS WHERE TWO ASSERTIONS MOVED TO. `finalize_path` and `finalize_as_guest`
used to be read off the spec by the orchestrator and passed down as arguments,
so they were pinned in the orchestration tests. The orchestrator now asks for
finalisation and is handed a result reference -- it cannot express a preference
about either, which is the coupling that was removed. The properties themselves
did not stop mattering:

  * the wrong endpoint is a deterministic HTTP 500 for a flow with no Flow row;
  * the wrong token-presence is an HTTP 200 and a valid, COMPLETELY BLANK PDF,
    with nothing logged anywhere.

Both are now pinned against the object that makes the decision.
"""
from __future__ import annotations

import pytest

from app.providers.connection import resolve_connection
from app.providers.errors import ProviderError
from app.providers.mitra.provider import MitraProvider
from app.providers.mitra.spec import MitraOptions
from tests.provider_factories import MITRA_ORIGIN_ENV, remote_spec


class _Settings:
    provider_host_ceiling = ""


class _FakeRest:
    """Records exactly what the provider chose to send."""

    def __init__(self):
        self.finalize_calls = []
        self.get_report_calls = []
        self.profile_calls = []
        self.profile_extras = []
        self.story_id = "9931"
        self.report_url = None
        self.report_error = None

    def upsert_profile(self, email, latest_flow_used, company, extra=None):
        self.profile_calls.append((email, latest_flow_used, company))
        self.profile_extras.append(extra)
        return "profile-1"

    def generate_session(self):
        return "remote-session-1"

    def finalize(self, session_id, profile_id, flow, language, token,
                 path=None, as_guest=False):
        self.finalize_calls.append({
            "session_id": session_id, "profile_id": profile_id, "flow": flow,
            "language": language, "token": token, "path": path,
            "as_guest": as_guest,
        })
        return self.story_id, "narrative content"

    def get_report(self, session_id, media_type):
        self.get_report_calls.append((session_id, media_type))
        if self.report_error is not None:
            raise self.report_error
        return self.report_url


class _Session:
    id = "sess-1"
    conversation_id = "conv-1"
    remote_session_id = "remote-session-1"
    remote_profile_id = "profile-1"
    language = "hi"


class _User:
    email = "someone@example.org"
    token = "the-real-token"
    display_name = "Asha Devi"


@pytest.fixture(autouse=True)
def _origin(monkeypatch):
    monkeypatch.setenv(MITRA_ORIGIN_ENV, "https://origin.test")


def _provider(rest, remote, profile_reader=None):
    provider = MitraProvider.__new__(MitraProvider)
    options = MitraOptions(**remote.options)
    provider._conn = resolve_connection(_Settings(), remote, options)
    provider._pool = None
    provider.options = options
    provider._rest = rest
    provider._profile_reader = profile_reader
    return provider


# ---------------------------------------------------------------------------
# Finalisation: the two relocated pins
# ---------------------------------------------------------------------------

def test_finalize_uses_the_endpoint_the_agent_declares():
    """v2 resolves the story bot from a Flow row keyed on the flow route. A flow
    with no such row is a deterministic HTTP 500, so the agent's own
    finalize_path is what keeps it off v2."""
    rest = _FakeRest()
    remote = remote_spec(finalize_path="/api/end-story/")
    _provider(rest, remote).finalize(remote, _Session(), _User())

    assert rest.finalize_calls[0]["path"] == "/api/end-story/"


def test_finalize_forwards_the_agents_token_presence_choice():
    """Token presence selects the PDF template's user_type, so a guest flow
    finalised WITH a token renders a blank PDF and still returns 200."""
    rest = _FakeRest()
    remote = remote_spec(finalize_as_guest=True)
    _provider(rest, remote).finalize(remote, _Session(), _User())

    assert rest.finalize_calls[0]["as_guest"] is True


def test_an_agent_that_sends_its_token_is_the_default():
    rest = _FakeRest()
    remote = remote_spec()
    _provider(rest, remote).finalize(remote, _Session(), _User())

    assert rest.finalize_calls[0]["as_guest"] is False
    assert rest.finalize_calls[0]["token"] == "the-real-token"


def test_finalize_sends_the_sessions_language_not_a_default():
    rest = _FakeRest()
    remote = remote_spec()
    _provider(rest, remote).finalize(remote, _Session(), _User())

    assert rest.finalize_calls[0]["language"] == "hi"


# ---------------------------------------------------------------------------
# The artifact is fetched inside finalisation
# ---------------------------------------------------------------------------

def test_the_artifact_is_fetched_before_finalize_returns():
    """SessionService.apply() refuses a second call on a terminal session, so
    the result reference and the artifact URL have to arrive together."""
    rest = _FakeRest()
    rest.report_url = "https://files.test/story.pdf"
    remote = remote_spec()

    result = _provider(rest, remote).finalize(remote, _Session(), _User())

    assert result.result_ref == "9931"
    assert result.artifact_url == "https://files.test/story.pdf"


def test_a_failed_artifact_fetch_does_not_undo_a_successful_finalize():
    """finalize() is IRREVERSIBLE upstream. Letting an artifact problem
    propagate left the session in 'finalizing' forever, with a story that exists
    remotely and can never be resubmitted. Observed live: report PDFs are served
    from a host outside the allowlist, so every successful story raised."""
    rest = _FakeRest()
    rest.report_error = ProviderError("allowlist rejected the URL")
    remote = remote_spec()

    result = _provider(rest, remote).finalize(remote, _Session(), _User())

    assert result.result_ref == "9931"
    assert result.artifact_url is None


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def test_the_profile_is_upserted_against_the_agents_own_company():
    """A profile is identified by (email, company), so a process-global company
    is the difference between every tenant sharing one profile and each having
    its own."""
    rest = _FakeRest()
    remote = remote_spec(company="tenant-co")

    class _Fresh(_Session):
        remote_session_id = None
        remote_profile_id = None

    _provider(rest, remote).open_session(remote, _Fresh(), _User())

    assert rest.profile_calls == [("someone@example.org", "guest-mi-story", "tenant-co")]


def test_open_session_is_idempotent_after_the_first_turn():
    rest = _FakeRest()
    remote = remote_spec()

    init = _provider(rest, remote).open_session(remote, _Session(), _User())

    assert rest.profile_calls == [], "a second turn must not create a second profile"
    assert init.remote_session_id == "remote-session-1"


def test_the_bot_route_is_re_read_every_turn():
    """Straight off this tenant's scoped config, so a configuration change takes
    effect on the next turn rather than at the next restart."""
    rest = _FakeRest()
    remote = remote_spec(bot_route="/changed-route")

    init = _provider(rest, remote).open_session(remote, _Session(), _User())

    assert init.remote_bot_route == "/changed-route"


# ---------------------------------------------------------------------------
# Configuration validation
# ---------------------------------------------------------------------------

def test_a_finalize_path_matching_neither_endpoint_is_rejected():
    """Anything unrecognised falls through to the v1 branch and finalises with
    the wrong body shape, which the platform ACCEPTS -- returning a story, a
    200 from get-story, and a downloadable, completely blank PDF."""
    remote = remote_spec(finalize_path="/api/end-storyy/")
    problem = _provider(_FakeRest(), remote).validate_config(remote, _Settings())

    assert problem is not None
    path, message = problem
    assert path == ["remote", "options", "finalize_path"]
    assert "end-storyy" in message


def test_a_scope_may_name_its_own_endpoints():
    """Validated against the endpoints THIS config resolves to. Checking against
    the shipped pair would both reject correct configs and accept wrong ones."""
    remote = remote_spec(
        finalize_path="/api/end-story/v3/",
        paths={"finalize_v2": "/api/end-story/v3/"},
    )
    assert _provider(_FakeRest(), remote).validate_config(remote, _Settings()) is None


def test_naming_an_endpoint_on_a_no_artifact_flow_is_rejected():
    remote = remote_spec(produces_artifact=False, finalize_path="/api/end-story/")
    problem = _provider(_FakeRest(), remote).validate_config(remote, _Settings())

    assert problem is not None
    assert "must be null" in problem[1]


def test_an_artifact_flow_must_name_an_endpoint():
    remote = remote_spec(finalize_path=None)
    problem = _provider(_FakeRest(), remote).validate_config(remote, _Settings())

    assert problem is not None
    assert "required" in problem[1]
