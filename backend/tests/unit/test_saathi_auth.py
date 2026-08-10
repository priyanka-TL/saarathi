"""Where Saathi's access token comes from, and what happens when one dies.

The 401 path is the one that matters. A JWT four days short of its own `exp`
was rejected by ELEVATE with "Session expired. Please login again." -- ELEVATE
tracks the session_id claim server-side and can end it at any moment. So a
token dying mid-conversation is an ordinary event, not an edge case, and the
refresh has to be exercised rather than merely written.
"""
from __future__ import annotations

import json

import pytest
import responses

from app.integrations.saathi.auth import (
    LOGIN_PATH,
    TENANT_HEADER,
    LoginTokenProvider,
    StaticTokenProvider,
    build_token_provider,
)
from app.integrations.saathi.exceptions import SaathiAuthError, SaathiConfigError

ELEVATE = "https://elevate.example.org"
LOGIN_URL = f"{ELEVATE}{LOGIN_PATH}"


class _Settings:
    """Duck-typed stand-in; build_token_provider only reads attributes."""

    def __init__(self, **kw):
        self.saathi_login_mechanism = kw.get("mechanism", "password")
        self.saathi_access_token = kw.get("token")
        self.saathi_email = kw.get("email")
        self.saathi_password = kw.get("password")
        self.saathi_tenant_code = kw.get("tenant_code", "saathi")
        self.elevate_base_url = kw.get("base_url", ELEVATE)


def _login(**kw) -> LoginTokenProvider:
    return LoginTokenProvider(
        base_url=ELEVATE, identifier="a@b.com", password="pw",
        tenant_code="saathi", **kw,
    )


# ---------------------------------------------------------------------------
# StaticTokenProvider
# ---------------------------------------------------------------------------


def test_static_returns_the_configured_token():
    assert StaticTokenProvider("tok-1").get() == "tok-1"


def test_static_refuses_to_hand_back_a_token_it_knows_is_dead():
    """Returning the same dead token would give the caller a second, identical
    401 and no explanation. The error names the mechanism and the way out."""
    provider = StaticTokenProvider("tok-1")
    provider.invalidate()

    with pytest.raises(SaathiAuthError) as excinfo:
        provider.get()

    assert excinfo.value.mechanism == "token"
    assert "SAATHI_LOGIN_MECHANISM=password" in str(excinfo.value)


def test_static_rejects_an_empty_token_at_construction():
    """At BOOT, not at the first request -- a misconfigured deployment should
    fail to start rather than render a button that errors on click."""
    with pytest.raises(SaathiConfigError):
        StaticTokenProvider("")


def test_static_repr_does_not_leak_the_token():
    assert "tok-secret" not in repr(StaticTokenProvider("tok-secret"))


# ---------------------------------------------------------------------------
# LoginTokenProvider
# ---------------------------------------------------------------------------


@responses.activate
def test_login_mints_a_token_and_caches_it():
    responses.add(responses.POST, LOGIN_URL,
                  json={"result": {"access_token": "minted-1"}}, status=200)

    provider = _login()
    assert provider.get() == "minted-1"
    assert provider.get() == "minted-1"
    assert len(responses.calls) == 1, "the token must be cached, not re-minted per call"


@responses.activate
def test_login_sends_the_tenant_header_and_the_identifier_field():
    """Both were established against the live QA service: without the tenant
    header ELEVATE answers 406 'Tenant domain not found', and the identity
    field is `identifier`, NOT `email`."""
    responses.add(responses.POST, LOGIN_URL,
                  json={"result": {"access_token": "t"}}, status=200)

    _login().get()

    request = responses.calls[0].request
    assert request.headers[TENANT_HEADER] == "saathi"
    body = json.loads(request.body)
    assert body["identifier"] == "a@b.com"
    assert "email" not in body


@responses.activate
def test_invalidate_forces_a_fresh_mint():
    """The 401 recovery path: a caller that saw a 401 invalidates and retries."""
    responses.add(responses.POST, LOGIN_URL,
                  json={"result": {"access_token": "first"}}, status=200)
    responses.add(responses.POST, LOGIN_URL,
                  json={"result": {"access_token": "second"}}, status=200)

    provider = _login()
    assert provider.get() == "first"
    provider.invalidate()
    assert provider.get() == "second"


@responses.activate
@pytest.mark.parametrize("body", [
    {"result": {"access_token": "t"}},
    {"result": {"accessToken": "t"}},
    {"access_token": "t"},
    {"result": {"tokens": {"access": "t"}}},
])
def test_the_token_is_found_in_every_envelope_shape_seen(body):
    """The envelope is ELEVATE's, not ours. A single wrong guess would fail at
    runtime as a token-less success, which is worse than a loud error."""
    responses.add(responses.POST, LOGIN_URL, json=body, status=200)
    assert _login().get() == "t"


@responses.activate
def test_a_quoted_token_is_unquoted():
    """Some ELEVATE builds return the JWT wrapped in quotes; Saathi's own
    client strips them too. A quoted token is not rejected as malformed -- it
    simply authenticates as nobody."""
    responses.add(responses.POST, LOGIN_URL,
                  json={"result": {"access_token": '"tok-quoted"'}}, status=200)
    assert _login().get() == "tok-quoted"


@responses.activate
def test_rejected_credentials_raise_rather_than_return_empty():
    responses.add(responses.POST, LOGIN_URL,
                  json={"message": "Invalid credentials"}, status=400)

    with pytest.raises(SaathiAuthError) as excinfo:
        _login().get()

    assert excinfo.value.mechanism == "password"
    assert "Invalid credentials" in str(excinfo.value)


@responses.activate
def test_a_tokenless_success_is_an_error_not_an_empty_string():
    responses.add(responses.POST, LOGIN_URL, json={"result": {}}, status=200)

    with pytest.raises(SaathiAuthError):
        _login().get()


def test_login_repr_leaks_neither_identifier_nor_password():
    text = repr(_login())
    assert "a@b.com" not in text
    assert "pw" not in text


def test_login_requires_its_credentials_at_construction():
    with pytest.raises(SaathiConfigError):
        LoginTokenProvider(base_url=ELEVATE, identifier="", password="pw", tenant_code="saathi")
    with pytest.raises(SaathiConfigError):
        LoginTokenProvider(base_url=ELEVATE, identifier="a@b.com", password="", tenant_code="saathi")


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def test_build_selects_the_mechanism_named_in_settings():
    assert build_token_provider(
        _Settings(mechanism="token", token="t")).mechanism == "token"
    assert build_token_provider(
        _Settings(mechanism="password", email="a@b.com", password="pw")).mechanism == "password"


def test_build_defaults_to_password():
    """The only mechanism that survives ELEVATE ending a session."""
    assert build_token_provider(
        _Settings(mechanism="", email="a@b.com", password="pw")).mechanism == "password"


def test_build_rejects_an_unknown_mechanism_by_name():
    with pytest.raises(SaathiConfigError) as excinfo:
        build_token_provider(_Settings(mechanism="oauth"))
    assert "oauth" in str(excinfo.value)
