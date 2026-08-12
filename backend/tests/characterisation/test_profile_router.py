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


class FakeElevate:
    """Stands in for ElevateUserClient. Records the token it was called with."""

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
        self.profile.update(fields)


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

def test_patch_sends_only_the_supplied_fields(client, elevate):
    """Sparse, so editing one field cannot blank the other four."""
    response = client.patch("/api/profile", json={"district": "Mysuru"})

    assert response.status_code == 200
    assert elevate.updates == [{"district": "Mysuru"}]


def test_patch_returns_the_refreshed_profile_so_the_ui_need_not_re_read(client, elevate):
    elevate.profile = {**COMPLETE, "district": None}

    body = client.patch("/api/profile", json={"district": "Mysuru"}).json()

    assert body["profile"]["district"] == "Mysuru"
    assert body["is_complete"] is True
    assert body["missing_fields"] == []


def test_patch_trims_whitespace_before_writing(client, elevate):
    client.patch("/api/profile", json={"role": "  Teacher  "})

    assert elevate.updates == [{"role": "Teacher"}]


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
