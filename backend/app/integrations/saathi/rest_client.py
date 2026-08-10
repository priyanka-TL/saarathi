"""The Saathi REST surface.

Responsible for: profile resolution, the terms-of-use gate, session creation and
completion polling.
Used by: SaathiFlowAgentHandler, one client per connection.

WHAT IS NOT HERE, AND WHY:

  * `upsert_profile` -- Saathi derives the profile from the ACCESS TOKEN
    (`read-elevate-profile`), so there is no (email, company) upsert. Mitra's
    email-derivation trap does not exist on this path.
  * `finalize` / `get_report` -- the saathi flow reports
    `create_story: "none"`; there is no story to submit and no PDF to fetch.
    Adding them "for symmetry" would invite a caller to finalise a flow that
    cannot, which fails by marking the session `failed`.

EVERY REQUEST CARRIES TWO CREDENTIALS: the `Origin` header (Saathi gates on it
exactly as Mitra does) and `X-auth-token` (the per-user ELEVATE JWT). Neither
is ever logged, repr'd or included in an exception.

A 401 is handled ONCE: the token provider is invalidated and the request is
retried with a freshly minted token. A second 401 propagates -- see `_request`.
"""
from __future__ import annotations

from typing import Any, Optional, Tuple

import requests

from app.core.logger import get_logger
from app.integrations.mitra.turn_recovery import ChatRow
from app.integrations.saathi.auth import TokenProvider
from app.integrations.saathi.exceptions import (
    SaathiAuthError,
    SaathiError,
    SaathiHTTPError,
)

logger = get_logger(__name__)

#: Saathi's REST paths. Its own API contract, not a preference.
PROFILE_PATH = "/api/shikshalokam/read-elevate-profile/"
ACCEPT_TNC_PATH = "/api/accept-tnc/"
GENERATE_SESSION_PATH = "/api/generate-session/"
CHAT_PATH = "/api/companychat/"


class SaathiRestClient:
    """Synchronous HTTP client for the Saathi REST surface. Stateless, no threads.

    :param base_url: e.g. ``https://qa.saathi.shikshalokam.org``.
    :param origin_url: sent as `Origin`. A CREDENTIAL -- never log or raise it.
    :param user_agent: browser UA string; Saathi expects one.
    :param tokens: supplies the ELEVATE JWT and can re-mint it on a 401.
    """

    def __init__(
        self,
        *,
        base_url: str,
        origin_url: str,
        user_agent: str,
        tokens: TokenProvider,
        connect_timeout: float,
        read_timeout: float,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._tokens = tokens
        self._timeout = (connect_timeout, read_timeout)

        # SECURITY: holds the Origin credential. Never in a log or a __repr__.
        self._fixed_headers = {
            "Origin": origin_url,
            "User-Agent": user_agent,
            "Accept": "application/json",
        }
        self._session = requests.Session()
        self._session.headers.update(self._fixed_headers)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"SaathiRestClient(base_url={self._base_url!r})"

    # ---- public API -------------------------------------------------------

    def read_profile(self) -> Tuple[str, bool]:
        """This token's Saathi profile.

        :returns: ``(profile_id, has_accepted_tnc)``.
        :raises SaathiAuthError: the token is dead and could not be renewed.
        """
        data = self._request("GET", PROFILE_PATH)
        details = data.get("profile_details") or {}
        profile_id = details.get("profileid")
        if not profile_id:
            raise SaathiError("read_profile: response carried no 'profileid'")
        return str(profile_id), bool(details.get("has_accepted_tnc"))

    def accept_tnc(self, profile_id: str) -> None:
        """Accept the terms of use for this profile.

        Idempotent on Saathi's side, but callers should check
        `has_accepted_tnc` first rather than calling it every turn.
        """
        self._request("PATCH", ACCEPT_TNC_PATH, json={"profile_id": int(profile_id)})

    def generate_session(self) -> str:
        data = self._request("GET", GENERATE_SESSION_PATH)
        session_id = data.get("sessionid")
        if not session_id:
            raise SaathiError("generate_session: response missing 'sessionid'")
        return str(session_id)

    def is_session_completed(self, session_id: str) -> bool:
        """Whether Saathi considers this conversation finished.

        The per-turn `finish_reason` on the WebSocket is NOT this signal -- it
        fires at the end of every bot turn. Only this poll is authoritative.
        """
        data = self._request("GET", CHAT_PATH, params={"session": session_id})
        results = data.get("results", [])
        if not results:
            return False
        return results[-1].get("status") == "COMPLETED"

    def recent_chat(self, session_id: str, profile_id: str, tail: int = 10) -> list[ChatRow]:
        """The tail of this session's transcript, oldest first.

        Reuses Mitra's `ChatRow` because the endpoint and its payload are the
        same Django view. Used to reconcile a turn that timed out.
        """
        head = self._request("GET", CHAT_PATH, params={"session": session_id, "limit": 1})
        count = int(head.get("count") or 0)
        if count == 0:
            return []

        offset = max(0, count - tail)
        page = self._request(
            "GET", CHAT_PATH, params={"session": session_id, "limit": tail, "offset": offset},
        )

        rows: list[ChatRow] = []
        for raw in page.get("results", []):
            sender_id = str(((raw.get("sender") or {}).get("id", "")))
            rows.append(ChatRow(
                id=int(raw.get("id") or 0),
                from_user=(sender_id == str(profile_id)),
                message=str(raw.get("message") or ""),
                translated_message=str(raw.get("translated_message") or ""),
                stage=str(raw.get("stage") or ""),
            ))
        rows.sort(key=lambda r: r.id)
        return rows

    # ---- transport --------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> dict:
        """One request, with a single automatic re-auth.

        ON 401, EXACTLY ONE RETRY. ELEVATE ends a token's session server-side,
        so a 401 is an expected mid-life event rather than a bug -- but a second
        401 means the credentials themselves are wrong, and retrying further
        would spin against the login endpoint.
        """
        response = self._send(method, path, json=json, params=params)

        if response.status_code == 401:
            logger.info(
                "saathi: token rejected, re-minting and retrying once",
                extra={"path": path, "mechanism": getattr(self._tokens, "mechanism", None)},
            )
            self._tokens.invalidate()
            # Raises SaathiAuthError under the `token` mechanism, which has
            # nothing to re-mint from -- deliberately, and with a message that
            # names the mechanism.
            response = self._send(method, path, json=json, params=params)
            if response.status_code == 401:
                raise SaathiAuthError(
                    f"Saathi {method} {path} still returned 401 after re-authenticating.",
                    mechanism=getattr(self._tokens, "mechanism", None),
                )

        if not response.ok:
            raise SaathiHTTPError(method, path, response.status_code, _safe_detail(response))

        try:
            return response.json()
        except Exception as exc:  # noqa: BLE001
            raise SaathiError(f"Saathi {method} {path} returned a non-JSON body") from exc

    def _send(self, method: str, path: str, *, json=None, params=None):
        url = f"{self._base_url}{path}"
        headers = {"X-auth-token": self._tokens.get()}
        if json is not None:
            headers["Content-Type"] = "application/json"
        try:
            return self._session.request(
                method, url, json=json, params=params, headers=headers,
                timeout=self._timeout,
                # Redirects are unexpected here; following one would replay the
                # credentials at whatever host the redirect names.
                allow_redirects=False,
            )
        except requests.exceptions.RequestException as exc:
            # The URL is omitted: query params echo a session id.
            raise SaathiError(f"Network error calling Saathi {method} {path}") from exc


def _safe_detail(response) -> str:
    """A short error detail. Never the whole body, never a credential."""
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        return ""
    for key in ("message", "detail", "error_message"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value[:200]
    return ""
