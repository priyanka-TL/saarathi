"""The ELEVATE profile integration: field unwrapping, the HTTP client, policy.

No network. `responses` intercepts the transport, following
`tests/unit/test_bhashini.py`.

The unwrapping cases are not decoration. ELEVATE sends the same attribute as a
dict, a bare string, a list or null depending on tenant configuration, and the
Django original this was ported from handles only two of its four attributes
that way -- the other two would raise AttributeError on a plain string. Each
shape gets a case so that asymmetry cannot come back.
"""

from __future__ import annotations

import pytest
import requests
import responses

from app.integrations.elevate.client import ElevateUserClient
from app.integrations.elevate.exceptions import (
    ElevateRejected,
    ElevateTimeout,
    ElevateUnauthorized,
    ElevateUpstreamError,
)
from app.integrations.elevate.mapping import label_of, to_profile, to_update_body, value_of
from app.services.profile_service import (
    InvalidProfileUpdate,
    MAX_FIELD_LENGTH,
    ProfileService,
    clean_update,
    is_profile_complete,
    missing_fields,
    serialize,
)

BASE_URL = "https://elevate.example"
READ_URL = f"{BASE_URL}/user/v1/user/read"
UPDATE_URL = f"{BASE_URL}/user/v1/user/update"
TOKEN = "caller-jwt"


def _client(**overrides) -> ElevateUserClient:
    # Timeouts stated explicitly: the client takes no defaults, so that Settings
    # stays the single declaration of each production value.
    kwargs = {"base_url": BASE_URL, "connect_timeout": 10.0, "read_timeout": 30.0}
    kwargs.update(overrides)
    return ElevateUserClient(**kwargs)


def _ok(result: dict) -> dict:
    return {"responseCode": "OK", "result": result}


def _full_result() -> dict:
    return {
        "id": 1355,
        "name": "Asha Rao",
        "userRole": {"value": "teacher", "label": "Teacher"},
        "userSchool": {"value": "s1", "label": "GHS Anekal"},
        "userDistrict": {"value": "d1", "label": "Bengaluru Urban"},
        "profileState": {"value": "ka", "label": "Karnataka"},
        "preferred_language": {"value": "kn", "label": "Kannada"},
        "has_accepted_terms_and_conditions": True,
    }


# ---------------------------------------------------------------------------
# field unwrapping
# ---------------------------------------------------------------------------

def test_a_dict_attribute_yields_its_label_and_value():
    assert label_of({"value": "teacher", "label": "Teacher"}) == "Teacher"
    assert value_of({"value": "kn", "label": "Kannada"}) == "kn"


@pytest.mark.parametrize("field", ["userRole", "userSchool", "userDistrict", "profileState"])
def test_a_plain_string_attribute_does_not_raise(field):
    """The bug in the original, pinned for all four attributes.

    `fetch_elevate_user` does `(user_data.get('profileState') or {}).get('label')`
    -- which is an AttributeError the moment ELEVATE sends a str, and a 500 for
    the user. `userRole`/`userSchool` branch on type there; the other two do not.
    """
    result = {"id": 7, field: "Karnataka"}

    profile = to_profile(result)

    assert "Karnataka" in profile.values()


@pytest.mark.parametrize("raw", [None, "", {}, {"label": ""}, {"label": None}, []])
def test_an_unset_attribute_becomes_none_however_it_is_spelled(raw):
    assert label_of(raw) is None


def test_a_multi_select_attribute_takes_the_first_populated_entry():
    # Joining them would write a composite string back on the next save.
    assert label_of([{"label": ""}, {"label": "Teacher"}, {"label": "Head"}]) == "Teacher"


def test_preferred_language_defaults_to_en_when_absent():
    # It decides which language the assistant replies in, so it cannot be null.
    assert to_profile({"id": 1})["preferred_language"] == "en"


def test_a_full_result_maps_onto_saarthis_own_field_names():
    profile = to_profile(_full_result())

    assert profile == {
        "user_id": "1355",
        "name": "Asha Rao",
        "role": "Teacher",
        "school_name": "GHS Anekal",
        "district": "Bengaluru Urban",
        "state": "Karnataka",
        "preferred_language": "kn",
        "has_accepted_tnc": True,
    }
    # ELEVATE's own spellings stop at the integration boundary.
    assert not {"userRole", "userSchool", "userDistrict", "profileState"} & profile.keys()


# ---------------------------------------------------------------------------
# the PATCH body
# ---------------------------------------------------------------------------

def test_the_update_body_carries_only_the_fields_supplied():
    body = to_update_body({"role": "Teacher"})

    assert body == {"userRole": "Teacher"}


def test_the_update_body_never_sends_the_hardcoded_about():
    """The original seeds every PATCH with a placeholder `about`.

    Its own comment says "hardcoded for now"; porting it would overwrite the
    user's real bio on every single save.
    """
    body = to_update_body({"name": "A", "role": "B", "school_name": "C",
                           "district": "D", "state": "E"})

    assert "about" not in body
    assert set(body) == {"name", "userRole", "userSchool", "userDistrict", "profileState"}


# ---------------------------------------------------------------------------
# completeness policy
# ---------------------------------------------------------------------------

def test_a_fully_populated_profile_is_complete():
    assert is_profile_complete(to_profile(_full_result()))


def test_missing_fields_are_reported_in_a_stable_order():
    profile = to_profile({"id": 1, "name": "Asha Rao", "userRole": "Teacher"})

    assert missing_fields(profile) == ["school_name", "district", "state"]


@pytest.mark.parametrize("blank", ["", "   ", "\t", None])
def test_a_blank_value_counts_as_missing(blank):
    """Without the strip(), one typed space marks someone permanently complete."""
    profile = {"name": "A", "role": "B", "school_name": "C", "district": "D", "state": blank}

    assert missing_fields(profile) == ["state"]


def test_serialize_returns_one_shape_for_both_routes():
    body = serialize(to_profile({"id": 1, "name": "Asha Rao"}))

    assert set(body) == {"profile", "is_complete", "missing_fields"}
    assert body["is_complete"] is False
    assert body["missing_fields"] == ["role", "school_name", "district", "state"]


# ---------------------------------------------------------------------------
# PATCH body validation
# ---------------------------------------------------------------------------

def test_a_partial_update_is_accepted_and_trimmed():
    assert clean_update({"role": "  Teacher  "}) == {"role": "Teacher"}


def test_an_unknown_key_is_ignored_rather_than_rejected():
    # A client sending a field this version does not know about still gets its
    # other changes applied.
    assert clean_update({"role": "Teacher", "nickname": "x"}) == {"role": "Teacher"}


@pytest.mark.parametrize(
    "body",
    [
        None,                              # no body at all
        [],                                # not an object
        {},                                # nothing to change
        {"nickname": "x"},                 # nothing RECOGNISED to change
        {"role": ""},                      # blank would erase a good value
        {"role": "   "},
        {"role": 7},                       # not a string
        {"role": "x" * (MAX_FIELD_LENGTH + 1)},
    ],
)
def test_an_unusable_update_is_refused(body):
    with pytest.raises(InvalidProfileUpdate):
        clean_update(body)


# ---------------------------------------------------------------------------
# the HTTP client
# ---------------------------------------------------------------------------

@responses.activate
def test_read_sends_the_callers_token_in_elevates_own_header():
    responses.add(responses.GET, READ_URL, json=_ok(_full_result()), status=200)

    profile = _client().read_profile(TOKEN)

    assert profile["role"] == "Teacher"
    # X-auth-token, NOT Authorization: Bearer -- only the header name differs.
    assert responses.calls[0].request.headers["X-auth-token"] == TOKEN
    assert "Authorization" not in responses.calls[0].request.headers


@responses.activate
def test_update_patches_a_sparse_body_and_returns_nothing():
    import json

    responses.add(responses.PATCH, UPDATE_URL, json=_ok({}), status=200)

    assert _client().update_profile(TOKEN, {"district": "Bengaluru Urban"}) is None

    sent = json.loads(responses.calls[0].request.body)
    assert sent == {"userDistrict": "Bengaluru Urban"}


@responses.activate
def test_a_401_becomes_unauthorized_and_nothing_else_does():
    """Only this may reach the router as a 401.

    The frontend clears the stored session on any 401, so mapping anything else
    to it would log a user out over a transient upstream fault.
    """
    responses.add(responses.GET, READ_URL, json={"message": "no"}, status=401)

    with pytest.raises(ElevateUnauthorized):
        _client().read_profile(TOKEN)


@pytest.mark.parametrize("status", [400, 422])
@responses.activate
def test_a_rejected_value_is_distinguishable_from_an_outage(status):
    responses.add(responses.PATCH, UPDATE_URL,
                  json={"message": "userRole is not a known entity"}, status=status)

    with pytest.raises(ElevateRejected) as caught:
        _client().update_profile(TOKEN, {"role": "Wizard"})

    assert caught.value.detail == "userRole is not a known entity"


@responses.activate
def test_a_server_error_becomes_an_upstream_error():
    responses.add(responses.GET, READ_URL, json={"message": "boom"}, status=503)

    with pytest.raises(ElevateUpstreamError) as caught:
        _client().read_profile(TOKEN)

    assert caught.value.status == 503


@responses.activate
def test_a_redirect_is_an_upstream_error_and_the_token_is_not_forwarded():
    """A 3xx must never be followed with this header attached.

    `requests` re-sends headers across a redirect, and the header holds the
    user's live session token -- so one misconfigured ELEVATE_BASE_URL would
    hand it to whatever answered. The original follows redirects.
    """
    responses.add(responses.GET, READ_URL, status=302,
                  headers={"Location": "https://attacker.example/collect"})

    with pytest.raises(ElevateUpstreamError):
        _client().read_profile(TOKEN)

    assert len(responses.calls) == 1


@responses.activate
def test_a_200_with_a_failure_envelope_is_not_success():
    # ELEVATE answers 200 with responseCode != OK; the status alone is not enough.
    responses.add(responses.GET, READ_URL,
                  json={"responseCode": "CLIENT_ERROR", "result": {}}, status=200)

    with pytest.raises(ElevateUpstreamError):
        _client().read_profile(TOKEN)


@responses.activate
def test_a_non_json_body_is_an_upstream_error_not_a_crash():
    responses.add(responses.GET, READ_URL, body="<html>gateway</html>", status=200)

    with pytest.raises(ElevateUpstreamError):
        _client().read_profile(TOKEN)


@responses.activate
def test_a_result_without_an_id_is_an_error_not_an_empty_profile():
    """The original returns a bare `{}` here.

    Its callers cannot tell that apart from "this user has filled nothing in",
    so they would conclude every field is missing and PATCH over good data.
    """
    responses.add(responses.GET, READ_URL, json=_ok({"name": "Asha Rao"}), status=200)

    with pytest.raises(ElevateUpstreamError):
        _client().read_profile(TOKEN)


@responses.activate
def test_a_timeout_is_its_own_error_so_the_router_can_answer_504():
    responses.add(responses.GET, READ_URL, body=requests.exceptions.ConnectTimeout())

    with pytest.raises(ElevateTimeout):
        _client().read_profile(TOKEN)


@responses.activate
def test_a_connection_failure_never_leaks_the_configured_url():
    responses.add(responses.GET, READ_URL, body=requests.exceptions.ConnectionError("dns"))

    with pytest.raises(ElevateUpstreamError) as caught:
        _client().read_profile(TOKEN)

    assert BASE_URL not in str(caught.value)


def test_the_repr_does_not_print_a_token():
    # There is no stored token to leak -- that is the point of passing it per
    # call -- and the base URL is all the repr should ever show.
    assert repr(_client()) == f"<ElevateUserClient base_url={BASE_URL}>"


# ---------------------------------------------------------------------------
# the service
# ---------------------------------------------------------------------------

class _FakeClient:
    def __init__(self, profile):
        self.profile = profile
        self.updates = []

    def read_profile(self, token):
        return self.profile

    def update_profile(self, token, fields):
        self.updates.append(fields)
        # ELEVATE dropped `role`: it resolves against an entity list this
        # tenant has configured, and "Wizard" is not in it.
        self.profile = {**self.profile, **{k: v for k, v in fields.items() if k != "role"}}


def test_update_returns_a_fresh_read_not_an_echo_of_the_request():
    """Trusting the write would report a field as filled when it was dropped.

    The user would then never be asked for it again, and the profile would stay
    permanently incomplete with the UI insisting it was fine.
    """
    client = _FakeClient({"name": "Asha Rao", "role": None, "school_name": "GHS",
                          "district": "BU", "state": "KA"})
    service = ProfileService(client=client)

    refreshed = service.update("t", {"role": "Wizard", "state": "Kerala"})

    assert refreshed["state"] == "Kerala"      # accepted
    assert refreshed["role"] is None           # silently dropped upstream
    assert not is_profile_complete(refreshed)


def test_a_service_without_a_client_reports_itself_unavailable():
    # What the router turns into 503 PROFILE_UNAVAILABLE.
    assert ProfileService(client=None).available is False
    assert ProfileService(client=_FakeClient({})).available is True
