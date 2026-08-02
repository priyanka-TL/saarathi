"""Identity is resolved from CONFIGURATION, not from the request.

`get_current_user` (app/dependencies/identity.py) reads nothing off the
request at all -- no `Authorization` header, no cookie, nothing. It always
returns `Authenticator.authenticate()`, which was decided once at container
build time from `.env`. This is deliberate, not an oversight: the frontend is
a bare SPA with no login flow and no way to obtain or send a caller-specific
JWT, so a design that expected one would either 401 every real browser
request (nothing to read) or invent a token source that does not exist.
`AUTH_CHECK` still has its two branches -- decode `SAARTHI_STATIC_TOKEN` vs.
the hardcoded development identity -- but the choice between them is made once
at startup, not per request.

This file used to test the opposite of that (identity varying per caller, via
the bearer token) -- that mechanism existed for one phase of this session's
work before turning out to be undeployable given the frontend's shape, and was
reverted. What is asserted here now is the actual, current contract: nothing
about a request -- present header, absent header, garbage header -- changes
who a request resolves to.

Multi-tenant CONFIGURATION still exists and is still exercised (see
tests/integration/test_admin_capabilities.py, tests/guards/
test_tenant_isolation.py): an admin's `/api/admin/capabilities` calls specify
`tenant_id`/`organization_id` explicitly, not derived from the caller's own
identity. What does not exist is an ordinary end-user request resolving to
more than one tenant.
"""
from __future__ import annotations

import jwt
import pytest
from starlette.testclient import TestClient


def make_token(*, user_id: str, tenant_code: str, roles=("mentee",), org_id="62") -> str:
    """Mint a Saarthi-shaped JWT.

    No longer decoded by anything in the live request path -- kept because
    building one is still occasionally useful for a test that wants to prove a
    header is IGNORED (see below), and because
    app/services/identity.py::context_from_token is still real, tested,
    decode-only logic (see tests/unit/test_identity.py) even though nothing on
    the live HTTP path calls it anymore.

    Signed with a throwaway secret ON PURPOSE: Saarthi is the sole validator
    of a real token; this app was never the one checking a signature.
    """
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
        "iat": 1785242569,
        "exp": 1785847369,
    }
    return jwt.encode(payload, "irrelevant-secret", algorithm="HS256")


# ---------------------------------------------------------------------------
# The actual bug this file exists to prevent a regression of
# ---------------------------------------------------------------------------


def test_a_request_with_no_credential_at_all_succeeds(anonymous_client):
    """THE headline behaviour. The frontend sends no Authorization header on
    any request, ever -- it has no login flow. If this 401s, the whole app is
    unusable from the browser, which is exactly what happened when identity
    was briefly made per-request without a frontend that could supply one."""
    response = anonymous_client.get("/api/conversations")
    assert response.status_code == 200


def test_a_request_with_no_credential_gets_the_configured_identity(anonymous_client, client):
    """Not just "succeeds" -- succeeds AS the same identity a credentialed
    request gets, because both paths resolve identically now."""
    created = client.post("/api/reset", json={})
    assert created.status_code == 200
    conversation_id = created.json()["conversation_id"]

    # Visible to the "anonymous" client too: there is only one identity.
    response = anonymous_client.get(f"/api/conversations/{conversation_id}/messages")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Nothing about the request changes the outcome
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        None,
        "Bearer not-a-real-jwt",
        "Basic dXNlcjpwYXNz",
        "",
    ],
    ids=["absent", "well-formed-but-fake-jwt", "wrong-scheme", "empty"],
)
def test_no_header_value_is_examined_at_all(api_app, header):
    """A present-but-garbage `Authorization` header does not even reach a
    decoder -- it is never read. All four cases resolve identically."""
    headers = {"Authorization": header} if header is not None else {}
    client = TestClient(api_app, raise_server_exceptions=False, headers=headers)
    assert client.get("/api/conversations").status_code == 200


def test_a_real_bearer_token_for_a_different_tenant_changes_nothing(api_app):
    """The strongest version of the point: even a WELL-FORMED token naming a
    different tenant is inert, because nothing reads it. Two requests --
    identical except for this header -- resolve to the same identity."""
    plain = TestClient(api_app, raise_server_exceptions=False)
    with_token = TestClient(
        api_app, raise_server_exceptions=False,
        headers={"Authorization": f"Bearer {make_token(user_id='1', tenant_code='someone_else')}"},
    )

    plain_ids = {c["id"] for c in plain.get("/api/conversations").json()["conversations"]}
    token_ids = {
        c["id"] for c in with_token.get("/api/conversations").json()["conversations"]
    }
    assert plain_ids == token_ids


# ---------------------------------------------------------------------------
# AUTH_CHECK's two branches -- chosen once, at startup, not per request
# ---------------------------------------------------------------------------


def test_auth_check_true_serves_the_decoded_static_token_identity(api_app):
    assert api_app.state.container.settings.auth_check is True
    identity = api_app.state.authenticator.authenticate()
    # Matches SAARTHI_STATIC_TOKEN in the test environment's .env-derived
    # settings -- see tests/conftest.py for why that value is deterministic.
    assert identity.tenant_code
    assert identity.user_id


def test_auth_check_false_serves_the_development_identity(api_app, anonymous_client, monkeypatch):
    from app.services.identity import DEFAULT_TENANT_CODE, DEFAULT_USER_ID, default_context

    monkeypatch.setattr(api_app.state.container.settings, "auth_check", False)
    monkeypatch.setattr(
        api_app.state.authenticator, "_user",
        default_context(api_app.state.container.settings),
    )

    identity = api_app.state.authenticator.authenticate()
    assert identity.tenant_code == DEFAULT_TENANT_CODE
    assert identity.user_id == DEFAULT_USER_ID
    assert anonymous_client.get("/api/conversations").status_code == 200


def test_authenticate_returns_the_same_cached_identity_every_call(api_app):
    """Resolved once at construction, not re-decoded per call -- there is no
    per-request work happening here at all."""
    authenticator = api_app.state.authenticator
    assert authenticator.authenticate() is authenticator.authenticate()
