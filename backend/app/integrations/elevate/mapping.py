"""ELEVATE's profile wire format, translated to Saarthi's own field names.

Responsible for: unwrapping ELEVATE's `result` object into a flat dict, and
building the sparse PATCH body that writes it back.
Used by: ElevateUserClient. Pure functions, no HTTP -- so the unwrapping rules
below are unit-testable without stubbing a transport.

WHY THIS FILE EXISTS SEPARATELY: ELEVATE's names (`userRole`, `userSchool`,
`profileState`) stop here. Letting them reach the router would make the
frontend a second place that knows ELEVATE's schema, which is the whole point
of having an integration layer.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# Saarthi's name -> ELEVATE's name. Read and write use the SAME map, so the two
# directions cannot drift -- the Django original spells each pair out twice,
# once in fetch_elevate_user and once in update_elevate_profile.
FIELD_TO_ELEVATE_KEY = {
    "name": "name",
    "role": "userRole",
    "school_name": "userSchool",
    "district": "userDistrict",
    "state": "profileState",
}


def _text_of(raw: Any, key: str) -> Optional[str]:
    """The human-readable text of an ELEVATE attribute, whatever shape it took.

    ELEVATE sends the SAME attribute as a `{value, label}` dict, a bare string,
    a list of either, or null, depending on how the tenant configured it. Every
    branch here is a shape that has actually been observed, so none of it is
    defensive padding.

    THE ORIGINAL GETS THIS HALF-RIGHT AND IT IS A LATENT 500. In
    `fetch_elevate_user`, `userRole` and `userSchool` branch on type, but
    `profileState` and `userDistrict` do `(user_data.get(...) or {}).get('label')`
    -- so the moment either arrives as a plain string, `.get` raises
    AttributeError on a str and the whole read fails. All four go through this
    one function precisely so that asymmetry cannot come back.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw or None
    if isinstance(raw, dict):
        text = raw.get(key)
        return str(text) if text not in (None, "") else None
    if isinstance(raw, (list, tuple)):
        # Multi-select attributes. First entry wins: the profile form is
        # single-valued, and silently joining them would write a composite
        # string back on the next save.
        for item in raw:
            text = _text_of(item, key)
            if text:
                return text
        return None
    return str(raw) or None


def label_of(raw: Any) -> Optional[str]:
    """The display label of an ELEVATE attribute (`userRole`, `userSchool`, ...)."""
    return _text_of(raw, "label")


def value_of(raw: Any) -> Optional[str]:
    """The stored value of an ELEVATE attribute (`preferred_language`)."""
    return _text_of(raw, "value")


def to_profile(result: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten ELEVATE's `result` object into Saarthi's profile shape.

    Ported from `fetch_elevate_user`, minus its DB write -- ELEVATE is the
    source of truth here and nothing is cached locally, so there is no row to
    keep in step.
    """
    return {
        "user_id": str(result.get("id") or "") or None,
        "name": _text_of(result.get("name"), "label"),
        "role": label_of(result.get("userRole")),
        "school_name": label_of(result.get("userSchool")),
        "district": label_of(result.get("userDistrict")),
        "state": label_of(result.get("profileState")),
        # `.value` not `.label`, and 'en' when absent -- this drives which
        # language the assistant replies in, so it cannot be null.
        "preferred_language": value_of(result.get("preferred_language")) or "en",
        "has_accepted_tnc": bool(result.get("has_accepted_terms_and_conditions", False)),
    }


def to_update_body(fields: Dict[str, Any]) -> Dict[str, Any]:
    """The sparse PATCH body for the fields actually being changed.

    SPARSE IS THE POINT: only keys present in `fields` are sent, so updating
    one field never blanks the four the form did not touch.

    The original seeds this body with `{'about': 'please get hardcode the
    about'}` and its own comment says "hardcoded for now". That is not ported:
    `about` is the user's real bio, and sending a placeholder would overwrite
    it on every single save.
    """
    body: Dict[str, Any] = {}
    for field, elevate_key in FIELD_TO_ELEVATE_KEY.items():
        if field in fields:
            body[elevate_key] = fields[field]
    return body
