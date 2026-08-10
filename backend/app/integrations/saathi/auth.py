"""Where Saathi's access token comes from.

Responsible for: producing a valid ELEVATE JWT, and re-minting it when one is
rejected.
Used by: SaathiRestClient on every request, and SaathiChannel on the handshake.

TWO MECHANISMS, CHOSEN BY `SAATHI_LOGIN_MECHANISM`, resolved through the
name->class table below in the same style as `storage/factory.py`:

    "password"  LoginTokenProvider   mints from email/password, re-mints on 401
    "token"     StaticTokenProvider  uses SAATHI_ACCESS_TOKEN verbatim

Callers never branch on the mechanism: both satisfy `get()` / `invalidate()`,
and a caller that hits a 401 calls `invalidate()` and retries exactly once.
Under `token` that retry is guaranteed to fail again, which is the honest
outcome -- there is nothing to re-mint from -- and it fails with a message that
names the mechanism rather than a bare 401.

WHY `token` CANNOT SELF-HEAL, measured rather than assumed. A JWT four days
short of its own `exp` was rejected by ELEVATE with "Session expired. Please
login again." -- ELEVATE tracks the `session_id` claim server-side and can end
it at any moment. So the token's `exp` is not the authority, and a static token
is a demo convenience, not a deployment posture.

THE TOKEN AND THE PASSWORD ARE CREDENTIALS. Neither appears in a log line, a
`__repr__`, or an exception message.
"""
from __future__ import annotations

import threading
from typing import Optional, Protocol

import requests

from app.core.logger import get_logger
from app.integrations.saathi.exceptions import SaathiAuthError, SaathiConfigError

logger = get_logger(__name__)

#: ELEVATE's login endpoint, relative to `elevate_base_url`.
LOGIN_PATH = "/user/v1/account/login"

#: ELEVATE resolves the tenant from this header. Without it login answers 406
#: "Tenant domain not found" -- a confusing error for a credential problem.
TENANT_HEADER = "x-tenant-code"

#: The login body's identity field is `identifier`, NOT `email`: it accepts an
#: email, a phone number or a username.
IDENTIFIER_FIELD = "identifier"


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
    """Returns `SAATHI_ACCESS_TOKEN` verbatim. Cannot recover from a 401."""

    mechanism = "token"

    def __init__(self, token: str) -> None:
        if not token:
            raise SaathiConfigError(
                "SAATHI_LOGIN_MECHANISM='token' but SAATHI_ACCESS_TOKEN is not set."
            )
        self._token = token
        self._invalidated = False

    def get(self) -> str:
        if self._invalidated:
            # Deliberately explicit rather than handing back the same dead token
            # and letting the caller see a second, identical 401.
            raise SaathiAuthError(
                "The configured SAATHI_ACCESS_TOKEN was rejected by ELEVATE and "
                "cannot be renewed. ELEVATE ends a token's session server-side, "
                "so this happens even before the token's own expiry. Set "
                "SAATHI_LOGIN_MECHANISM=password to re-mint automatically, or "
                "replace SAATHI_ACCESS_TOKEN.",
                mechanism=self.mechanism,
            )
        return self._token

    def invalidate(self) -> None:
        self._invalidated = True

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "StaticTokenProvider(token=<redacted>)"


class LoginTokenProvider:
    """Mints a token from email/password, and re-mints when one is rejected."""

    mechanism = "password"

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
            raise SaathiConfigError(
                "SAATHI_LOGIN_MECHANISM='password' requires SAATHI_EMAIL and "
                "SAATHI_PASSWORD."
            )
        if not base_url:
            raise SaathiConfigError("ELEVATE_BASE_URL is required to mint a Saathi token.")

        self._base_url = base_url.rstrip("/")
        self._identifier = identifier
        self._password = password
        self._tenant_code = tenant_code
        self._timeout = (connect_timeout, read_timeout)

        # One token is shared by every request in the process, so minting must
        # not race: two threads hitting a 401 together would otherwise perform
        # two logins and each invalidate the other's session.
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
        """Exchange the credentials for a JWT.

        :raises SaathiAuthError: on rejected credentials or an unusable response.
        """
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
            raise SaathiAuthError(
                f"Network error contacting ELEVATE login: {type(exc).__name__}",
                mechanism=self.mechanism,
            ) from exc

        if response.status_code >= 400:
            # The body carries ELEVATE's own message, which is safe to surface
            # (it names the failure, never the credentials).
            raise SaathiAuthError(
                f"ELEVATE login failed: HTTP {response.status_code} "
                f"{_safe_message(response)}",
                mechanism=self.mechanism,
            )

        token = _extract_token(response)
        if not token:
            raise SaathiAuthError(
                "ELEVATE login returned no access token.", mechanism=self.mechanism
            )

        logger.info(
            "saathi: minted an access token",
            extra={"mechanism": self.mechanism, "tenant_code": self._tenant_code},
        )
        return token

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"LoginTokenProvider(identifier=<redacted>, tenant={self._tenant_code!r})"


def _safe_message(response) -> str:
    """ELEVATE's `message`, truncated. Never the whole body."""
    try:
        return str(response.json().get("message", ""))[:200]
    except Exception:  # noqa: BLE001 - a non-JSON error body is still an error
        return ""


def _extract_token(response) -> str:
    """Pull the access token out of ELEVATE's response envelope.

    Several key spellings are tried because the envelope is ELEVATE's, not
    ours, and a single wrong guess would fail at runtime with a token-less
    success. `tests/unit/test_saathi_auth.py` pins the shapes we have seen.
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
    does the same strip (`shikshalokam/utils/project_utils.py:18`). A quoted
    token is not rejected as malformed; it simply authenticates as nobody.
    """
    token = token.strip()
    if len(token) >= 2 and token.startswith('"') and token.endswith('"'):
        return token[1:-1]
    return token


#: mechanism name -> builder. Mirrors storage/factory.py's name->driver table:
#: adding a third mechanism is an entry here, not a branch at the call site.
_PROVIDERS = ("password", "token")


def build_token_provider(settings) -> TokenProvider:
    """The TokenProvider this deployment's configuration describes.

    :raises SaathiConfigError: at BOOT, when the chosen mechanism is missing
        the credentials it needs -- a deployment should fail to start rather
        than render a Saathi button that errors on click.
    """
    mechanism = (getattr(settings, "saathi_login_mechanism", "") or "password").strip().lower()

    if mechanism == "token":
        return StaticTokenProvider(settings.saathi_access_token or "")

    if mechanism == "password":
        return LoginTokenProvider(
            base_url=settings.elevate_base_url,
            identifier=settings.saathi_email or "",
            password=settings.saathi_password or "",
            tenant_code=settings.saathi_tenant_code,
        )

    raise SaathiConfigError(
        f"SAATHI_LOGIN_MECHANISM={mechanism!r} is not supported. "
        f"Supported: {', '.join(_PROVIDERS)}."
    )
