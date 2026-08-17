"""ELEVATE user-service failures.

Responsible for: the error types the profile integration raises.
Used by: the client raises them; the profile router maps them to status codes.

The Django original this is ported from returns SENTINEL DICTS instead
(`{'error': 'unauthorized', 'status_code': 401}`), and every one of its views
repeats the same three-block check. Typed exceptions are used here because this
codebase's rule is that integrations raise and the ROUTER decides HTTP
(app/services/voice_service.py says so outright), and because a sentinel has a
worse failure mode: `fetch_elevate_user` returns a bare `{}` both for "the
envelope said not-ok" and for "the profile came back without an id", which is
indistinguishable from a genuinely empty profile. A caller that mistakes the
first for the second concludes every field is missing and overwrites good data.
"""
from typing import Optional


class ElevateError(Exception):
    """Base class for all ELEVATE user-service errors."""


class ElevateUnauthorized(ElevateError):
    """Raised when ELEVATE rejects the caller's token (HTTP 401).

    Kept separate because it is the ONLY failure allowed to surface as a 401.
    The frontend wipes the stored session on any 401 (src/api/http.js), which
    is right when ELEVATE says the session ended upstream -- our own JWT
    verification already passed in get_current_user, so this genuinely means
    the token was revoked or expired ELEVATE-side -- and is a spurious logout
    for anything else.
    """


class ElevateRejected(ElevateError):
    """Raised when ELEVATE refuses the submitted values (HTTP 400/422).

    Distinct from an upstream outage: the caller can fix this by editing the
    form, so the router answers 400 rather than telling them to retry a request
    that will fail identically every time.
    """

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail or "ELEVATE rejected the submitted profile values")
        self.detail = detail


class ElevateTimeout(ElevateError):
    """Raised when ELEVATE does not answer inside the configured timeout."""

    def __init__(self, task: str, timeout_s: float) -> None:
        super().__init__(f"ELEVATE {task} timed out after {timeout_s}s")
        self.task = task


class ElevateUpstreamError(ElevateError):
    """Raised on a 5xx, a redirect, an unreadable body, or a not-ok envelope.

    `detail` is ELEVATE's own message where it sent one. THE RAW BODY IS NEVER
    INCLUDED: `/user/v1/user/read` answers with the user's entire profile, so
    echoing it into an exception message puts PII in every log line that
    formats one.
    """

    def __init__(self, task: str, status: Optional[int] = None, detail: str = "") -> None:
        where = f"HTTP {status}" if status is not None else "an unreadable response"
        message = f"ELEVATE {task} returned {where}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)
        self.task = task
        self.status = status
        self.detail = detail
