"""Profile policy: what a complete profile is, and how one is changed.

Responsible for: deciding which fields are mandatory, and orchestrating the
read/update calls against ELEVATE.
Used by: app/routers/profile.py, via app/dependencies/profile.py.

The split with app/integrations/elevate is deliberate: the integration owns
ELEVATE's WIRE FORMAT, this owns SAARTHI'S POLICY. Raises typed errors and maps
no status codes -- the router does that.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping

from app.integrations.elevate.exceptions import ElevateError

logger = logging.getLogger(__name__)

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


def merge_for_update(
    current: Mapping[str, Any], changes: Mapping[str, Any]
) -> Dict[str, str]:
    """Every mandatory field ELEVATE should still hold after this write.

    THE WHOLE POINT: ELEVATE's `/user/v1/user/update` CLEARS the profile fields
    its body omits. A sparse body -- only what the user edited -- therefore
    wipes the rest, which is the data-loss bug this exists to prevent. So the
    caller's changes are merged over the profile ELEVATE currently holds and
    the COMPLETE set goes upstream every time.

    That is also correct if ELEVATE turns out to merge after all: resending an
    unchanged value is a no-op. Being right under both semantics is deliberate,
    because the upstream contract is not documented anywhere we can check.

    Fields empty on BOTH sides are left out entirely rather than sent as `""`:
    an empty string asks ELEVATE to store a blank, which is not the same as
    leaving an attribute unset, and `_is_filled` would then disagree with what
    is actually stored.
    """
    merged: Dict[str, str] = {}
    for field in MANDATORY_FIELDS:
        value = changes[field] if field in changes else current.get(field)
        if _is_filled(value):
            merged[field] = str(value).strip()
    return merged


def _log_fields_that_did_not_stick(
    sent: Mapping[str, str], refreshed: Mapping[str, Any]
) -> None:
    """Warn when a value we just wrote is not what came back.

    The one signal worth raising here. "Carried a field over" is NOT -- that is
    the normal case for every single-field edit now, so logging it would be
    pure noise.

    This fires if the merge silently breaks, if ELEVATE's semantics shift
    again, and -- the reason it is worth the line -- if the four
    entity-suspect fields reject the bare label strings we send. `to_profile`
    unwraps `userRole`/`userSchool`/`userDistrict`/`profileState` from
    `{value, label}` objects, but `to_update_body` writes plain strings back.
    If those attributes are entity references upstream, our writes may not
    resolve, and the merge above would quietly paper over it forever.

    FIELD NAMES ONLY, NEVER VALUES: these are the user's school and district.
    """
    dropped = [
        field
        for field, value in sent.items()
        if str(refreshed.get(field) or "").strip() != value
    ]
    if dropped:
        logger.warning("profile: ELEVATE did not persist fields=%s", sorted(dropped))


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
        """Read, merge the caller's changes, write the COMPLETE set, re-read.

        THREE CALLS, EACH LOAD-BEARING:

        1. The pre-read is the merge baseline. ELEVATE clears whatever the
           write body omits, so the other four fields have to be resent, and
           only ELEVATE knows their current values. Doing this server-side also
           makes a stale client harmless -- the merge happens against upstream
           truth, not against whatever the browser last loaded.
        2. The write carries all five (see `merge_for_update`).
        3. The re-read is what the caller gets. ELEVATE may normalise a value
           or drop one it cannot resolve, and reporting "complete" for a value
           that did not stick would stop the user ever being asked again.

        KNOWN RACE, ACCEPTED: read-modify-write is not atomic. Two tabs, or a
        retried request, can lose an update -- A reads, B reads, A writes, B
        writes from its stale baseline, A's edit is gone. Closing it needs
        optimistic concurrency (an ETag / `If-Match`) that ELEVATE is not known
        to offer. The window is small and a user rarely races themselves, so it
        is documented rather than solved. It is a real trade: the sparse
        version had no such race, and lost data unconditionally instead.
        """
        current = self._client.read_profile(token)
        merged = merge_for_update(current, fields)
        self._client.update_profile(token, merged)

        try:
            refreshed = self._client.read_profile(token)
        except ElevateError:
            # THE WRITE ALREADY LANDED. Failing here would send the user back
            # to retry a save that succeeded, showing them stale values while
            # they did it. What we just wrote is the honest answer.
            logger.warning(
                "profile: post-write re-read failed; answering from the merged values"
            )
            return {**current, **merged}

        _log_fields_that_did_not_stick(merged, refreshed)
        return refreshed
