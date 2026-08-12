"""`.env.example` documents every setting, and names them correctly.

WHY THIS EXISTS. `Settings` sets `extra="ignore"`, so an undocumented key is
invisible in both directions: an operator cannot discover it, and a MISSPELLED
one in a real `.env` is accepted and silently does nothing. The file drifted
fourteen fields behind before anyone noticed, and the way it was noticed was a
feature failing to switch on with no error anywhere.

The file is also the operator's manual, so this is a low bar on purpose: it
checks that every field appears, not that the prose around it is any good.
"""
from __future__ import annotations

import pathlib
import re

from app.core.settings import Settings

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]
ENV_EXAMPLE = BACKEND_ROOT / ".env.example"

#: `KEY=` at the start of a line, optionally commented out. A commented key is
#: still documentation -- it says "this exists and here is its default".
_KEY = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=")


def _documented() -> set[str]:
    keys = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        match = _KEY.match(line.strip())
        if match:
            keys.add(match.group(1).lower())
    return keys


def test_every_setting_is_documented() -> None:
    missing = {name.lower() for name in Settings.model_fields} - _documented()
    assert not missing, (
        ".env.example does not document these Settings fields: "
        f"{sorted(missing)}. extra='ignore' means nothing will ever warn an "
        "operator that they exist."
    )


def test_no_documented_key_is_a_typo() -> None:
    """The other direction, and the one with teeth.

    A key in the example that matches no field is either retired or misspelled,
    and both look identical to an operator who copies it into a real `.env`:
    the value is accepted and does nothing. Retired keys belong in the
    "NOT HERE ANY MORE" block as prose, not as `KEY=value` lines.
    """
    fields = {name.lower() for name in Settings.model_fields}
    # Credentials are named BY a config row rather than declared as fields, so
    # they are legitimately in the example without being in Settings. Listed
    # explicitly rather than pattern-matched, so adding one is a deliberate act.
    named_by_config = {
        "mitra_origin_url",
        "saathi_origin_url",
        "saathi_email",
        "saathi_password",
        "saathi_access_token",
    }

    unknown = _documented() - fields - named_by_config
    assert not unknown, (
        f".env.example documents keys that are neither Settings fields nor "
        f"credentials named by a config row: {sorted(unknown)}. A misspelled "
        "key is accepted in silence, so this is how one is caught."
    )
