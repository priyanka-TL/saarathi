"""Where Saathi's per-turn access token comes from: the caller, never a mint.

This used to test app/providers/saathi/auth.py's LoginTokenProvider /
StaticTokenProvider -- a connection-level credential shared by every user of a
tenant's Saathi agent, minted from a service login or a hardcoded env var. That
module is gone. Saathi's docstring always called it "a per-user assistant
conversation", and the fix that makes that literally true is: the credential is
`UserContext.token`, the caller's own already-verified ELEVATE JWT from their
own login (app/services/identity.py), threaded through as an explicit
per-call argument -- never minted, cached, or stored on the provider instance,
because that instance is shared across every conversation on this tenant's
connection (ProviderRegistry.get() caches by (provider, conn.checksum), not
per user).
"""
from __future__ import annotations

import pytest
import responses as resp_lib

from app.domain.core import OrgMembership, UserContext
from app.providers.connection import resolve_connection
from app.providers.errors import ProviderAuthError
from app.providers.saathi.provider import AUTH_HEADER, SaathiProvider
from app.providers.saathi.spec import SaathiOptions
from tests.provider_factories import SAATHI_ORIGIN_ENV, remote_spec


def _user(token: str | None) -> UserContext:
    return UserContext(
        user_id="1",
        email="1@shikshalokam.org",
        display_name="Test User",
        tenant_code="saathi",
        orgs=(OrgMembership(org_id="1", org_code="default", roles=("mentee",)),),
        active_org_id="1",
        token=token,
    )


@pytest.fixture()
def provider(monkeypatch) -> SaathiProvider:
    monkeypatch.setenv(SAATHI_ORIGIN_ENV, "https://saathi-origin.test")
    spec = remote_spec("saathi")
    options = SaathiOptions.model_validate(spec.options)
    conn = resolve_connection(_Settings(), spec, options)
    return SaathiProvider(conn, pool=None)


class _Settings:
    provider_host_ceiling = ""


# ---------------------------------------------------------------------------
# _access_token: the caller's own token, no fallback
# ---------------------------------------------------------------------------


def test_access_token_returns_the_callers_own_token(provider):
    assert provider._access_token(_user("caller-jwt")) == "caller-jwt"


def test_access_token_raises_when_the_caller_has_none(provider):
    """No shared/hardcoded fallback identity exists to converse as instead --
    unlike Mitra's guest flow, a missing token is a hard stop for Saathi."""
    with pytest.raises(ProviderAuthError, match="logged-in ELEVATE user"):
        provider._access_token(_user(None))


def test_two_different_users_resolve_two_different_tokens_on_the_same_instance(provider):
    """THE HEADLINE PROPERTY. One shared provider instance, two different
    logged-in identities, on two different calls."""
    assert provider._access_token(_user("token-a")) == "token-a"
    assert provider._access_token(_user("token-b")) == "token-b"


# ---------------------------------------------------------------------------
# The REST client sends whatever token it was called with -- nothing cached
# ---------------------------------------------------------------------------


@resp_lib.activate
def test_read_profile_sends_the_given_token_as_x_auth_token(provider):
    resp_lib.add(
        resp_lib.GET, "https://saathi.test/api/shikshalokam/read-elevate-profile/",
        json={"profile_details": {"profileid": "42", "has_accepted_tnc": True}},
        status=200,
    )

    provider._rest.read_profile("token-a")

    assert resp_lib.calls[0].request.headers[AUTH_HEADER] == "token-a"


@resp_lib.activate
def test_the_same_client_sends_a_different_users_token_on_the_next_call(provider):
    """No instance-level caching of the credential: back-to-back calls on the
    SAME SaathiRestClient/transport, for two different users, must each carry
    that call's own token -- proving there is nothing left to leak between
    concurrent callers."""
    resp_lib.add(
        resp_lib.GET, "https://saathi.test/api/shikshalokam/read-elevate-profile/",
        json={"profile_details": {"profileid": "1", "has_accepted_tnc": True}},
        status=200,
    )
    resp_lib.add(
        resp_lib.GET, "https://saathi.test/api/shikshalokam/read-elevate-profile/",
        json={"profile_details": {"profileid": "2", "has_accepted_tnc": True}},
        status=200,
    )

    provider._rest.read_profile("token-a")
    provider._rest.read_profile("token-b")

    assert resp_lib.calls[0].request.headers[AUTH_HEADER] == "token-a"
    assert resp_lib.calls[1].request.headers[AUTH_HEADER] == "token-b"


@resp_lib.activate
def test_open_session_uses_the_given_users_token_throughout(provider):
    """open_session's three REST calls (profile, accept-tnc, generate-session)
    all carry the SAME caller's token, resolved once via _access_token."""
    resp_lib.add(
        resp_lib.GET, "https://saathi.test/api/shikshalokam/read-elevate-profile/",
        json={"profile_details": {"profileid": "7", "has_accepted_tnc": False}},
        status=200,
    )
    resp_lib.add(
        resp_lib.PATCH, "https://saathi.test/api/accept-tnc/",
        json={}, status=200,
    )
    resp_lib.add(
        resp_lib.GET, "https://saathi.test/api/generate-session/",
        json={"sessionid": "s-1"}, status=200,
    )

    class _Session:
        remote_session_id = None
        remote_profile_id = None

    init = provider.open_session(remote_spec("saathi"), _Session(), _user("caller-jwt"))

    assert init.remote_session_id == "s-1"
    assert init.remote_profile_id == "7"
    for call in resp_lib.calls:
        assert call.request.headers[AUTH_HEADER] == "caller-jwt"
