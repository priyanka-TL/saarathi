"""Caller identity.

Responsible for: producing a UserContext, either from configuration (the
AUTH_CHECK=false dev identity) or from a per-request bearer token supplied by
a logged-in user.
Used by: built once by the container; `authenticate()` called per request by
the identity dependency, with that request's bearer token if it sent one.

AUTH_CHECK=true MEANS A BEARER TOKEN IS REQUIRED, ALWAYS. There used to be a
second, no-token path here: a boot-provisioned SAARTHI_STATIC_TOKEN, decoded
unverified and served to any caller that sent no Authorization header at all.
That was a deliberate transitional bridge from before the frontend had a real
login flow -- it is gone now that it does (frontend/src/api/http.js attaches
a real per-request token once a user logs in against ELEVATE), because it was
also, unavoidably, an authentication bypass: ANY caller, logged in or not,
got served as a specific real identity for free. A per-request bearer token
is decoded WITH signature and expiry verification, against ELEVATE_JWT_SECRET
(ELEVATE's own ACCESS_TOKEN_SECRET) -- it comes from the request itself,
exactly what an attacker controls, so trusting it unverified would let anyone
forge any user, tenant or role. Without that secret configured, a per-request
token is refused outright rather than trusted unverified.
"""
from __future__ import annotations

from typing import Optional

import jwt

from app.core.settings import Settings
from app.exceptions.domain import InvalidTokenError
from app.domain.core import UserContext, OrgMembership


# InvalidTokenError now lives in app/exceptions/domain.py (imported above).


# ---------------------------------------------------------------------------
# The AUTH_CHECK=false identity.
#
# Mirrors the shape of a real Saarthi token so the rest of the app behaves
# identically with auth off: `shikshalokam` is the tenant every existing row is
# scoped to (conversations.tenant_code), org 62 / `sot` is the active org whose
# roles UserContext.roles resolves, and the role set deliberately excludes
# `admin` -- turning auth off must not open the admin surface.
# ---------------------------------------------------------------------------
DEFAULT_USER_ID = "1355"
DEFAULT_NAME = "Priyanka Pradeep"
DEFAULT_TENANT_CODE = "shikshalokam"
DEFAULT_ORG_ID = "62"
DEFAULT_ORG_CODE = "sot"
DEFAULT_ROLES = ("mentee", "creator", "program_designer")


def context_from_token(token: str, settings: Settings, *, verify: bool = False) -> UserContext:
    """Decode the JWT and produce a UserContext exactly as prescribed.

    `verify=True`: the live, per-request path (see `Authenticator.authenticate`
    below). Signature and expiry ARE checked, against
    `settings.elevate_jwt_secret`. No secret configured means no per-request
    token can be trusted, so this raises rather than silently falling back to
    an unverified decode.

    `verify=False` (the default): decode only, no live caller -- kept so the
    decode/mapping logic itself (TRAP 1, TRAP 3, below) can be exercised
    directly in tests without also needing a signed token.

    Applies TRAP 1 (email derivation) and TRAP 3 (roles from containing org).
    """
    if verify:
        if not settings.elevate_jwt_secret:
            raise InvalidTokenError(
                "ELEVATE_JWT_SECRET is not configured; a per-request bearer "
                "token cannot be verified and is refused rather than trusted "
                "unverified."
            )
        try:
            decoded = jwt.decode(
                token,
                settings.elevate_jwt_secret,
                algorithms=["HS256"],
                options={"verify_signature": True, "verify_exp": True},
            )
        except jwt.InvalidTokenError as e:
            raise InvalidTokenError(f"Invalid or expired token: {e}")
    else:
        try:
            decoded = jwt.decode(
                token,
                options={"verify_signature": False, "verify_exp": False},
            )
        except jwt.InvalidTokenError as e:
            raise InvalidTokenError(f"Invalid token: {e}")

    data = decoded.get("data", {})

    # TRAP 1: email is derived
    identifier = str(data.get(settings.jwt_identifier_field, ""))
    email = identifier + settings.jwt_email_suffix

    # TRAP 3: roles are nested inside organizations[]. The grant scope is the CONTAINING org's id.
    orgs = []
    for org_data in data.get("organizations", []):
        org_id = str(org_data.get("id", ""))
        org_code = str(org_data.get("code", ""))

        # Read roles from the containing org, never role["organization_id"]
        roles = []
        for role_data in org_data.get("roles", []):
            title = role_data.get("title")
            if title:
                roles.append(title)

        orgs.append(OrgMembership(org_id=org_id, org_code=org_code, roles=tuple(roles)))

    # Find active org if there is one? The token might have organization_ids array.
    # The first one is typically the active one if not explicitly specified.
    org_ids = data.get("organization_ids", [])
    active_org_id = str(org_ids[0]) if org_ids else None

    return UserContext(
        user_id=identifier,
        email=email,
        display_name=data.get("name", ""),
        tenant_code=data.get("tenant_code", ""),
        orgs=tuple(orgs),
        active_org_id=active_org_id,
        locale="en", # Default, maybe from header or token if available
        token=token
    )


def default_context(settings: Settings) -> UserContext:
    """The AUTH_CHECK=false identity, built from the DEFAULT_* constants.

    The email is derived through jwt_email_suffix rather than hardcoded: TRAP 1
    says it must match Mitra's SSO derivation, and that has to hold in both
    modes or auth-off runs create a second Mitra profile for the same person.

    token is None -- there is no token in this mode. A non-guest remote_flow
    agent therefore cannot finalize against a remote platform with auth off; the
    provider allowlist
    is 0 by default, so nothing reaches that path.
    """
    return UserContext(
        user_id=DEFAULT_USER_ID,
        email=DEFAULT_USER_ID + settings.jwt_email_suffix,
        display_name=DEFAULT_NAME,
        tenant_code=DEFAULT_TENANT_CODE,
        orgs=(
            OrgMembership(
                org_id=DEFAULT_ORG_ID,
                org_code=DEFAULT_ORG_CODE,
                roles=DEFAULT_ROLES,
            ),
        ),
        active_org_id=DEFAULT_ORG_ID,
        locale="en",
        token=None,
    )


class Authenticator:
    """The source of caller identity for the whole application.

    AUTH_CHECK=false: always the hardcoded dev identity, resolved once here at
    container build time -- a per-request token is never consulted, so dev
    mode stays deterministic regardless of what a request sends.

    AUTH_CHECK=true: `authenticate(token)` requires a per-request bearer
    token, always -- decoded WITH verification (see `context_from_token`).
    No token means no identity to serve: this raises, and
    `get_current_user` (app/dependencies/identity.py) turns that into a 401.
    There is deliberately no fallback identity for a missing token; see the
    module docstring for why one existed briefly and was removed.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._auth_check = settings.auth_check
        self._default = default_context(settings)

    def authenticate(self, token: Optional[str] = None) -> UserContext:
        if not self._auth_check:
            return self._default
        if token:
            return context_from_token(token, self._settings, verify=True)
        raise InvalidTokenError("No bearer token was supplied.")
