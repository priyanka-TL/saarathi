"""The ELEVATE user-service client.

Responsible for: reading and updating a user's profile over one HTTP session.
Used by: ProfileService, built once by the container when ELEVATE_BASE_URL is set.

Ported from Mitra's `chatbot/utils/elevate/profile_utils.py`
(`fetch_elevate_user` / `update_elevate_profile`), minus its Django `Profile`
write -- ELEVATE is the source of truth and Saarthi caches nothing.
`requests` exceptions never escape this package; everything becomes an
ElevateError subtype.

THE TOKEN IS A PER-CALL ARGUMENT, NEVER TRANSPORT STATE. One client instance is
shared by every request in the process, so a token stored on `self` would serve
one user's session to whoever called next. app/providers/saathi/rest.py states
the same rule for the same reason.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

import requests

from app.integrations.elevate.exceptions import (
    ElevateRejected,
    ElevateTimeout,
    ElevateUnauthorized,
    ElevateUpstreamError,
)
from app.integrations.elevate.mapping import to_profile, to_update_body

logger = logging.getLogger(__name__)

# ELEVATE authenticates with its own header, NOT `Authorization: Bearer`. The
# value is the same JWT the caller sent us; only the header name differs.
AUTH_HEADER = "X-auth-token"

READ_PATH = "/user/v1/user/read"
UPDATE_PATH = "/user/v1/user/update"

# Cap on how much of ELEVATE's error message is quoted into an exception.
_DETAIL_LIMIT = 200


# The four attributes ELEVATE may model as entity references. We READ them as
# `{value, label}` objects but WRITE bare label strings back -- see
# _log_attribute_shapes.
_ENTITY_SUSPECT_KEYS = ("userRole", "userSchool", "userDistrict", "profileState")


def _log_attribute_shapes(result: Dict[str, Any]) -> None:
    """Record the JSON TYPE of the entity-suspect attributes. Never the values.

    Evidence-gathering for an open question, not diagnostics. `to_profile`
    unwraps these four from `{value, label}` objects, but `to_update_body`
    sends plain strings back. If ELEVATE models them as entity references, a
    bare label may not resolve and our writes would not stick -- a bug distinct
    from the sparse-body data loss, and one that read-merge-write would hide.

    DEBUG, so normal operation stays quiet: raise LOG_LEVEL when you want to
    settle it. If these consistently come back `dict` while we send `str`, the
    write path needs entity resolution.
    """
    if not logger.isEnabledFor(logging.DEBUG):
        return
    shapes = " ".join(
        f"{key}={type(result.get(key)).__name__}" for key in _ENTITY_SUSPECT_KEYS
    )
    logger.debug("elevate read: attribute shapes %s", shapes)


class ElevateUserClient:
    """Profile read/update against ELEVATE's user service."""

    def __init__(
        self,
        *,
        base_url: str,
        # NO DEFAULTS on the timeouts, matching BhashiniClient: each has a
        # Settings field behind it and build_container passes both. A default
        # here would be a second declaration of the same number, free to drift
        # from the one operators actually tune.
        connect_timeout: float,
        read_timeout: float,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout: Tuple[float, float] = (connect_timeout, read_timeout)
        self._session = requests.Session()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"<ElevateUserClient base_url={self.base_url}>"

    # ---- public API -------------------------------------------------------

    def read_profile(self, token: str) -> Dict[str, Any]:
        """The caller's profile, in Saarthi's field names."""
        result = self._request("GET", READ_PATH, token, task="read")
        _log_attribute_shapes(result)
        profile = to_profile(result)
        if not profile.get("user_id"):
            # The original returns a bare `{}` here, which its callers cannot
            # tell apart from "this user has filled nothing in".
            raise ElevateUpstreamError("read", detail="response carried no user id")
        return profile

    def update_profile(self, token: str, fields: Dict[str, Any]) -> None:
        """Write the given fields. Callers re-read rather than trusting this.

        Returns nothing on purpose. ELEVATE's PATCH envelope is not pinned, and
        a free-text `userRole` may be normalised or dropped if the tenant has
        it configured as an entity reference -- so the only way to know what
        actually stuck is to read it back.
        """
        self._request("PATCH", UPDATE_PATH, token, task="update", json=to_update_body(fields))

    # ---- transport --------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        token: str,
        *,
        task: str,
        json: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            response = self._session.request(
                method,
                url,
                headers={AUTH_HEADER: token},
                json=json,
                timeout=self._timeout,
                # REDIRECTS ARE A CREDENTIAL LEAK HERE. `requests` re-sends
                # headers across a redirect, and this header is the user's live
                # session token -- one http->https or trailing-slash
                # misconfiguration of ELEVATE_BASE_URL would hand it to
                # whatever answered. The original follows redirects; this does
                # not, and treats a 3xx as an upstream fault.
                allow_redirects=False,
            )
        except requests.exceptions.Timeout:
            raise ElevateTimeout(task, self._timeout[1])
        except requests.exceptions.RequestException as exc:
            # The URL is deliberately absent from the message: it is built from
            # operator config and has ended up in user-visible errors before.
            raise ElevateUpstreamError(task, detail=type(exc).__name__)

        # Status is logged, the body never is -- `/user/v1/user/read` answers
        # with the user's entire profile. The original logs `response.text` on
        # update at INFO level.
        logger.info("elevate %s: HTTP %s", task, response.status_code)

        if response.status_code == 401:
            raise ElevateUnauthorized("ELEVATE rejected the caller's token")
        if response.status_code in (400, 422):
            raise ElevateRejected(self._detail(response))
        if response.status_code >= 300:
            raise ElevateUpstreamError(task, response.status_code, self._detail(response))

        try:
            payload = response.json()
        except ValueError:
            raise ElevateUpstreamError(task, response.status_code, "body was not JSON")
        if not isinstance(payload, dict):
            raise ElevateUpstreamError(task, response.status_code, "body was not an object")

        # ELEVATE can answer 200 with a failure envelope, so the status alone
        # is not success. Case-insensitive, as in the original.
        if str(payload.get("responseCode") or "").lower() != "ok":
            raise ElevateUpstreamError(
                task, response.status_code, "envelope responseCode was not 'ok'"
            )

        result = payload.get("result")
        return result if isinstance(result, dict) else {}

    @staticmethod
    def _detail(response: requests.Response) -> str:
        """ELEVATE's own message, if it sent one -- never the whole body."""
        try:
            payload = response.json()
        except ValueError:
            return ""
        if not isinstance(payload, dict):
            return ""
        for key in ("message", "error_message", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value[:_DETAIL_LIMIT]
        return ""
