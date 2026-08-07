"""Exceptions raised by the Bhashini integration layer.

Thin wrappers so callers can catch voice failures without importing
`requests.exceptions` -- the same discipline `integrations/mitra/exceptions.py`
applies.

NOTHING HERE MAY CARRY A CREDENTIAL. Three secrets travel in the request headers
of every call (`BHASHINI_API_KEY`, `BHASHINI_USER_ID`, `BHASHINI_AUTHORIZATION`),
so messages quote Bhashini's own error text and never the request that produced
it.
"""
from typing import Optional


class BhashiniError(Exception):
    """Base class for all Bhashini integration errors."""


class BhashiniTimeout(BhashiniError):
    """Raised when Dhruva does not answer inside the configured timeout.

    Kept separate because the router maps it to 504 UPSTREAM_TIMEOUT, which the
    frontend already renders with a retry button -- Dhruva is a public-sector
    service with no SLA, and "try again" is usually the right advice.
    """

    def __init__(self, task: str, timeout_s: float) -> None:
        super().__init__(f"Bhashini {task} timed out after {timeout_s}s")
        self.task = task


class BhashiniUpstreamError(BhashiniError):
    """Raised when Bhashini answers with a non-2xx status or an unusable body.

    `detail` is Bhashini's own message where it sent one. The raw body is not
    included: it echoes request fields back, and those include the auth headers.
    """

    def __init__(self, task: str, status: Optional[int] = None, detail: str = "") -> None:
        where = f"HTTP {status}" if status is not None else "an unreadable response"
        message = f"Bhashini {task} returned {where}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)
        self.task = task
        self.status = status
        self.detail = detail


class AudioConversionError(BhashiniError):
    """Raised when audio cannot be transcoded, split or merged.

    Usually a corrupt or truncated upload, or a missing ffmpeg binary -- so the
    message says which, because the two have completely different fixes.
    """


class UnsupportedLanguageError(BhashiniError):
    """Raised for a language code with no configured Bhashini service."""

    def __init__(self, language: str, supported: list) -> None:
        super().__init__(
            f"language {language!r} is not supported. Supported: {', '.join(supported)}"
        )
        self.language = language
