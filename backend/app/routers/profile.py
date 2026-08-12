"""The caller's own profile -- read it, and update it.

Responsible for: validating profile requests and mapping ELEVATE failures to
status codes. The policy lives in app/services/profile_service.py, the wire
format in app/integrations/elevate/.
Used by: the SPA's Profile section, and the profile-completion dialog it shows
after login when mandatory fields are missing.

    GET   /api/profile  -> {profile, is_complete, missing_fields}
    PATCH /api/profile  -> the same shape, re-read from ELEVATE after the write

ONE SHAPE FROM BOTH ROUTES, so the client has a single path for "here is the
current profile" and never has to merge a PATCH echo into what it already held.

THE CALLER'S OWN TOKEN IS THE CREDENTIAL. Nothing here takes a user id: the
identity dependency has already verified the JWT's signature, and that same JWT
is what ELEVATE is asked with -- so a caller can only ever read or write their
own profile, and there is no id parameter to tamper with.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.dependencies.body import json_body_strict
from app.dependencies.identity import get_current_user
from app.dependencies.profile import get_profile_service
from app.domain.core import UserContext
from app.exceptions.envelope import error_response
from app.integrations.elevate import (
    ElevateRejected,
    ElevateTimeout,
    ElevateUnauthorized,
    ElevateUpstreamError,
)
from app.services.profile_service import (
    InvalidProfileUpdate,
    ProfileService,
    clean_update,
    serialize,
)
from app.utils.responses import json_response

logger = logging.getLogger(__name__)

router = APIRouter(tags=["profile"])


def _unavailable() -> JSONResponse:
    """503, for a deployment with no ELEVATE user service configured.

    Mirrors VOICE_DISABLED: "this deployment does not offer the feature" is not
    an error the caller can act on, and the frontend reads it as "hide the
    Profile section" rather than showing a failure.
    """
    return error_response("Profile is not available.", "PROFILE_UNAVAILABLE", 503)


def _token_or_none(user: UserContext) -> Optional[str]:
    """The caller's ELEVATE token, or None when there isn't one.

    None happens for real, not just in theory: with AUTH_CHECK=false the dev
    identity is synthesised from config and carries `token=None`
    (app/services/identity.py::default_context). Passing that to the client
    would put a None into a request header and raise, surfacing as a 500 --
    so the routes below treat it as "not configured for this caller" instead.
    """
    return user.token or None


def _handle_elevate_error(exc: Exception) -> JSONResponse:
    """The one place ELEVATE failures become status codes.

    ONLY ElevateUnauthorized MAY BECOME A 401. The frontend clears the stored
    session on any 401 (src/api/http.js), which is correct when ELEVATE says
    the token is dead -- our own verification already passed, so the session
    ended upstream -- and a spurious logout for every other failure.
    """
    if isinstance(exc, ElevateUnauthorized):
        return error_response("Your session has expired.", "UNAUTHORIZED", 401)
    if isinstance(exc, ElevateRejected):
        # Recoverable by editing the form, so 400 rather than "retry later".
        # Free-text role and school names are the likely cause: a tenant may
        # have them configured as entity references with a fixed value list.
        detail = exc.detail or "Those profile details were not accepted."
        return error_response(detail, "PROFILE_REJECTED", 400)
    if isinstance(exc, ElevateTimeout):
        return error_response(str(exc), "UPSTREAM_TIMEOUT", 504)
    logger.warning("profile: elevate call failed (%s)", exc)
    return error_response("Could not reach the profile service.", "PROFILE_UPSTREAM_ERROR", 502)


@router.get("/api/profile", response_model=None)
def get_profile(
    user: UserContext = Depends(get_current_user),
    profiles: ProfileService = Depends(get_profile_service),
) -> JSONResponse:
    """The caller's profile, plus whether it is complete and what is missing.

    A plain `def`, like every route here -- `tests/guards/test_sync_contract.py`
    pins that, and an outbound HTTP call is blocking work that belongs in a
    worker thread.

    `is_complete` and `missing_fields` are computed server-side on purpose. The
    mandatory-field list is policy; duplicated in JS it would drift the first
    time a sixth field is added, and the popup would then disagree with the API
    about whether it should be showing.
    """
    if not profiles.available:
        return _unavailable()
    token = _token_or_none(user)
    if token is None:
        return _unavailable()

    try:
        profile = profiles.read(token)
    except Exception as exc:  # noqa: BLE001 -- narrowed inside the mapper
        return _handle_elevate_error(exc)

    return json_response(serialize(profile))


@router.patch("/api/profile", response_model=None)
def update_profile(
    data: Optional[Dict[str, Any]] = Depends(json_body_strict),
    user: UserContext = Depends(get_current_user),
    profiles: ProfileService = Depends(get_profile_service),
) -> JSONResponse:
    """Update one or more profile fields, then return the refreshed profile.

    PATCH, not PUT: the body is sparse, which is also how ELEVATE's own endpoint
    behaves, so a form that edits one field cannot blank the other four.

    The response is a fresh READ, not an echo of the request -- see
    ProfileService.update for why trusting the write would let the UI report a
    field as filled when ELEVATE silently dropped it.
    """
    if not profiles.available:
        return _unavailable()
    token = _token_or_none(user)
    if token is None:
        return _unavailable()

    try:
        fields = clean_update(data)
    except InvalidProfileUpdate as exc:
        return error_response(str(exc), "INVALID_REQUEST", 400)

    try:
        profile = profiles.update(token, fields)
    except Exception as exc:  # noqa: BLE001 -- narrowed inside the mapper
        return _handle_elevate_error(exc)

    # Field NAMES only. The values are the user's own profile data and this line
    # would otherwise put their school and district in every request log.
    logger.info("profile updated: fields=%s", sorted(fields))
    return json_response(serialize(profile))
