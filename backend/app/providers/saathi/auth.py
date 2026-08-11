"""Where Saathi's access token comes from.

Responsible for: producing a valid ELEVATE JWT, and re-minting it when one is
rejected.
Used by: SaathiProvider, on every REST request and on every socket handshake.

TWO SCHEMES, CHOSEN BY `remote.auth.scheme` IN THE CONFIG ROW, resolved through
the name->builder table below in the same style as `storage/factory.py`:

    "elevate_login"  LoginTokenProvider   mints from identifier/password, re-mints on 401
    "static_token"   StaticTokenProvider  uses a pre-issued token verbatim

Callers never branch on the scheme: both satisfy `get()` / `invalidate()`, and a
caller that hits a 401 calls `invalidate()` and retries exactly once. Under
`static_token` that retry is guaranteed to fail again, which is the honest
outcome -- there is nothing to re-mint from -- and it fails with a message that
names the scheme rather than a bare 401.

WHY `static_token` CANNOT SELF-HEAL, measured rather than assumed. A JWT four
days short of its own `exp` was rejected by ELEVATE with "Session expired. Please
login again." -- ELEVATE tracks the `session_id` claim server-side and can end it
at any moment. So the token's `exp` is not the authority, and a static token is a
demo convenience, not a deployment posture.

ONE PROVIDER PER CONNECTION, NOT ONE PER PROCESS. This used to be a single
container-built singleton fed from `SAATHI_*` settings, which meant one
deployment could serve exactly one Saathi identity for every tenant. It is now
built from the resolved connection and cached alongside it, so a tenant-scoped
config row naming its own credential variables gets its own token.

THE TOKEN, THE PASSWORD AND THE ORIGIN ARE CREDENTIALS. None appears in a log
line, a `__repr__`, or an exception message.
"""
from __future__ import annotations

import threading
from typing import Optional, Protocol

import requests

from app.core.logger import get_logger
from app.providers.errors import ProviderAuthError, ProviderConfigError

logger = get_logger("provider.saathi.auth")

#: ELEVATE's login endpoint, relative to the configured token endpoint.
LOGIN_PATH = "/user/v1/account/login"

#: ELEVATE resolves the tenant from this header. Without it login answers 406
#: "Tenant domain not found" -- a confusing error for a credential problem.
TENANT_HEADER = "x-tenant-code"

#: The login body's identity field is `identifier`, NOT `email`: it accepts an
#: email, a phone number or a username.
IDENTIFIER_FIELD = "identifier"

#: The schemes this provider understands. Adding a third is an entry in
#: `_BUILDERS` below, not a branch at a call site.
SCHEMES = ("elevate_login", "static_token")


class TokenProvider(Protocol):
    """A source of ELEVATE access tokens."""

    mechanism: str

    def get(self) -> str:
        """A token believed valid. May mint one; may return a cached one."""
        ...

    def invalidate(self) -> None:
        """Discard the cached token. Called by a caller that saw a 401."""
        ...


class StaticTokenProvider:
    """Returns a pre-issued token verbatim. Cannot recover from a 401."""

    mechanism = "static_token"

    def __init__(self, token: str, token_env: str = "") -> None:
        if not token:
            raise ProviderConfigError(
                f"remote.auth.scheme is 'static_token' but the variable named by "
                f"token_env ({token_env or 'unset'}) holds no value."
            )
        self._token = token
        self._invalidated = False

    def get(self) -> str:
        if self._invalidated:
            # Deliberately explicit rather than handing back the same dead token
            # and letting the caller see a second, identical 401.
            raise ProviderAuthError(
                "The configured static token was rejected by ELEVATE and cannot "
                "be renewed. ELEVATE ends a token's session server-side, so this "
                "happens even before the token's own expiry. Use "
                "remote.auth.scheme='elevate_login' to re-mint automatically, or "
                "replace the token.",
                mechanism=self.mechanism,
            )
        return self._token

    def invalidate(self) -> None:
        self._invalidated = True

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "StaticTokenProvider(token=<redacted>)"


class LoginTokenProvider:
    """Mints a token from identifier/password, and re-mints when one is rejected."""

    mechanism = "elevate_login"

    def __init__(
        self,
        *,
        base_url: str,
        identifier: str,
        password: str,
        tenant_code: str,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
    ) -> None:
        if not identifier or not password:
            raise ProviderConfigError(
                "remote.auth.scheme is 'elevate_login' but the variables named by "
                "identifier_env and secret_env do not both hold values."
            )
        if not base_url:
            raise ProviderConfigError(
                "remote.auth.token_endpoint is required to mint a Saathi token."
            )

        self._base_url = base_url.rstrip("/")
        self._identifier = identifier
        self._password = password
        self._tenant_code = tenant_code
        self._timeout = (connect_timeout, read_timeout)

        # One token is shared by every request through this connection, so
        # minting must not race: two threads hitting a 401 together would
        # otherwise perform two logins and each invalidate the other's session.
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._session = requests.Session()

    def get(self) -> str:
        with self._lock:
            if self._token is None:
                self._token = self._login()
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None

    def _login(self) -> str:
        """Exchange the credentials for a JWT."""
        url = f"{self._base_url}{LOGIN_PATH}"
        try:
            response = self._session.post(
                url,
                json={IDENTIFIER_FIELD: self._identifier, "password": self._password},
                headers={
                    "Content-Type": "application/json",
                    TENANT_HEADER: self._tenant_code,
                },
                timeout=self._timeout,
            )
        except requests.exceptions.RequestException as exc:
            raise ProviderAuthError(
                f"Network error contacting ELEVATE login: {type(exc).__name__}",
                mechanism=self.mechanism,
            ) from exc

        if response.status_code >= 400:
            # The body carries ELEVATE's own message, which is safe to surface
            # (it names the failure, never the credentials).
            raise ProviderAuthError(
                f"ELEVATE login failed: HTTP {response.status_code} "
                f"{_safe_message(response)}",
                mechanism=self.mechanism,
            )

        token = _extract_token(response)
        if not token:
            raise ProviderAuthError(
                "ELEVATE login returned no access token.", mechanism=self.mechanism
            )

        logger.info(
            "saathi: minted an access token",
            extra={"mechanism": self.mechanism, "tenant_code": self._tenant_code},
        )
        return token

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"LoginTokenProvider(identifier=<redacted>, tenant={self._tenant_code!r})"


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def _build_static(conn, auth: dict) -> TokenProvider:
    return StaticTokenProvider(
        conn.secrets.get("token_env", ""),
        token_env=auth.get("token_env", ""),
    )


def _build_login(conn, auth: dict) -> TokenProvider:
    return LoginTokenProvider(
        base_url=auth.get("token_endpoint", ""),
        identifier=conn.secrets.get("identifier_env", ""),
        password=conn.secrets.get("secret_env", ""),
        tenant_code=auth.get("tenant_code", ""),
        connect_timeout=conn.connect_timeout_s,
        read_timeout=conn.read_timeout_s,
    )


#: scheme -> builder. Mirrors storage/factory.py's name->driver table: adding a
#: third scheme is an entry here, not a branch at the call site.
_BUILDERS = {
    "elevate_login": _build_login,
    "static_token": _build_static,
}


def build_token_provider(conn) -> TokenProvider:
    """The TokenProvider this connection's `remote.auth` block describes.

    :raises ProviderConfigError: when the chosen scheme is unknown, or is
        missing the credentials it needs. Raised as the provider instance is
        built -- which is the first turn, not the boot -- so a misconfigured
        agent fails with a clear message instead of a silent 401 loop.
    """
    auth = dict(conn.auth)
    scheme = (auth.get("scheme") or "").strip().lower()

    builder = _BUILDERS.get(scheme)
    if builder is None:
        raise ProviderConfigError(
            f"remote.auth.scheme={scheme!r} is not supported by the saathi "
            f"provider. Supported: {', '.join(SCHEMES)}."
        )
    return builder(conn, auth)


def _safe_message(response) -> str:
    """ELEVATE's `message`, truncated. Never the whole body."""
    try:
        return str(response.json().get("message", ""))[:200]
    except Exception:  # noqa: BLE001 - a non-JSON error body is still an error
        return ""


def _extract_token(response) -> str:
    """Pull the access token out of ELEVATE's response envelope.

    Several key spellings are tried because the envelope is ELEVATE's, not ours,
    and a single wrong guess would fail at runtime with a token-less success.
    tests/unit/test_saathi_auth.py pins the shapes we have seen.
    """
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        return ""

    result = body.get("result") if isinstance(body.get("result"), dict) else body
    for key in ("access_token", "accessToken", "token"):
        value = result.get(key) if isinstance(result, dict) else None
        if isinstance(value, str) and value:
            return _unquote(value)

    # Some ELEVATE builds nest the pair under `result.tokens`.
    tokens = result.get("tokens") if isinstance(result, dict) else None
    if isinstance(tokens, dict):
        for key in ("access", "access_token", "accessToken"):
            value = tokens.get(key)
            if isinstance(value, str) and value:
                return _unquote(value)
    return ""


def _unquote(token: str) -> str:
    """Strip surrounding double quotes.

    ELEVATE hands the token back quoted in some builds -- Saathi's own client
    does the same strip. A quoted token is not rejected as malformed; it simply
    authenticates as nobody.
    """
    token = token.strip()
    if len(token) >= 2 and token.startswith('"') and token.endswith('"'):
        return token[1:-1]
    return token
