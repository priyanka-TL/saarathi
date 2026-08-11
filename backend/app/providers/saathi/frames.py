"""Choice shapes only Saathi sends.

Responsible for: the one `extra_content` shape the shared parser does not cover.
Used by: SaathiProvider declares it in `option_readers`.

WHY IT LIVES HERE. This shape used to sit in the shared parser as "Shape 4 --
Saathi quick replies", inside the Mitra package. That is exactly the coupling
this architecture removes: a platform-specific quirk in a module every other
platform reads. A reader that declines by returning None composes cleanly, so the
shared shapes are untouched.
"""
from __future__ import annotations

from typing import Optional

from app.providers.transport.frames import ParsedOption


def quick_reply_chips(extra_content: dict) -> Optional[list]:
    """``{quick_reply_chips: list[str]}`` -> options.

    Plain STRINGS, not dicts, so the shared state-machine loop cannot serve
    them. Verified against live Saathi traffic: a turn carrying four chips
    parsed to zero options before this shape existed, and the buttons vanished
    with nothing logged.

    Returns None when the key is absent, so the shared shapes still get a turn.
    """
    if "quick_reply_chips" not in extra_content:
        return None
    raw_chips = extra_content["quick_reply_chips"]
    if not isinstance(raw_chips, list):
        return None
    return [
        ParsedOption(id=text, label=text, value=text)
        for text in (str(c) for c in raw_chips if c is not None)
        if text
    ]
