"""Options shared by every JSON-framed WebSocket conversational provider.

Responsible for: the parts of `remote.options` this protocol family defines.
Used by: each provider's own options model inherits from `WsFlowOptions` and adds
what only its platform has.

`extra="forbid"` is declared here and inherited. It is the single most important
line in the file: pydantic does NOT propagate a parent's `extra` setting to a
nested FIELD model, and an options block that silently drops a typo takes the
field's default instead -- which for a finalize endpoint or a bot route means an
HTTP 200 and blank or misrouted output, with nothing logged anywhere.
"""
from __future__ import annotations

from typing import List

from pydantic import BaseModel, ConfigDict, Field


class HandshakeOptions(BaseModel):
    """How long to wait after sending the handshake frame.

    These servers never acknowledge one, so `settle_ms` is a settle window
    rather than a timeout. `ack_types` exists for a future release that does
    acknowledge: a frame whose source matches ends the wait early.
    """

    model_config = ConfigDict(extra="forbid")

    settle_ms:  int = 1500
    ack_types:  List[str] = Field(default_factory=lambda: ["authenticated", "auth_success"])
    timeout_ms: int = 5000


class WsFlowOptions(BaseModel):
    """The options every provider in this family carries."""

    model_config = ConfigDict(extra="forbid")

    # min_length=1, not Optional: an empty value does not fail loudly upstream,
    # it silently resolves the wrong bot or splits a user's profile.
    bot_route: str = Field(min_length=1)

    handshake: HandshakeOptions = Field(default_factory=HandshakeOptions)

    # Address fields sent in the authenticate frame. These servers do not
    # validate them against anything real for the flows this serves.
    ip_city:  str = ""
    ip_state: str = ""
    ip_zip:   str = ""

    # Whether to ask the REST surface, after every turn, if the conversation is
    # finished. The per-turn `finish_reason` on the socket is NOT that signal --
    # it fires at the end of every bot turn.
    completion_poll_every_turn: bool = True
