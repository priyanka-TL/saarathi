"""Saathi failures.

Responsible for: the error types the Saathi integration raises.
Used by: the client and channel raise them; the routers map them to status codes.

Mirrors app/integrations/mitra/exceptions.py, and for the same reason: a router
that had to `except requests.HTTPError` would be coupled to this package's
HTTP library.
"""
from __future__ import annotations

from typing import Optional


class SaathiError(Exception):
    """Base for every Saathi-side failure."""


class SaathiAuthError(SaathiError):
    """The access token was rejected, or none could be obtained.

    RAISED FOR BOTH MECHANISMS, deliberately. Under `token` it means the
    configured JWT is dead -- ELEVATE tracks its session_id server-side and can
    invalidate it long before the token's own `exp`, so this is expected rather
    than exceptional. Under `password` it means a re-mint was already attempted
    and also failed.

    Never carries the token or the password.
    """

    def __init__(self, detail: str, *, mechanism: Optional[str] = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.mechanism = mechanism


class SaathiConfigError(SaathiError):
    """The Saathi credentials or endpoints are not configured coherently.

    Raised at BOOT, not at request time: a deployment missing its credentials
    should fail to start rather than serve a button that errors on click.
    """


class SaathiHTTPError(SaathiError):
    """A non-2xx from the Saathi REST surface."""

    def __init__(self, method: str, path: str, status: int, detail: str = "") -> None:
        super().__init__(f"Saathi {method} {path} -> HTTP {status}{f': {detail}' if detail else ''}")
        self.method = method
        self.path = path
        self.status = status
        self.detail = detail


class SaathiChannelClosed(SaathiError):
    """The WebSocket closed, either during the handshake or mid-turn."""

    def __init__(self, reason: str = "") -> None:
        super().__init__(f"Saathi channel closed{f': {reason}' if reason else ''}")
        self.reason = reason


class SaathiTurnTimeout(SaathiError):
    """The bot did not finish a turn within the configured window.

    Does NOT mean the turn failed -- it means we stopped listening. The reply
    may still have been recorded on Saathi's side.
    """

    def __init__(self, step: Optional[int] = None) -> None:
        super().__init__("Saathi did not complete the turn in time")
        self.step = step


class SaathiConcurrentTurnError(SaathiError):
    """A second turn was attempted while one was already in flight."""

    def __init__(self) -> None:
        super().__init__("a turn is already in flight on this Saathi channel")
