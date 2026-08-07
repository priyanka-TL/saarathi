"""MitraRestClient -- the five Mitra REST endpoints.

Responsible for: profile upsert, session creation, completion polling,
finalisation and report fetch.
Used by: OrchestrationService and TurnFinalizer, one client per connection.

THE ORIGIN HEADER IS A CREDENTIAL. Mitra gates admission on it, so it lives only
in env, is built into `_fixed_headers` once, and must never be logged, printed
or included in an exception -- `__repr__` is overridden for that reason. Without
it every request is a 403; do not remove it "to clean up".

WHICH FINALIZE ENDPOINT (v1 vs v2) IS PER-AGENT CONFIG, NOT A CONSTANT, and the
choice has two independent consequences:

  * BOT RESOLUTION. v2 requires a row in Mitra's Flow table keyed on the flow
    route; a flow without one is a deterministic HTTP 500.
  * PDF RENDERER. Only v1 knows the chaupal (discussion) flow exists. A v2
    finalisation of a chaupal flow returns a story, a StoryMedia row, a 200 from
    get-story and a downloadable file that is COMPLETELY BLANK, with nothing
    logged anywhere.

`as_guest` exists for the same class of silent failure: Mitra derives
`auth = access_token is not None` and picks the PDF template's user_type from
it, so finalising with a token on a guest flow renders a blank PDF. It must
match what the socket authenticated as.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import requests
from requests import Session as HTTPSession

from app.domain.agent_spec import DEFAULT_REPORT_MEDIA_TYPE
from app.integrations.mitra.exceptions import (
    MitraError,
    MitraHTTPError,
    MitraRedirectError,
    MitraSSRFError,
)
from app.integrations.mitra.turn_recovery import ChatRow

# A third-party API contract, not a preference: change these when Mitra moves
# an endpoint. app/domain/agent_spec.py mirrors them (it cannot import them --
# the domain layer is import-pure) and a test asserts the two stay in step.
FINALIZE_V1_PATH = "/api/end-story/"
FINALIZE_V2_PATH = "/api/end-story/v2/"


@dataclass(frozen=True)
class MitraPaths:
    """The Mitra REST surface, in one place.

    Held by MitraRestClient so no request method carries a literal path.
    """

    profile: str = "/api/profile/"
    generate_session: str = "/api/generate-session/"
    chat: str = "/api/companychat/"
    get_story: str = "/api/get-story/"
    finalize_v1: str = FINALIZE_V1_PATH
    finalize_v2: str = FINALIZE_V2_PATH

    def is_v2_finalize(self, path: str) -> bool:
        """Compared on the normalised path, so a trailing slash cannot change
        where the token goes."""
        return path.strip("/") == self.finalize_v2.strip("/")

    def is_known_finalize(self, path: str) -> bool:
        """Whether `path` is one of the configured finalize endpoints.

        What the admin config route checks: a path matching neither would fall
        through to the v1 branch and finalise with the wrong body shape.
        """
        return path.strip("/") in {self.finalize_v1.strip("/"), self.finalize_v2.strip("/")}


class MitraRestClient:
    """Synchronous HTTP client for the Mitra REST surface. Stateless, no threads.

    :param origin_url: sent as `Origin` on every call. A CREDENTIAL -- never
        log, print or raise it.
    :param allowed_hosts: extra FQDNs whose presigned URLs may be fetched;
        base_url's own host is always included.
    :param paths: Mitra's endpoint paths; only a Mitra-side move needs these.
    """

    def __init__(
        self,
        base_url: str,
        origin_url: str,
        user_agent: str,
        allowed_hosts: list[str],
        # NO DEFAULTS, deliberately. MitraConnection (app/integrations/mitra/
        # connection.py) owns these numbers -- Mitra's timeouts are part of the
        # DB-backed remote spec, not .env, so the connection is the single place
        # they are declared and MitraClientRegistry.get() always passes them.
        connect_timeout: float,
        read_timeout: float,
        paths: Optional[MitraPaths] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = (connect_timeout, read_timeout)
        self.paths = paths or MitraPaths()

        # The base URL's own hostname is always trusted.
        base_host = urlparse(base_url).hostname or ""
        self._allowed_hosts: frozenset[str] = frozenset(
            h.strip().lower() for h in [base_host] + allowed_hosts if h.strip()
        )

        # SECURITY: holds the Origin credential. Must never appear in a log,
        # a __repr__ or an exception.
        self._fixed_headers: dict[str, str] = {
            "Origin": origin_url,
            "User-Agent": user_agent,
            "Accept": "application/json",
        }

        # One reusable session: shared pool, fixed headers applied once.
        self._session: HTTPSession = requests.Session()
        self._session.headers.update(self._fixed_headers)

    def __repr__(self) -> str:
        # Omits _fixed_headers so the Origin credential cannot leak into a log
        # line that reprs this object.
        return f"MitraRestClient(base_url={self._base_url!r})"

    # ------------------------------------------------------------------
    # Public API — five methods, matching §1.8 steps 1, 2, 7, 8, 9
    # ------------------------------------------------------------------

    def upsert_profile(self, email: str, latest_flow_used: str, company: str) -> str:
        """Create or retrieve this user's Mitra profile. Idempotent.

        `email` MUST be the derived email (user_id + JWT_EMAIL_SUFFIX): a
        different derivation creates a SECOND profile and splits the user's
        story history. `company` is per-agent, not a global.
        """
        data = self._request(
            "POST",
            self.paths.profile,
            json={"email": email, "latest_flow_used": latest_flow_used, "company": company},
        )
        # §1.8 — response has ``id``; ``profileid`` is a legacy fallback key.
        profile_id = data.get("id") or data.get("profileid")
        if not profile_id:
            raise MitraError("upsert_profile: response missing both 'id' and 'profileid' keys")
        return str(profile_id)

    def generate_session(self) -> str:
        """Allocate a new Mitra session id.

        GET /api/generate-session/ → returns ``sessionid``.

        Returns:
            session_id (str)
        """
        data = self._request("GET", self.paths.generate_session)
        session_id = data.get("sessionid")
        if not session_id:
            raise MitraError("generate_session: response missing 'sessionid' key")
        return str(session_id)

    def is_session_completed(self, session_id: str) -> bool:
        """Poll whether the Mitra session has reached COMPLETED state.

        GET /api/companychat/?session=<id> → inspects results[-1].status.

        IMPORTANT: The per-turn ``finish_reason`` on the WebSocket is NOT a
        completion signal — it fires at the end of every bot turn (§1.3).
        Only this REST poll is authoritative for interview completion (§1.8
        step 7).

        Returns:
            True if the last CompanyChat row has status == "COMPLETED".
        """
        data = self._request("GET", self.paths.chat, params={"session": session_id})
        results = data.get("results", [])
        if not results:
            return False
        return results[-1].get("status") == "COMPLETED"

    def recent_chat(
        self, session_id: str, profile_id: str, tail: int = 10
    ) -> list[ChatRow]:
        """Return the last ``tail`` CompanyChat rows, oldest first.

        Used to reconcile a turn Saarthi stopped listening for, without
        re-sending it (see turn_recovery).

        PAGINATION MATTERS HERE. Mitra uses LimitOffsetPagination with
        PAGE_SIZE=100 (settings.py:430), so a bare GET returns the FIRST 100
        rows -- the oldest ones. Reading ``results[-1]`` off that page silently
        stops being "the latest message" once an interview passes 100 rows, so
        this asks for ``count`` first and then offsets to the true tail.

        Direction comes from ``sender.id`` vs the caller's own profile id rather
        than a hardcoded AI profile, so it stays correct across environments.
        """
        head = self._request(
            "GET", self.paths.chat, params={"session": session_id, "limit": 1},
        )
        count = int(head.get("count") or 0)
        if count == 0:
            return []

        offset = max(0, count - tail)
        page = self._request(
            "GET", self.paths.chat,
            params={"session": session_id, "limit": tail, "offset": offset},
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

    def finalize(
        self,
        session_id: str,
        profile_id: str,
        flow: str,
        language: str,
        token: str,
        path: Optional[str] = None,
        as_guest: bool = False,
    ) -> tuple[str, str]:
        """Submit the completed session for story synthesis.

        ``path`` comes from ``spec.remote.finalize_path`` and selects the
        endpoint — see "WHICH FINALIZE ENDPOINT" and "THE ENDPOINT ALSO SELECTS
        THE PDF RENDERER" in the module docstring for why that is per-agent and
        not a constant.

        IMPORTANT — v2 vs v1 difference (§1.5):
          v2 reads the token from the ``Authorization: Bearer`` header.
          v1 reads it from the request body (``access_token``).
        The token placement follows ``path`` automatically. Sending the v1
        body shape to v2 leaves the call unauthenticated (v2 never reads the
        body key) and vice versa, so these two must never be set separately.

        ``as_guest`` (from ``spec.remote.finalize_as_guest``) suppresses the
        token entirely — ``access_token: null`` in the v1 body, no
        ``Authorization`` header on v2 — for flows Mitra treats as guest
        flows. ``token`` is still accepted and simply unused, so callers need
        no branching. See "WHY ``as_guest`` EXISTS" in the module docstring:
        this is a PDF-template selector on Mitra's side, not an auth decision.

        Returns:
            (story_id, content) where story_id is the Mitra Story.id.

        Note on idempotency (§1.5): ``Story.session`` is UNIQUE in Mitra's
        database, so calling this twice for the same session will fail on
        Mitra's side. The ``agent_sessions.state = 'finalizing'`` claim in
        ``SessionService`` is Saarthi's guard that prevents double-submission.
        """
        path = path or self.paths.finalize_v2

        payload = {
            "session": session_id,
            "profile_id": profile_id,
            "stage": "COMPLETED",
            "flow": flow,
            "language": language,
        }
        extra_headers = None
        if self.paths.is_v2_finalize(path):
            if not as_guest:
                extra_headers = {"Authorization": f"Bearer {token}"}
        else:
            # Explicit null, not an omitted key: this mirrors the Node client's
            # body byte-for-byte, and v1 reads the key with .get() either way.
            payload["access_token"] = None if as_guest else token

        data = self._request("POST", path, json=payload, extra_headers=extra_headers)
        story_id = str(data.get("id", ""))
        content = str(data.get("content", ""))
        if not story_id:
            raise MitraError("finalize: response missing 'id' (story id) key")
        return story_id, content

    def get_report(
        self, session_id: str, media_type: str = DEFAULT_REPORT_MEDIA_TYPE,
    ) -> Optional[str]:
        """Fetch the generated report URL for a completed session.

        GET /api/get-story/?session=<id> → walks results[0].story_media[],
        finds the entry matching ``media_type``, and returns ``public_url``.

        The URL is validated (https + allowlist) before being returned.
        Returns None if the story_media list is empty or no matching entry
        exists (report generation may lag — poll until non-None).

        Returns:
            Validated public_url (str), or None if not yet generated.
        """
        data = self._request("GET", self.paths.get_story, params={"session": session_id})
        results = data.get("results", [])
        if not results:
            return None
        story_media = results[0].get("story_media", [])
        for entry in story_media:
            if entry.get("media_type") == media_type:
                url = entry.get("public_url")
                if url:
                    self._validate_url(url)  # raises MitraSSRFError on failure
                    return url
        return None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> dict:
        """Execute one HTTP request and return the parsed JSON body.

        Raises:
            MitraRedirectError: on any 3xx (redirects disabled by design).
            MitraHTTPError:     on any non-2xx, non-3xx status.
            MitraError:         on network errors or non-JSON responses.
        """
        url = f"{self._base_url}{path}"
        headers = {}
        if extra_headers:
            headers.update(extra_headers)

        try:
            response = self._session.request(
                method,
                url,
                json=json,
                params=params,
                headers=headers,
                timeout=self._timeout,
                allow_redirects=False,  # §13.2 — redirects are unexpected; raise loudly
            )
        except requests.exceptions.RequestException as exc:
            # Do NOT include `url` in the message — it could contain query
            # params that echo a session_id, though not the Origin credential.
            raise MitraError(f"Network error calling Mitra {method} {path}") from exc

        if 300 <= response.status_code < 400:
            raise MitraRedirectError(response.status_code)

        if not response.ok:
            raise MitraHTTPError(
                method, path, response.status_code,
                detail=self._extract_error_detail(response),
            )

        try:
            return response.json()
        except Exception as exc:
            raise MitraError(f"Mitra {method} {path} returned non-JSON body") from exc

    def _extract_error_detail(self, response) -> Optional[str]:
        """Pull Mitra's own error text out of a failed response, safely.

        SECURITY (§13.2): the raw body is NEVER returned. A 403 from the edge
        can reflect the ``Origin`` credential back in its body, so this reads
        only the three known keys of Mitra's JSON error envelope, truncates
        them, and drops the result entirely if the credential appears in it
        anyway. Non-JSON bodies (HTML error pages, proxy output) yield None.
        """
        try:
            body = response.json()
        except Exception:
            return None
        if not isinstance(body, dict):
            return None

        parts = [
            str(body[key])
            for key in ("error_message", "error_type", "detail")
            if body.get(key)
        ]
        if not parts:
            return None

        detail = " | ".join(parts)[:300]
        # Read the credential from its single home rather than keeping a
        # second copy of it on the instance.
        origin = self._fixed_headers.get("Origin")
        if origin and origin in detail:
            return None
        return detail

    def _validate_url(self, url: str) -> None:
        """Assert that a URL returned by Mitra is safe to use.

        Checks:
          1. Scheme must be ``https`` (prevents http downgrade).
          2. Hostname must be in the allowlist (prevents SSRF via a
             compromised Mitra response redirecting Saarthi to an
             internal host).

        Does NOT include the URL in the raised exception (§13.2 SSRF note).
        """
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise MitraSSRFError()
        host = (parsed.hostname or "").lower()
        if host not in self._allowed_hosts:
            raise MitraSSRFError()


# NOTE: the old `from_settings(settings) -> MitraRestClient` factory and its
# `paths_from_settings` companion are both gone. A client is not a function of
# Settings at all any more -- its base URL, timeouts, paths and Origin
# credential resolve per agent and per tenant. Build a MitraConnection
# (app/integrations/mitra/connection.py) from the agent spec and ask
# MitraClientRegistry for the client, which also shares one connection pool per
# distinct configuration instead of one per caller.
