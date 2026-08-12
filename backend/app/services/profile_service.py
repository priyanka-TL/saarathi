"""Profile policy: what a complete profile is, and how one is changed.

Responsible for: deciding which fields are mandatory, and orchestrating the
read/update calls against ELEVATE.
Used by: app/routers/profile.py, via app/dependencies/profile.py.

The split with app/integrations/elevate is deliberate: the integration owns
ELEVATE's WIRE FORMAT, this owns SAARTHI'S POLICY. Raises typed errors and maps
no status codes -- the router does that.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping

# The five fields the profile-completion popup exists to collect. Taken from
# Mitra's `submit_user_context` tool, which writes exactly these
# (chatbot/services/response_handlers/common_handler.py::_save_submitted_user_context).
#
# DECIDED HERE, ONCE, AND RETURNED TO THE CLIENT. Mitra instead stores a local
# `is_onboarding_completed` boolean, set unconditionally whenever the LLM fires
# that tool -- with no check that any of the five are actually non-empty, and
# no rollback if the ELEVATE write then fails. Deriving completeness from the
# live values each time means there is no flag that can drift from reality.
MANDATORY_FIELDS = ("name", "role", "school_name", "district", "state")

# Anything longer is a paste accident, not a school name. Bounds what is sent
# upstream and what a stored value can cost to render.
MAX_FIELD_LENGTH = 200


def _is_filled(value: Any) -> bool:
    """Whether a profile field counts as answered.

    BLANK IS NOT ANSWERED. ELEVATE represents an unset attribute as `null`,
    `''`, and `{"label": ""}` in different places, and a user can type a space.
    Without the strip() a single space would mark someone permanently complete
    and they would never be asked again.
    """
    return bool(value is not None and str(value).strip())


def missing_fields(profile: Mapping[str, Any]) -> List[str]:
    """The mandatory fields this profile has not filled in, in a stable order."""
    return [field for field in MANDATORY_FIELDS if not _is_filled(profile.get(field))]


def is_profile_complete(profile: Mapping[str, Any]) -> bool:
    return not missing_fields(profile)


def serialize(profile: Mapping[str, Any]) -> Dict[str, Any]:
    """The response body for both routes -- one shape, so the client has one path."""
    return {
        "profile": dict(profile),
        "is_complete": is_profile_complete(profile),
        "missing_fields": missing_fields(profile),
    }


class InvalidProfileUpdate(ValueError):
    """Raised when the submitted body cannot be turned into an update."""


def clean_update(body: Any) -> Dict[str, str]:
    """Validate a PATCH body into the fields to write. Raises InvalidProfileUpdate.

    Unknown keys are ignored rather than rejected, so a client sending a field
    this version does not know about still gets its other changes applied.

    BLANK VALUES ARE REJECTED, NOT FORWARDED. `to_update_body` sends every key
    it is given, so passing `""` through would erase a good value in ELEVATE --
    the exact opposite of what a user clearing a field by accident intends.
    Clearing a field is not a supported operation; there is no UI for it.
    """
    if not isinstance(body, dict):
        raise InvalidProfileUpdate("body must be a JSON object")

    fields: Dict[str, str] = {}
    for field in MANDATORY_FIELDS:
        if field not in body:
            continue
        value = body[field]
        if not isinstance(value, str):
            raise InvalidProfileUpdate(f"{field} must be a string")
        value = value.strip()
        if not value:
            raise InvalidProfileUpdate(f"{field} must not be blank")
        if len(value) > MAX_FIELD_LENGTH:
            raise InvalidProfileUpdate(f"{field} must be at most {MAX_FIELD_LENGTH} characters")
        fields[field] = value

    if not fields:
        raise InvalidProfileUpdate(
            "supply at least one of: " + ", ".join(MANDATORY_FIELDS)
        )
    return fields


class ProfileService:
    """Reads and updates the caller's ELEVATE profile."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @property
    def available(self) -> bool:
        """False when ELEVATE_BASE_URL is unset, which the router turns into 503.

        Built even when unconfigured, so handlers ask rather than check for
        None -- the same choice app/dependencies/voice.py documents.
        """
        return self._client is not None

    def read(self, token: str) -> Dict[str, Any]:
        return self._client.read_profile(token)

    def update(self, token: str, fields: Dict[str, str]) -> Dict[str, Any]:
        """Write the fields, then RE-READ and return what ELEVATE actually stored.

        The second call is not redundant. ELEVATE may normalise a value or drop
        one it cannot resolve, and the client's whole purpose is to tell the UI
        the truth -- reporting "complete" for a value that did not stick would
        stop the user being asked again for a field that is still empty.
        """
        self._client.update_profile(token, fields)
        return self._client.read_profile(token)
