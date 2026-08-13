"""What the Mitra provider tells Mitra about the caller, and when it stays silent.

A guest interview authenticates with `access_token: None`, so the profile upsert
is the ONLY channel through which Mitra learns who is talking. That makes this
body worth pinning from both directions:

  * an agent that opted in must actually send the fields, or the MOM report goes
    back to naming whoever the transcript happened to mention;
  * an agent that did NOT opt in must send a body byte-for-byte identical to the
    one it sent before this feature existed -- `record_stories` shares this class
    and this REST client with `capture_discussion`, and a story interview
    acquiring a new wire body is exactly the regression this feature must not
    cause.

THE FAILURE MODES HERE ARE ALL SILENT. Mitra answers 200 to a body with an
unexpected key, to a body missing every optional key, and to one that resolved
the wrong profile entirely -- so none of this is caught downstream.
"""
from __future__ import annotations

import pytest

from app.providers.connection import resolve_connection
from app.providers.mitra.provider import MitraProvider
from app.providers.mitra.spec import MitraOptions
from tests.provider_factories import MITRA_ORIGIN_ENV, remote_spec


class _Settings:
    provider_host_ceiling = ""


class _FakeRest:
    """Records the profile body and nothing else."""

    def __init__(self):
        self.calls = []

    def upsert_profile(self, email, latest_flow_used, company, extra=None):
        self.calls.append({
            "email": email, "latest_flow_used": latest_flow_used,
            "company": company, "extra": extra,
        })
        return "profile-1"

    def generate_session(self):
        return "remote-session-1"


class _Fresh:
    """A session that has never reached the remote platform."""

    id = "session-1"
    remote_session_id = None
    remote_profile_id = None
    language = "en"


class _Established:
    """A session already bound to a remote id -- i.e. turn 2 onwards."""

    id = "session-1"
    remote_session_id = "remote-session-1"
    remote_profile_id = "profile-1"
    language = "en"


class _User:
    user_id = "1355"
    email = "1355@shikshalokam.org"
    display_name = "Asha Devi"
    token = "the-callers-own-token"


class _NoToken(_User):
    token = None


class _FakeElevate:
    """The profile reader, counting reads so "once per session" is testable."""

    def __init__(self, profile=None, raises=None):
        self._profile = profile if profile is not None else {
            "name": "Asha Devi Kumari",
            "role": "Head Teacher",
            "school_name": "GHS Ramnagar",
            "district": "Bengaluru Rural",
            "state": "Karnataka",
        }
        self._raises = raises
        self.tokens = []

    def read_profile(self, token):
        self.tokens.append(token)
        if self._raises is not None:
            raise self._raises
        return self._profile


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


def _discussion(**overrides):
    """The Capture Discussions binding: its own bot, opted in, still a guest."""
    return remote_spec(
        flow_name="guest-discussion",
        bot_route="/saarthi_discussion_flow",
        finalize_path="/api/end-story/",
        finalize_as_guest=True,
        send_user_profile=True,
        **overrides,
    )


# ---------------------------------------------------------------------------
# Opted in: the fields are sent
# ---------------------------------------------------------------------------

def test_the_elevate_profile_reaches_mitras_own_column_names():
    """The mapping is the point. Mitra's MOM renderer reads `profile.first_name`
    and falls back to a name scraped out of the transcript when it is blank, so
    the left-hand names here are not interchangeable with any others."""
    rest, elevate = _FakeRest(), _FakeElevate()
    remote = _discussion()

    _provider(rest, remote, elevate).open_session(remote, _Fresh(), _User())

    assert rest.calls[0]["extra"] == {
        "first_name": "Asha Devi Kumari",
        "designation": "Head Teacher",
        "org_associated": "GHS Ramnagar",
        "location": "Bengaluru Rural, Karnataka",
    }


def test_the_identifying_keys_cannot_be_overwritten_by_the_profile():
    """Mitra resolves the profile by (email, company). The profile fields come
    from a different source, so a collision would move the write to a DIFFERENT
    profile -- and the response shape is identical either way."""
    rest = _FakeRest()
    remote = _discussion()
    elevate = _FakeElevate({"name": "Asha", "role": "", "school_name": "",
                            "district": "", "state": ""})

    _provider(rest, remote, elevate).open_session(remote, _Fresh(), _User())

    call = rest.calls[0]
    assert call["email"] == "1355@shikshalokam.org"
    assert call["company"] == "testco"
    assert call["latest_flow_used"] == "guest-discussion"
    assert set(call["extra"]) == {"first_name"}


def test_blank_profile_values_are_dropped_rather_than_sent_as_empty_strings():
    """Mitra runs a NON-partial serializer over an existing profile, so an
    omitted key keeps its stored value while "" overwrites a good one with a
    blank. A half-filled ELEVATE profile must not erase what Mitra already had.
    """
    rest = _FakeRest()
    remote = _discussion()
    elevate = _FakeElevate({
        "name": "Asha Devi Kumari", "role": None, "school_name": "   ",
        "district": "", "state": "",
    })

    _provider(rest, remote, elevate).open_session(remote, _Fresh(), _User())

    assert rest.calls[0]["extra"] == {"first_name": "Asha Devi Kumari"}


def test_location_joins_district_and_state_but_skips_a_missing_half():
    rest = _FakeRest()
    remote = _discussion()
    elevate = _FakeElevate({"name": "Asha", "role": "Teacher", "school_name": "GHS",
                            "district": "", "state": "Karnataka"})

    _provider(rest, remote, elevate).open_session(remote, _Fresh(), _User())

    assert rest.calls[0]["extra"]["location"] == "Karnataka"


def test_the_language_field_is_never_sent():
    """Mitra's post_profile treats the presence of `preferred_route` as a request
    to transliterate first_name through AI4Bharat, so sending the user's
    language would quietly rewrite their name in the report."""
    rest, elevate = _FakeRest(), _FakeElevate()
    remote = _discussion()

    _provider(rest, remote, elevate).open_session(remote, _Fresh(), _User())

    assert "preferred_route" not in rest.calls[0]["extra"]


# ---------------------------------------------------------------------------
# Once per session, not once per turn
# ---------------------------------------------------------------------------

def test_the_profile_is_read_once_per_session_not_once_per_turn():
    """It is a blocking outbound call on a thread that already holds a pooled DB
    connection. The early return in open_session is what bounds it, so this
    pins the consequence rather than the branch."""
    rest, elevate = _FakeRest(), _FakeElevate()
    remote = _discussion()
    provider = _provider(rest, remote, elevate)

    provider.open_session(remote, _Fresh(), _User())
    provider.open_session(remote, _Established(), _User())
    provider.open_session(remote, _Established(), _User())

    assert len(elevate.tokens) == 1
    assert len(rest.calls) == 1


def test_the_read_uses_the_callers_own_token():
    """One provider instance is cached per (name, connection checksum) and
    shared across every conversation on it, so the credential must arrive as an
    argument. A token held on the instance would serve one user's profile to
    whoever opened a session next."""
    rest, elevate = _FakeRest(), _FakeElevate()
    remote = _discussion()

    _provider(rest, remote, elevate).open_session(remote, _Fresh(), _User())

    assert elevate.tokens == ["the-callers-own-token"]


# ---------------------------------------------------------------------------
# Degradation: a failed lookup is not a failed interview
# ---------------------------------------------------------------------------

def test_a_caller_with_no_token_still_sends_the_jwt_name():
    """AUTH_CHECK=false has no token at all. The interview must still run, and
    display_name is a JWT claim that needs no call."""
    rest, elevate = _FakeRest(), _FakeElevate()
    remote = _discussion()

    _provider(rest, remote, elevate).open_session(remote, _Fresh(), _NoToken())

    assert rest.calls[0]["extra"] == {"first_name": "Asha Devi"}
    assert elevate.tokens == []


def test_an_unconfigured_profile_reader_still_sends_the_jwt_name():
    """ELEVATE_BASE_URL unset means container.elevate is None."""
    rest = _FakeRest()
    remote = _discussion()

    _provider(rest, remote, None).open_session(remote, _Fresh(), _User())

    assert rest.calls[0]["extra"] == {"first_name": "Asha Devi"}


def test_a_failing_profile_read_degrades_instead_of_failing_the_interview():
    """The fields are an improvement on what Mitra would otherwise infer, not a
    precondition for capturing a discussion."""
    rest = _FakeRest()
    remote = _discussion()
    elevate = _FakeElevate(raises=RuntimeError("elevate is down"))

    init = _provider(rest, remote, elevate).open_session(remote, _Fresh(), _User())

    assert rest.calls[0]["extra"] == {"first_name": "Asha Devi"}
    assert init.remote_session_id == "remote-session-1"


def test_the_jwt_name_backfills_a_profile_with_no_name():
    rest = _FakeRest()
    remote = _discussion()
    elevate = _FakeElevate({"name": "", "role": "Teacher", "school_name": "",
                            "district": "", "state": ""})

    _provider(rest, remote, elevate).open_session(remote, _Fresh(), _User())

    assert rest.calls[0]["extra"]["first_name"] == "Asha Devi"


# ---------------------------------------------------------------------------
# NOT opted in: the regression pin for the story flow
# ---------------------------------------------------------------------------

def test_an_agent_that_did_not_opt_in_sends_no_extra_fields_at_all():
    """`record_stories` is on this same class, this same client and this same
    Mitra deployment. Its body must be what it always was."""
    rest, elevate = _FakeRest(), _FakeElevate()
    # The story binding: default options, so send_user_profile is absent.
    remote = remote_spec(flow_name="guest-mi-story", bot_route="/guided_guest")

    _provider(rest, remote, elevate).open_session(remote, _Fresh(), _User())

    assert rest.calls[0]["extra"] == {}
    assert elevate.tokens == [], "an opted-out agent must not even read the profile"


def test_send_user_profile_defaults_to_false():
    """The default is the isolation. Were it true, adding the flag would have
    changed the story flow's wire body with no config edit anywhere."""
    assert MitraOptions(bot_route="/x", company="c").send_user_profile is False


def test_the_two_agents_do_not_share_a_provider_instance():
    """The structural half of the isolation, and the reason a per-agent flag is
    safe on a class both agents share: ProviderRegistry caches by
    (name, connection.checksum), and RemoteConnection folds `options` into that
    checksum -- so the story agent's instance cannot observe the discussion
    agent's flag no matter what either config says."""
    discussion = _discussion()
    story = remote_spec(flow_name="guest-mi-story", bot_route="/guided_guest")

    discussion_conn = resolve_connection(
        _Settings(), discussion, MitraOptions(**discussion.options))
    story_conn = resolve_connection(
        _Settings(), story, MitraOptions(**story.options))

    assert discussion_conn.checksum != story_conn.checksum
