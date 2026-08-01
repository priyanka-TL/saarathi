"""Caller identity, resolved once at startup by a single Authenticator.

AUTH_CHECK is the only switch:

  AUTH_CHECK=true   The token comes from the environment (SAARTHI_STATIC_TOKEN)
                    and is DECODED, not validated -- no signature check, no
                    expiry check. Saarthi has already validated it upstream and
                    is the only component allowed to; re-validating here can
                    only reject a request Saarthi already accepted.

  AUTH_CHECK=false  No token is read at all. The Authenticator returns the
                    hardcoded identity below.

Both branches collapse into one immutable UserContext in __init__, so every
caller downstream -- routes, repositories, AccessSpec, the admin gate -- is
unaware of which mode is active and no auth logic exists anywhere else.

This module stays free of any web framework (the .importlinter contracts forbid
app.services importing fastapi/starlette). Nothing here reads a request: the
identity does not depend on one.
"""
from __future__ import annotations

import jwt

from app.core.settings import Settings
from app.domain.core import UserContext, OrgMembership


class InvalidTokenError(Exception):
    """The env token could not be decoded at all (malformed / not a JWT)."""


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


def context_from_token(token: str, settings: Settings) -> UserContext:
    """Decode the JWT and produce a UserContext exactly as prescribed.

    DECODE ONLY. verify_signature and verify_exp are both off, unconditionally:
    validation is Saarthi's job (see the module docstring). The only failure
    mode left is a token that is not decodable at all.

    Applies TRAP 1 (email derivation) and TRAP 3 (roles from containing org).
    """
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
    agent therefore cannot finalize against Mitra with auth off; MITRA_ENABLED
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
    """The single source of caller identity for the whole application.

    Resolution happens once, here, at container build time -- before the server
    accepts traffic. An undecodable SAARTHI_STATIC_TOKEN is therefore a startup
    failure rather than a per-request 401, which is the right shape for a
    misconfiguration: it cannot reach a user.
    """

    def __init__(self, settings: Settings) -> None:
        if settings.auth_check:
            token = settings.saarthi_static_token
            if not token:
                raise ValueError("SAARTHI_STATIC_TOKEN is required when AUTH_CHECK is true")
            self._user = context_from_token(token, settings)
        else:
            self._user = default_context(settings)

    def authenticate(self) -> UserContext:
        return self._user
