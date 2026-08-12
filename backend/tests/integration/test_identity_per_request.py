"""Identity is resolved per request from the Authorization header, and a
missing or invalid one is UNAUTHORIZED -- there is no fallback identity.

`get_current_user` (app/dependencies/identity.py) reads the request's
`Authorization: Bearer <token>` header. When present and it VERIFIES (against
ELEVATE_JWT_SECRET -- see app/services/identity.py::context_from_token), the
UserContext comes from that token: their real tenant, org, roles. When
present but INVALID -- bad signature, expired, or no ELEVATE_JWT_SECRET
configured at all to check it against -- the request 401s. When ABSENT, the
request ALSO 401s (when AUTH_CHECK=true): falling back to a different,
unrelated identity would be worse than rejecting a call that carries no
credential at all, and doing so was exactly the authentication bypass this
project's own review found -- any caller, logged in or not, was served a
real identity for free via a boot-provisioned SAARTHI_STATIC_TOKEN. That
fallback is gone; this file used to pin it deliberately (see prior history)
and now pins its removal instead.

Multi-tenant CONFIGURATION still exists independently (see
tests/integration/test_admin_capabilities.py, tests/guards/
test_tenant_isolation.py): an admin's /api/admin/capabilities calls specify
tenant_id/organization_id explicitly, not derived from the caller. What's new
here is that an ORDINARY end-user request can now resolve to more than one
identity too, depending on who is logged in -- or to none at all, if no one
is.
"""
from __future__ import annotations

import time

import jwt
import pytest
from starlette.testclient import TestClient

#: Matches what `verified_app` configures as ELEVATE_JWT_SECRET -- a token
#: minted with this by default actually verifies, so a test proving forgery is
#: rejected has to pass a different secret explicitly. (The whole suite now
#: has a secret configured globally too -- see tests/conftest.py -- but
#: `verified_app` still patches it explicitly so this file works even if that
#: default ever changes.)
TEST_SECRET = "test-elevate-jwt-secret"


def make_token(
    *,
    user_id: str,
    tenant_code: str,
    roles=("mentee",),
    org_id: str = "62",
    secret: str = TEST_SECRET,
    exp_offset_s: int = 3600,
) -> str:
    """Mint a Saarthi-shaped JWT, signed with `secret` and expiring in
    `exp_offset_s` seconds (negative => already expired)."""
    payload = {
        "data": {
            "id": user_id,
            "name": f"User {user_id}",
            "tenant_code": tenant_code,
            "organization_ids": [org_id],
            "organizations": [
                {
                    "id": int(org_id),
                    "code": "sot",
                    "roles": [{"title": r} for r in roles],
                }
            ],
        },
        "iat": int(time.time()),
        "exp": int(time.time()) + exp_offset_s,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


@pytest.fixture()
def verified_app(api_app, monkeypatch):
    """`api_app`, with ELEVATE_JWT_SECRET configured so a bearer token minted
    with TEST_SECRET actually verifies.
    """
    monkeypatch.setattr(api_app.state.container.settings, "elevate_jwt_secret", TEST_SECRET)
    return api_app


def _bearer_client(app, token: str | None = None) -> TestClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return TestClient(app, raise_server_exceptions=False, headers=headers)


# ---------------------------------------------------------------------------
# No credential at all: UNAUTHORIZED. This is the headline behaviour.
# ---------------------------------------------------------------------------


def test_a_request_with_no_credential_at_all_401s(anonymous_client):
    """No fallback identity for a missing token -- this used to be a 200 via
    SAARTHI_STATIC_TOKEN, which was exactly the authentication bypass this
    file now exists to keep closed."""
    response = anonymous_client.get("/api/conversations")
    assert response.status_code == 401


def test_a_logged_in_users_activity_is_invisible_to_an_anonymous_caller(anonymous_client, client):
    """The contrast that matters: `client` (a real, verified bearer token) can
    create and read its own data; `anonymous_client` (no token) cannot reach
    ANY of it -- not "sees nothing," but 401, before the question of whose
    data it is even comes up."""
    created = client.post("/api/reset", json={})
    assert created.status_code == 200
    conversation_id = created.json()["conversation_id"]

    response = anonymous_client.get(f"/api/conversations/{conversation_id}/messages")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# A present-but-invalid credential: rejected, never silently downgraded
# ---------------------------------------------------------------------------


def test_a_garbage_bearer_token_401s(api_app):
    client = _bearer_client(api_app, "not-a-real-jwt")
    assert client.get("/api/conversations").status_code == 401


def test_a_well_formed_token_401s_when_no_secret_is_configured(api_app, monkeypatch):
    """Models a deployment that hasn't set ELEVATE_JWT_SECRET yet -- even a
    perfectly well-formed, correctly-shaped token cannot be trusted and is
    refused. (The suite configures a secret globally by default -- see
    tests/conftest.py -- so this test explicitly blanks it for itself.)"""
    monkeypatch.setattr(api_app.state.container.settings, "elevate_jwt_secret", "")
    token = make_token(user_id="1", tenant_code="someone_else")
    client = _bearer_client(api_app, token)
    assert client.get("/api/conversations").status_code == 401


def test_a_forged_signature_401s_even_with_a_secret_configured(verified_app):
    forged = make_token(user_id="1", tenant_code="attacker", secret="wrong-secret")
    assert _bearer_client(verified_app, forged).get("/api/conversations").status_code == 401


def test_an_expired_bearer_token_401s_even_with_a_secret_configured(verified_app):
    expired = make_token(user_id="1", tenant_code="t", exp_offset_s=-3600)
    assert _bearer_client(verified_app, expired).get("/api/conversations").status_code == 401


@pytest.mark.parametrize(
    "header",
    ["Basic dXNlcjpwYXNz", ""],
    ids=["wrong-scheme", "empty"],
)
def test_a_non_bearer_or_empty_header_401s_same_as_no_header(verified_app, header):
    """Not every header is a credential attempt -- one that isn't even shaped
    like a bearer token is treated the same as no header at all, which is
    401 (no fallback), same as a genuinely absent header."""
    client = TestClient(
        verified_app, raise_server_exceptions=False, headers={"Authorization": header},
    )
    assert client.get("/api/conversations").status_code == 401


# ---------------------------------------------------------------------------
# A verified bearer token: the real per-request identity
# ---------------------------------------------------------------------------


def test_a_verified_bearer_token_is_accepted(verified_app):
    token = make_token(user_id="111", tenant_code="tenant-a")
    assert _bearer_client(verified_app, token).get("/api/conversations").status_code == 200


def test_two_different_verified_tokens_both_resolve_independently(verified_app):
    """The headline behaviour this feature exists to deliver: the SAME running
    process serves two different logged-in identities on two requests."""
    token_a = make_token(user_id="111", tenant_code="tenant-a")
    token_b = make_token(user_id="222", tenant_code="tenant-b")

    resp_a = _bearer_client(verified_app, token_a).get("/api/conversations")
    resp_b = _bearer_client(verified_app, token_b).get("/api/conversations")

    assert resp_a.status_code == 200
    assert resp_b.status_code == 200


# ---------------------------------------------------------------------------
# Authenticator's own behaviour, below the HTTP layer
# ---------------------------------------------------------------------------


def test_authenticate_with_no_token_raises_every_time(api_app, monkeypatch):
    """No caching, no fallback: every argument-less call raises afresh. (There
    used to be a cached fallback identity here -- see this file's own module
    docstring for why it's gone.)"""
    monkeypatch.setattr(api_app.state.container.settings, "elevate_jwt_secret", "")
    authenticator = api_app.state.authenticator

    from app.exceptions.domain import InvalidTokenError

    with pytest.raises(InvalidTokenError):
        authenticator.authenticate()
    with pytest.raises(InvalidTokenError):
        authenticator.authenticate()


def test_auth_check_false_serves_the_development_identity_regardless_of_any_token(
    api_app, monkeypatch
):
    from app.services.identity import DEFAULT_TENANT_CODE, DEFAULT_USER_ID, default_context

    monkeypatch.setattr(api_app.state.container.settings, "auth_check", False)
    monkeypatch.setattr(
        api_app.state.authenticator, "_default",
        default_context(api_app.state.container.settings),
    )
    monkeypatch.setattr(api_app.state.authenticator, "_auth_check", False)

    identity = api_app.state.authenticator.authenticate()
    assert identity.tenant_code == DEFAULT_TENANT_CODE
    assert identity.user_id == DEFAULT_USER_ID

    # Even a well-formed, otherwise-valid bearer token is ignored in this mode.
    token = make_token(user_id="999", tenant_code="someone_else")
    client = _bearer_client(api_app, token)
    assert client.get("/api/conversations").status_code == 200
