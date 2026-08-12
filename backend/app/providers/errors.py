"""Remote-provider failures, named for what happened rather than for who it
happened to.

Responsible for: the one exception hierarchy every provider raises and the core
catches.
Used by: the transports and providers raise them; app/exceptions/envelope.py maps
them to 504/429/502; the chat and sessions routers catch the base class.

WHY A SINGLE HIERARCHY. Each integration used to declare its own base class
(`MitraError`, `SaathiError`), and the routers caught one of them by name. The
consequence was not cosmetic: `SaathiError` subclassed plain `Exception` and was
caught NOWHERE, so a Saathi REST failure fell through to the blanket handler and
answered 500 INTERNAL where the identical Mitra failure answered 502
UPSTREAM_UNAVAILABLE. A shared base makes that class of omission impossible --
adding a provider cannot forget to be caught.

MESSAGES ARE PROVIDER-LABELLED, not provider-typed. `ProviderHTTPError` takes the
provider's display name so an operator still reads "Mitra GET /api/profile/
returned HTTP 500"; what changed is that the core no longer needs a class per
vendor to say it.

NO CREDENTIAL EVER REACHES ONE OF THESE. The Origin header, the ELEVATE token and
the login password are all credentials; `ProviderSSRFError` omits the URL and
the HTTP error's `detail` is extracted key-by-key by the transport, never taken
from a raw body.
"""
from __future__ import annotations

from typing import Optional


class ProviderError(Exception):
    """Base for every failure originating at a remote provider.

    Caught by name in app/routers/chat.py and app/routers/sessions.py. A
    provider that raises anything outside this hierarchy is a bug: it will be
    reported to the user as an internal error rather than an upstream one.
    """


class ProviderHTTPError(ProviderError):
    """A non-2xx from a provider's REST surface.

    ``detail`` carries the provider's OWN structured error text, extracted
    key-by-key by the transport -- never the raw body, which can reflect request
    headers (and therefore the Origin credential) back. Without it every
    upstream failure reads "returned HTTP 500" and gives an operator nothing to
    act on: a missing upstream Flow row and a genuine outage look identical.
    """

    def __init__(
        self,
        provider: str,
        method: str,
        path: str,
        status: int,
        detail: Optional[str] = None,
    ) -> None:
        msg = f"{provider} {method} {path} returned HTTP {status}"
        if detail:
            msg = f"{msg}: {detail}"
        super().__init__(msg)
        self.provider = provider
        self.method = method
        self.path = path
        self.status = status
        self.detail = detail


class ProviderSSRFError(ProviderError):
    """A URL returned by a provider failed the https + allowlist check.

    The URL itself is NOT included in the message, to stop an SSRF payload
    leaking into logs via exception formatting.
    """

    def __init__(self, provider: str = "the provider") -> None:
        super().__init__(
            f"URL returned by {provider} failed allowlist validation "
            "(must be https and from an approved host)"
        )
        self.provider = provider


class ProviderRedirectError(ProviderError):
    """A provider answered with a 3xx.

    Redirects are disabled on every transport: following one would replay the
    credentials at whatever host the redirect names. Any 3xx means the
    configured base_url is wrong.
    """

    def __init__(self, status: int, provider: str = "the provider") -> None:
        super().__init__(
            f"Unexpected redirect (HTTP {status}) from {provider} -- "
            "check remote.base_url on this agent's configuration"
        )
        self.status = status
        self.provider = provider


class ProviderTurnTimeout(ProviderError):
    """No reply arrived before the turn timeout or the idle-gap backstop.

    DOES NOT MEAN THE TURN FAILED -- it means this side stopped listening. The
    provider may well have recorded the turn and answered it, which is why the
    orchestrator reconciles rather than resends. See app/providers/recovery.py.
    """

    def __init__(self, step: Optional[int] = None) -> None:
        super().__init__(f"no bot response before timeout (last step={step})")
        self.step = step


class ProviderChannelClosed(ProviderError):
    """The streaming transport died (reader thread exited) and a caller tried
    to use the channel anyway."""

    def __init__(self, reason: str = "") -> None:
        super().__init__(f"channel closed: {reason}" if reason else "channel closed")
        self.reason = reason


class ProviderRemoteError(ProviderError):
    """The provider sent an in-band error frame mid-turn."""


class ProviderConcurrentTurnError(ProviderError):
    """A second turn was attempted while one was already in flight on the same
    channel. Maps to 429; never retried automatically."""

    def __init__(self) -> None:
        super().__init__("A turn is already in progress for this conversation.")


class ProviderAuthError(ProviderError):
    """Credentials were rejected, or none could be obtained.

    Never carries the token, the password or the Origin header.
    """

    def __init__(self, detail: str, *, mechanism: Optional[str] = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.mechanism = mechanism


class ProviderConfigError(ProviderError):
    """A provider's configuration is incoherent.

    Raised at CONFIG-WRITE time or at BOOT, not mid-turn: a deployment missing
    its credentials should fail loudly rather than render a button that errors
    on click.
    """


class UnknownProvider(ProviderConfigError):
    """A config row names a provider nothing registered."""

    def __init__(self, name: str, known: object = ()) -> None:
        known_list = ", ".join(sorted(known)) or "none"
        super().__init__(
            f"unknown remote provider {name!r}; registered providers: {known_list}"
        )
        self.name = name


class ProviderNotEnabled(ProviderConfigError):
    """A config row names a registered provider this deployment has switched off.

    Distinct from UnknownProvider on purpose: one is a typo in a config row, the
    other is a deliberate deployment choice, and the operator fixes them in
    different places.
    """

    def __init__(self, name: str) -> None:
        super().__init__(
            f"remote provider {name!r} is not enabled in this deployment "
            f"(add it to PROVIDERS_ENABLED)"
        )
        self.name = name
