"""The /api/profile surface, end to end through the TestClient.

The ELEVATE user service is replaced with a fake -- the suite runs with
`--disable-socket` and ELEVATE is a live service holding real people's data, so
what is pinned here is this app's contract: the response shape, the error
envelopes, the gating, and the fact that neither route takes a user id.

`ELEVATE_BASE_URL` is set to an unresolvable `.invalid` host in
tests/conftest.py, so a real client IS built and these tests swap a fake over
it. The unconfigured case clears it explicitly instead.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.integrations.elevate.exceptions import (
    ElevateRejected,
    ElevateTimeout,
    ElevateUnauthorized,
    ElevateUpstreamError,
)

COMPLETE = {
    "user_id": "1355",
    "name": "Asha Rao",
    "role": "Teacher",
    "school_name": "GHS Anekal",
    "district": "Bengaluru Urban",
    "state": "Karnataka",
    "preferred_language": "kn",
    "has_accepted_tnc": True,
}

# What a write leaves behind for everything the body did NOT carry. The five
# profile fields are cleared; identity and settings ELEVATE holds elsewhere are
# not, which is why they are listed here rather than blanked with the rest.
CLEARED_BY_A_WRITE = {
    "user_id": "1355",
    "name": None,
    "role": None,
    "school_name": None,
    "district": None,
    "state": None,
    "preferred_language": "kn",
    "has_accepted_tnc": True,
}


class FakeElevate:
    """Stands in for ElevateUserClient. Records the token it was called with.

    `update_profile` REPLACES rather than merges, because that is what ELEVATE
    actually does: any of the five profile fields the body omits comes back
    null on the next read.

    THIS FAKE USED TO DO `self.profile.update(fields)` -- a dict merge -- and
    that single line is why the sparse-PATCH data-loss bug shipped green. The
    fake was more forgiving than the real service, so a test suite that
    explicitly asserted "editing one field must not blank the other four"
    passed while production blanked them. Do not soften it back.
    """

    def __init__(self, profile=None):
        self.profile = dict(profile if profile is not None else COMPLETE)
        self.read_error: Exception | None = None
        self.update_error: Exception | None = None
        self.tokens: list[str] = []
        self.updates: list[dict] = []

    def read_profile(self, token):
        self.tokens.append(token)
        if self.read_error:
            raise self.read_error
        return dict(self.profile)

    def update_profile(self, token, fields):
        self.tokens.append(token)
        self.updates.append(fields)
        if self.update_error:
            raise self.update_error
        self.profile = {**CLEARED_BY_A_WRITE, **fields}


@pytest.fixture()
def elevate(api_app):
    """Swap a fake ELEVATE client onto the container, and put it back after.

    `Container` is a frozen dataclass, so this replaces it wholesale rather than
    mutating it -- the same thing build_container would have produced.
    """
    original = api_app.state.container
    fake = FakeElevate()
    api_app.state.container = dataclasses.replace(original, elevate=fake)
    yield fake
    api_app.state.container = original


@pytest.fixture()
def unconfigured(api_app):
    """A deployment with no ELEVATE_BASE_URL set."""
    original = api_app.state.container
    api_app.state.container = dataclasses.replace(original, elevate=None)
    yield
    api_app.state.container = original


# ---------------------------------------------------------------------------
# gating
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,kwargs", [("get", {}), ("patch", {"json": {"role": "Teacher"}})])
def test_both_routes_503_when_no_elevate_service_is_configured(
    client, unconfigured, method, kwargs
):
    """PROFILE_UNAVAILABLE, not an error the caller can act on.

    Mirrors VOICE_DISABLED: the frontend reads it as "hide the Profile section"
    rather than showing a failure, so an existing deployment that never sets
    ELEVATE_BASE_URL is unaffected by this feature existing.
    """
    response = getattr(client, method)("/api/profile", **kwargs)

    assert response.status_code == 503
    assert response.json()["error_code"] == "PROFILE_UNAVAILABLE"


@pytest.mark.parametrize("method,kwargs", [("get", {}), ("patch", {"json": {"role": "Teacher"}})])
def test_both_routes_401_without_a_credential(anonymous_client, elevate, method, kwargs):
    response = getattr(anonymous_client, method)("/api/profile", **kwargs)

    assert response.status_code == 401
    assert elevate.tokens == []  # never reached the upstream


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------

def test_get_returns_the_profile_with_completeness_computed_server_side(client, elevate):
    response = client.get("/api/profile")

    assert response.status_code == 200
    assert response.json() == {
        "profile": COMPLETE,
        "is_complete": True,
        "missing_fields": [],
    }


def test_get_reports_which_mandatory_fields_are_missing(client, elevate):
    """The list the dialog needs, decided here rather than duplicated in JS.

    Duplicated, it would drift the first time a sixth field is added and the
    popup would disagree with the API about whether it should be showing.
    """
    elevate.profile = {**COMPLETE, "school_name": "", "state": None}

    body = client.get("/api/profile").json()

    assert body["is_complete"] is False
    assert body["missing_fields"] == ["school_name", "state"]


def test_the_callers_own_token_is_what_reaches_elevate(client, elevate):
    """No user id is accepted or sent -- the token IS the identity.

    get_current_user has already verified its signature, and that same JWT is
    what ELEVATE is asked with, so a caller can only ever read their own
    profile and there is no id parameter to tamper with.
    """
    client.get("/api/profile")

    assert len(elevate.tokens) == 1
    assert elevate.tokens[0]  # a real token, not None


# ---------------------------------------------------------------------------
# PATCH
# ---------------------------------------------------------------------------

def test_a_single_field_edit_still_sends_elevate_the_complete_set(client, elevate):
    """The client request stays sparse; the UPSTREAM body must not be.

    This assertion is the inverse of the one it replaces, which read
    `elevate.updates == [{"district": "Mysuru"}]` under the banner "sparse, so
    editing one field cannot blank the other four". That was exactly backwards:
    ELEVATE clears what the body omits, so a sparse body is what blanked them.
    """
    response = client.patch("/api/profile", json={"district": "Mysuru"})

    assert response.status_code == 200
    assert elevate.updates == [{
        "name": "Asha Rao",
        "role": "Teacher",
        "school_name": "GHS Anekal",
        "district": "Mysuru",       # the edit
        "state": "Karnataka",
    }]


def test_consecutive_single_field_updates_lose_nothing(client, elevate):
    """The reported bug, as its own regression test.

    Three saves in a row, each touching ONE field. Before the read-merge-write
    fix the third response came back with only `name` populated and everything
    else null -- which is precisely what the user reported and screenshotted.
    """
    client.patch("/api/profile", json={"district": "Mysuru"})
    client.patch("/api/profile", json={"role": "Head Teacher"})
    body = client.patch("/api/profile", json={"name": "P P"}).json()

    assert body["profile"] == {
        "user_id": "1355",
        "name": "P P",
        "role": "Head Teacher",
        "school_name": "GHS Anekal",
        "district": "Mysuru",
        "state": "Karnataka",
        "preferred_language": "kn",
        "has_accepted_tnc": True,
    }
    assert body["is_complete"] is True
    assert body["missing_fields"] == []


def test_a_field_never_set_is_omitted_rather_than_sent_blank(client, elevate):
    """Sending "" would ask ELEVATE to store a blank, not to leave it unset."""
    elevate.profile = {**COMPLETE, "state": None}

    client.patch("/api/profile", json={"district": "Mysuru"})

    assert "state" not in elevate.updates[0]
    assert elevate.updates[0]["district"] == "Mysuru"


def test_patch_returns_the_refreshed_profile_so_the_ui_need_not_re_read(client, elevate):
    elevate.profile = {**COMPLETE, "district": None}

    body = client.patch("/api/profile", json={"district": "Mysuru"}).json()

    assert body["profile"]["district"] == "Mysuru"
    assert body["is_complete"] is True
    assert body["missing_fields"] == []


def test_patch_trims_whitespace_before_writing(client, elevate):
    client.patch("/api/profile", json={"role": "  Head Teacher  "})

    # Trimmed -- and carried alongside the four fields the edit did not touch,
    # which is what stops the write clearing them.
    assert elevate.updates[0]["role"] == "Head Teacher"
    assert elevate.updates[0]["school_name"] == "GHS Anekal"


@pytest.mark.parametrize(
    "body",
    [
        None,                    # no body at all
        {},                      # nothing to change
        {"nickname": "x"},       # nothing RECOGNISED to change
        {"role": ""},            # blank would erase a good value upstream
        {"role": "   "},
        {"role": 7},             # not a string
        {"role": "x" * 201},     # a paste accident, not a job title
    ],
)
def test_patch_refuses_an_unusable_body_without_calling_elevate(client, elevate, body):
    response = client.patch("/api/profile", json=body)

    assert response.status_code == 400
    assert response.json()["error_code"] == "INVALID_REQUEST"
    assert elevate.updates == []


# ---------------------------------------------------------------------------
# error mapping
# ---------------------------------------------------------------------------

def test_an_expired_elevate_session_is_the_only_thing_that_becomes_a_401(client, elevate):
    """The frontend clears the stored session on any 401.

    That is right here -- our own JWT verification passed, so a 401 from ELEVATE
    means the session ended upstream -- and a spurious logout for anything else,
    which is why the cases below map to 502/504 instead.
    """
    elevate.read_error = ElevateUnauthorized("token revoked")

    response = client.get("/api/profile")

    assert response.status_code == 401
    assert response.json()["error_code"] == "UNAUTHORIZED"


def test_a_rejected_value_is_a_400_the_user_can_fix(client, elevate):
    # Free-text role/school may be validated against a tenant's entity list.
    # Telling the user to retry would send them round a loop that always fails.
    elevate.update_error = ElevateRejected("userRole is not a known entity")

    response = client.patch("/api/profile", json={"role": "Wizard"})

    assert response.status_code == 400
    assert response.json()["error_code"] == "PROFILE_REJECTED"
    assert response.json()["error"] == "userRole is not a known entity"


def test_an_upstream_outage_is_a_502(client, elevate):
    elevate.read_error = ElevateUpstreamError("read", 503)

    response = client.get("/api/profile")

    assert response.status_code == 502
    assert response.json()["error_code"] == "PROFILE_UPSTREAM_ERROR"


def test_a_timeout_is_a_504(client, elevate):
    elevate.read_error = ElevateTimeout("read", 30.0)

    response = client.get("/api/profile")

    assert response.status_code == 504
    assert response.json()["error_code"] == "UPSTREAM_TIMEOUT"


def test_a_failed_re_read_still_reports_the_save_that_landed(client, elevate):
    """The write succeeded; only the confirming read failed.

    Answering 502 here would send the user back to retry a save that already
    happened, and show them stale values while they did it. The merged values
    are what was just written, so they are the honest answer.
    """
    calls = {"n": 0}
    original_read = elevate.read_profile

    def read_then_fail(token):
        calls["n"] += 1
        if calls["n"] > 1:                      # the post-write re-read
            raise ElevateUpstreamError("read", 503)
        return original_read(token)

    elevate.read_profile = read_then_fail

    response = client.patch("/api/profile", json={"district": "Mysuru"})

    assert response.status_code == 200
    assert response.json()["profile"]["district"] == "Mysuru"
    # And nothing else was lost on the way through.
    assert response.json()["profile"]["school_name"] == "GHS Anekal"


def test_a_failed_pre_read_does_not_write_a_half_known_profile(client, elevate):
    """No baseline means no safe merge, so the write must not go out at all.

    Writing with an empty baseline would send only the caller's one field --
    exactly the sparse body that caused the data loss.
    """
    elevate.read_error = ElevateUpstreamError("read", 503)

    response = client.patch("/api/profile", json={"district": "Mysuru"})

    assert response.status_code == 502
    assert elevate.updates == []


def test_an_upstream_failure_never_echoes_elevates_body(client, elevate):
    """`/user/v1/user/read` answers with the user's entire profile.

    So a handler that passed the upstream body through would put someone else's
    PII in an error response and every log line that formatted it.
    """
    elevate.read_error = ElevateUpstreamError("read", 500, "raw body with PII")

    body = client.get("/api/profile").json()

    assert "PII" not in body["error"]
    assert body["error"] == "Could not reach the profile service."


def test_every_error_carries_the_standard_envelope(client, elevate):
    elevate.read_error = ElevateUpstreamError("read", 500)

    body = client.get("/api/profile").json()

    assert set(body) == {"status", "error", "error_code", "request_id"}
    assert body["status"] == "error"
