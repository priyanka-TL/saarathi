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

from app.providers.saathi.auth import (
    LOGIN_PATH,
    TENANT_HEADER,
    LoginTokenProvider,
    StaticTokenProvider,
    build_token_provider,
)
from app.providers.errors import ProviderAuthError, ProviderConfigError

ELEVATE = "https://elevate.example.org"
LOGIN_URL = f"{ELEVATE}{LOGIN_PATH}"


class _Conn:
    """Duck-typed stand-in for a resolved RemoteConnection.

    The builder reads its scheme and endpoint off `auth` -- which the CONFIG ROW
    supplies -- and its credential VALUES off `secrets`, which
    resolve_connection read from the variables that row NAMED. That split is the
    whole point: one deployment can now serve several identities, because the
    scheme and the tenant travel with the agent rather than with the process.
    """

    def __init__(self, **kw):
        self.auth = (
            ("scheme", kw.get("scheme", "elevate_login")),
            ("token_endpoint", kw.get("base_url", ELEVATE)),
            ("tenant_code", kw.get("tenant_code", "saathi")),
            ("credential_env", "SAATHI_ORIGIN_URL"),
            ("identifier_env", "SAATHI_EMAIL"),
            ("secret_env", "SAATHI_PASSWORD"),
            ("token_env", "SAATHI_ACCESS_TOKEN"),
        )
        self.secrets = {
            "identifier_env": kw.get("email") or "",
            "secret_env": kw.get("password") or "",
            "token_env": kw.get("token") or "",
        }
        self.connect_timeout_s = 10.0
        self.read_timeout_s = 30.0


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

    with pytest.raises(ProviderAuthError) as excinfo:
        provider.get()

    assert excinfo.value.mechanism == "static_token"
    assert "remote.auth.scheme='elevate_login'" in str(excinfo.value)


def test_static_rejects_an_empty_token_at_construction():
    """At BOOT, not at the first request -- a misconfigured deployment should
    fail to start rather than render a button that errors on click."""
    with pytest.raises(ProviderConfigError):
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

    with pytest.raises(ProviderAuthError) as excinfo:
        _login().get()

    assert excinfo.value.mechanism == "elevate_login"
    assert "Invalid credentials" in str(excinfo.value)


@responses.activate
def test_a_tokenless_success_is_an_error_not_an_empty_string():
    responses.add(responses.POST, LOGIN_URL, json={"result": {}}, status=200)

    with pytest.raises(ProviderAuthError):
        _login().get()


def test_login_repr_leaks_neither_identifier_nor_password():
    text = repr(_login())
    assert "a@b.com" not in text
    assert "pw" not in text


def test_login_requires_its_credentials_at_construction():
    with pytest.raises(ProviderConfigError):
        LoginTokenProvider(base_url=ELEVATE, identifier="", password="pw", tenant_code="saathi")
    with pytest.raises(ProviderConfigError):
        LoginTokenProvider(base_url=ELEVATE, identifier="a@b.com", password="", tenant_code="saathi")


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def test_build_selects_the_scheme_named_in_the_config_row():
    """The scheme travels with the AGENT now, not with the process.

    It used to be one SAATHI_LOGIN_MECHANISM setting, which meant one identity
    per deployment for every tenant.
    """
    assert build_token_provider(
        _Conn(scheme="static_token", token="t")).mechanism == "static_token"
    assert build_token_provider(
        _Conn(scheme="elevate_login", email="a@b.com", password="pw")
    ).mechanism == "elevate_login"


def test_build_rejects_an_unknown_scheme_by_name():
    with pytest.raises(ProviderConfigError) as excinfo:
        build_token_provider(_Conn(scheme="oauth"))
    assert "oauth" in str(excinfo.value)
    # And names the ones that DO work, so the operator can fix it in one pass.
    assert "elevate_login" in str(excinfo.value)
