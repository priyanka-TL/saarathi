"""Mitra failures.

Responsible for: the error types the Mitra integration raises.
Used by: the REST client and channel raise them; the routers map them to 502/504.
"""
from typing import Optional


class MitraError(Exception):
    """Base class for all Mitra integration errors."""


class MitraHTTPError(MitraError):
    """Raised when Mitra returns a non-2xx HTTP status code.

    ``detail`` carries Mitra's OWN structured error text (the ``error_message``
    / ``error_type`` keys of its error envelope), extracted key-by-key by
    ``MitraRestClient._extract_error_detail`` — never the raw body, which can
    reflect request headers back (§13.2). Without it every upstream failure
    reads "returned HTTP 500" and gives an operator nothing to act on: a
    missing Mitra Flow row and a genuine Mitra outage look identical.
    """

    def __init__(
        self, method: str, path: str, status: int, detail: Optional[str] = None
    ) -> None:
        msg = f"Mitra {method} {path} returned HTTP {status}"
        if detail:
            msg = f"{msg}: {detail}"
        super().__init__(msg)
        self.status = status
        self.detail = detail


class MitraSSRFError(MitraError):
    """Raised when a URL returned by Mitra fails the allowlist check.

    The URL itself is NOT included in the message to prevent SSRF payloads
    from leaking into logs via exception formatting.
    """

    def __init__(self) -> None:
        super().__init__(
            "URL returned by Mitra failed allowlist validation "
            "(must be https and from an approved host)"
        )


class MitraRedirectError(MitraError):
    """Raised when Mitra returns a redirect (3xx).

    Redirects are disabled on the transport. Any 3xx is unexpected and
    indicates a misconfiguration of MITRA_BASE_URL.
    """

    def __init__(self, status: int) -> None:
        super().__init__(f"Unexpected redirect (HTTP {status}) from Mitra — check MITRA_BASE_URL")
        self.status = status


class MitraTurnTimeout(MitraError):
    """Raised by MitraChannel.send_and_await_turn when no bot frame arrives
    before either the turn timeout or the idle-gap backstop, and nothing had
    been accumulated yet to flush instead."""

    def __init__(self, step: Optional[int] = None) -> None:
        super().__init__(f"no bot response before timeout (last step={step})")
        self.step = step


class MitraChannelClosed(MitraError):
    """Raised when the underlying WebSocket connection has died (reader
    thread exited) and a caller tries to use the channel anyway."""

    def __init__(self, reason: str = "") -> None:
        super().__init__(f"channel closed: {reason}" if reason else "channel closed")
        self.reason = reason


class MitraRemoteError(MitraError):
    """Raised when Mitra sends a system/error frame mid-turn (source=='system'
    with an error message set)."""

    def __init__(self, msg: str) -> None:
        super().__init__(msg)


class MitraConcurrentTurnError(MitraError):
    """Raised when a concurrent request attempts to use a MitraChannel that
    is already executing a turn."""

    def __init__(self) -> None:
        super().__init__("A turn is already in progress for this conversation.")
