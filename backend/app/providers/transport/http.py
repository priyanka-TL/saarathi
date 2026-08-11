"""One provider's REST transport.

Responsible for: issuing a request, refusing a redirect, extracting a safe error
detail, and validating a URL a provider handed back.
Used by: every provider's REST client, which supplies only the paths and payloads.

WHAT IS SHARED AND WHY. The two integrations that existed before this module
each hand-rolled the same shape: one reusable `requests.Session`, fixed headers
applied once, a tuple timeout, `allow_redirects=False`, 3xx -> raise, non-2xx ->
raise with a key-by-key error detail, non-JSON -> raise. None of that is
platform knowledge; all of it is the thing a new provider would otherwise
re-derive, usually forgetting the parts whose failure is silent.

THE FIXED HEADERS HOLD A CREDENTIAL. Providers gate admission on the `Origin`
header, so it is built in once and `__repr__` is overridden. It must never reach
a log line, a `__repr__` or an exception -- which is also why
`_extract_error_detail` reads only known keys and drops its result entirely if
the credential appears in it anyway.

ONE OPTIONAL RE-AUTH. `on_unauthorized` exists because a per-user provider's
token can be ended server-side mid-life, making a 401 an expected event rather
than a bug. It fires at most once per request; a second 401 propagates, because
retrying further would spin against the login endpoint.
"""
from __future__ import annotations

from typing import Callable, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlparse

import requests
from requests import Session as HTTPSession

from app.providers.errors import (
    ProviderError,
    ProviderHTTPError,
    ProviderRedirectError,
    ProviderSSRFError,
)

#: Error-envelope keys read out of a failed response, in preference order. A
#: provider may override; the default covers the DRF-shaped envelopes seen so far.
DEFAULT_ERROR_KEYS: Sequence[str] = ("error_message", "error_type", "detail")

#: How much of the extracted detail is kept. Long enough to name the failure,
#: short enough that a body cannot be exfiltrated through an error message.
DEFAULT_DETAIL_LIMIT = 300


def url_is_permitted(url: str, allowed_hosts: Iterable[str]) -> bool:
    """Whether a URL a provider handed back may be surfaced or fetched.

    1. Scheme must be https (prevents an http downgrade).
    2. Hostname must be in the allowlist (prevents a compromised or buggy
       provider response pointing at an internal host, or at an attacker's).

    THE RULE LIVES HERE ONCE; THE POLICY DIFFERS BY CALLER. `validate_url` below
    raises on a false, because it is about to fetch the URL and refusing loudly
    is right. The attachment path in `ws_flow/base.py` drops the URL with a
    warning instead, because the turn's reply text is still worth showing and
    failing a whole turn over one bad link would be worse than showing one fewer
    download button.

    Exact host match, no suffix or wildcard: `evil-mohini-static.example` must
    not pass merely because it ends with a permitted string.
    """
    parsed = urlparse(url or "")
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return host in {h.strip().lower() for h in allowed_hosts if h and h.strip()}


class RestTransport:
    """A provider's HTTP surface: stateless, no threads, no path knowledge.

    :param provider: display name, used only in error messages.
    :param origin_url: sent as `Origin` on every call. A CREDENTIAL.
    :param allowed_hosts: extra FQDNs whose URLs may be fetched; base_url's own
        host is always trusted.
    :param auth_headers: called per request for the per-request credential
        headers (a token that may have been re-minted since the last call).
    :param on_unauthorized: called once on a 401; returns True to retry.
    """

    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        origin_url: str,
        user_agent: str,
        connect_timeout: float,
        read_timeout: float,
        allowed_hosts: Sequence[str] = (),
        extra_headers: Optional[Mapping[str, str]] = None,
        auth_headers: Optional[Callable[[], Mapping[str, str]]] = None,
        on_unauthorized: Optional[Callable[[], bool]] = None,
        error_keys: Sequence[str] = DEFAULT_ERROR_KEYS,
        detail_limit: int = DEFAULT_DETAIL_LIMIT,
    ) -> None:
        self.provider = provider
        self._base_url = base_url.rstrip("/")
        self._timeout = (connect_timeout, read_timeout)
        self._auth_headers = auth_headers
        self._on_unauthorized = on_unauthorized
        self._error_keys = tuple(error_keys)
        self._detail_limit = detail_limit

        # The base URL's own hostname is always trusted.
        base_host = urlparse(base_url).hostname or ""
        self._allowed_hosts: frozenset[str] = frozenset(
            h.strip().lower() for h in [base_host, *allowed_hosts] if h and h.strip()
        )

        # SECURITY: holds the Origin credential. Must never appear in a log, a
        # __repr__ or an exception.
        self._fixed_headers: dict[str, str] = {
            "Origin": origin_url,
            "User-Agent": user_agent,
            "Accept": "application/json",
        }
        if extra_headers:
            self._fixed_headers.update(extra_headers)

        # One reusable session: shared connection pool, fixed headers once.
        self._session: HTTPSession = requests.Session()
        self._session.headers.update(self._fixed_headers)

    def __repr__(self) -> str:
        # Omits _fixed_headers so the Origin credential cannot leak into a log
        # line that reprs this object.
        return f"RestTransport(provider={self.provider!r}, base_url={self._base_url!r})"

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> dict:
        """Execute one request and return the parsed JSON body.

        :raises ProviderRedirectError: on any 3xx (redirects are disabled).
        :raises ProviderHTTPError: on any other non-2xx.
        :raises ProviderError: on a network error or a non-JSON body.
        """
        response = self._send(method, path, json=json, params=params, extra_headers=extra_headers)

        if response.status_code == 401 and self._on_unauthorized is not None:
            # EXACTLY ONE RETRY. A provider that ends a token's session
            # server-side makes a 401 an expected mid-life event; a second one
            # means the credentials themselves are wrong.
            if self._on_unauthorized():
                response = self._send(
                    method, path, json=json, params=params, extra_headers=extra_headers,
                )

        if 300 <= response.status_code < 400:
            raise ProviderRedirectError(response.status_code, self.provider)

        if not response.ok:
            raise ProviderHTTPError(
                self.provider, method, path, response.status_code,
                detail=self._extract_error_detail(response),
            )

        try:
            return response.json()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(
                f"{self.provider} {method} {path} returned a non-JSON body"
            ) from exc

    def _send(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ):
        url = f"{self._base_url}{path}"
        headers: dict[str, str] = {}
        if self._auth_headers is not None:
            headers.update(self._auth_headers())
        if json is not None:
            headers.setdefault("Content-Type", "application/json")
        if extra_headers:
            headers.update(extra_headers)

        try:
            return self._session.request(
                method,
                url,
                json=json,
                params=params,
                headers=headers,
                timeout=self._timeout,
                # Redirects are unexpected; following one would replay the
                # credentials at whatever host the redirect names.
                allow_redirects=False,
            )
        except requests.exceptions.RequestException as exc:
            # Do NOT include `url`: query params echo a session id.
            raise ProviderError(
                f"Network error calling {self.provider} {method} {path}"
            ) from exc

    # ------------------------------------------------------------------
    # Safety helpers
    # ------------------------------------------------------------------

    def _extract_error_detail(self, response) -> Optional[str]:
        """Pull the provider's own error text out of a failed response, safely.

        The raw body is NEVER returned. A 403 from an edge proxy can reflect the
        `Origin` credential back in its body, so this reads only known keys,
        truncates them, and drops the result entirely if the credential appears
        in it anyway. Non-JSON bodies (HTML error pages) yield None.
        """
        try:
            body = response.json()
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(body, dict):
            return None

        parts = [str(body[key]) for key in self._error_keys if body.get(key)]
        if not parts:
            return None

        detail = " | ".join(parts)[: self._detail_limit]
        # Read the credential from its single home rather than keeping a second
        # copy of it on the instance.
        origin = self._fixed_headers.get("Origin")
        if origin and origin in detail:
            return None
        return detail

    def validate_url(self, url: str) -> None:
        """Assert that a URL a provider handed back is safe to fetch.

        The rule itself lives in `app/providers/connection.py::url_is_permitted`
        so the attachment path can apply the SAME check with a different policy
        (drop rather than raise). This is the raising half.

        The URL is NOT included in the raised exception.
        """
        if not url_is_permitted(url, self._allowed_hosts):
            raise ProviderSSRFError(self.provider)
