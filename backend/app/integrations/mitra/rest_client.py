"""MitraRestClient — typed REST adapter for the five Mitra HTTP endpoints.

SECURITY NOTICE — THE ORIGIN HEADER IS A CREDENTIAL
====================================================
Mitra's Django backend (chatbot/consumers/async_consumer.py) gates WebSocket
admission on the HTTP `Origin` header. The REST endpoints share the same
admission check. Sending `Origin: <MITRA_ORIGIN_URL>` plus a browser
User-Agent is *required* for any request to succeed, and is technically
*impersonation of a trusted browser client*.

This is a known workaround, not a correct server-to-server credential. It is
treated as a secret:

  - Lives ONLY in MITRA_ORIGIN_URL env var (never in YAML, never in DB)
  - Built into ``_fixed_headers`` once at construction time
  - ``_fixed_headers`` is NEVER printed, logged, or included in exceptions
  - ``__repr__`` is overridden to omit it

The correct long-term fix is a server-to-server credential from the Mitra
team. File that as a follow-up. DO NOT remove this header "to clean up" —
without it every request returns 403.

See also: design doc §13.2.

ENDPOINT CONTRACT (§1.8)
========================
All five methods in this module correspond to steps in the §1.8 table:

  upsert_profile       POST /api/profile/              step 1
  generate_session     GET  /api/generate-session/     step 2
  is_session_completed GET  /api/companychat/          step 7
  finalize             POST <spec.remote.finalize_path> step 8
  get_report           GET  /api/get-story/            step 9

WHICH FINALIZE ENDPOINT (v1 vs v2)
==================================
The two end-story endpoints resolve the story bot by DIFFERENT mechanisms,
and that -- not the token placement -- is what decides which one a flow may
use (verified against mitra-service source):

  /api/end-story/v2/  generate_story() -> get_story_company_bot_simple(flow)
                      -> Flow.objects.get(flow_route=flow)
                      Requires a row in Mitra's Flow table keyed on
                      flow_route == flow.

  /api/end-story/     create_story_object() -> get_story_company_bot(profile, flow)
                      -> CompanyBot.objects.get(route='/guest-story') etc.,
                      branching on the SessionFlowName enum. No Flow row
                      needed.

A flow with no Flow row 500s on v2 -- `Flow.objects.get` raises
Flow.DoesNotExist, which get_story_company_bot_simple re-raises as DRF
NotFound, which end_story_v2's `except Flow.DoesNotExist` does NOT match, so
it lands in the view's generic `except Exception` -> HTTP 500. It is a
CONFIGURATION error reported as a server error; it is deterministic, not a
race. Which endpoint each agent uses is therefore per-agent config
(``spec.remote.finalize_path``), not a global constant.

THE ENDPOINT ALSO SELECTS THE PDF RENDERER
==========================================
The bot-resolution difference above is not the only consequence, and for
`guest-discussion` it is not even the important one. The two views run two
different story-and-PDF pipelines, and only v1 knows the chaupal (discussion)
flow exists:

  v1  create_story_object: flow == GuestDiscussion -> save_chaupal_report
      -> save_shikshalokam_story -> get_story_html -> get_mom_report_html
      = the populated minutes-of-meeting report.

  v2  generate_story: NO chaupal branch. Always save_generic_story
      -> save_project_story -> get_html_from_template, which returns ""
      when no PDFTemplates row matches the flow -- and save_project_story
      passes that "" to Gotenberg, which renders an empty page and returns
      HTTP 200.

So a v2 finalisation of a chaupal flow yields a story, a StoryMedia row, a
200 from get-story and a downloadable file that is COMPLETELY BLANK, with
nothing logged anywhere. Choosing the endpoint is choosing the renderer.

WHY ``as_guest`` EXISTS
=======================
Mitra derives ``auth = access_token is not None`` and uses it to pick the PDF
template's ``user_type`` (AUTH vs GUEST). Presenting a token on a guest flow
therefore looks up a template nobody registered and lands in the same empty
-> blank-PDF branch. It must match what MitraChannel._authenticate sends on
the socket (``access_token: None``): interviewing as a guest and finalising as
an authenticated user is the mismatch, not either half. Per-agent via
``spec.remote.finalize_as_guest``.
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

# Mitra's own endpoint paths, appended to the connection's base URL. A
# third-party API contract rather than a preference -- change them when Mitra
# moves an endpoint, not to suit a deployment. Overridable per agent through
# `remote.connection.paths`; app/domain/agent_spec.py mirrors these defaults
# (it cannot import them -- the domain layer is import-pure) and
# tests/unit/mitra/test_paths_defaults.py asserts the two stay in step.
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
        """Trailing slashes vary between the YAML and the configured value;
        compare on the normalised path so `/api/end-story/v2` and
        `/api/end-story/v2/` do not disagree about where the token goes."""
        return path.strip("/") == self.finalize_v2.strip("/")

    def is_known_finalize(self, path: str) -> bool:
        """True iff `path` is one of the configured finalize endpoints.

        `RemoteSpec.finalize_path` is a plain string (the domain layer cannot
        import Settings), so this is what POST /api/agents/{key}/config checks
        to catch a YAML path that matches neither endpoint -- which would
        otherwise fall through to the v1 branch and finalize with the wrong
        body shape.
        """
        return path.strip("/") in {self.finalize_v1.strip("/"), self.finalize_v2.strip("/")}


class MitraRestClient:
    """Thin, typed HTTP client for the Mitra REST surface.

    Instantiate once per process (or once per request — it is stateless).
    All I/O is synchronous; the class has no background threads.

    Args:
        base_url:        MITRA_BASE_URL — e.g. "https://mitra.example.com"
        origin_url:      MITRA_ORIGIN_URL — sent as ``Origin`` on every call.
                         **Credential: never log, never print, never raise.**
        user_agent:      MITRA_USER_AGENT — browser UA string.
        allowed_hosts:   Extra FQDNs whose presigned URLs may be fetched.
                         The hostname from ``base_url`` is always included.
        connect_timeout: Seconds to wait for TCP connection.
        read_timeout:    Seconds to wait for the first response byte.
        paths:           Mitra's endpoint paths (MITRA_*_PATH). Defaults to the
                         shipped contract; only a Mitra-side move needs these.
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

        # ----------------------------------------------------------------
        # SECURITY: _fixed_headers contains the Origin credential.
        # This attribute must NEVER appear in logs, __repr__, or exceptions.
        # See the module-level docstring for full rationale.
        # ----------------------------------------------------------------
        self._fixed_headers: dict[str, str] = {
            "Origin": origin_url,
            "User-Agent": user_agent,
            "Accept": "application/json",
        }

        # One reusable session — shares connection pool, applies fixed headers.
        self._session: HTTPSession = requests.Session()
        self._session.headers.update(self._fixed_headers)

    def __repr__(self) -> str:
        # Intentionally omits _fixed_headers to prevent the Origin credential
        # from leaking into log messages that repr objects.
        return f"MitraRestClient(base_url={self._base_url!r})"

    # ------------------------------------------------------------------
    # Public API — five methods, matching §1.8 steps 1, 2, 7, 8, 9
    # ------------------------------------------------------------------

    def upsert_profile(self, email: str, latest_flow_used: str, company: str) -> str:
        """Create or retrieve the Mitra profile for this user.

        POST /api/profile/ → returns the profile id.

        ``company`` comes from the AGENT's own ``remote.company`` -- per agent and
        per tenant, not a global. ``email`` must be the *derived* email from module 2.4
        (``user_id + JWT_EMAIL_SUFFIX``). A different derivation produces a
        second profile and splits the user's story history. §1.7 trap 1.

        In the static-token POC the same user is always looked up and Mitra
        returns their existing profile id — this is idempotent.

        Returns:
            profile_id (str)
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
