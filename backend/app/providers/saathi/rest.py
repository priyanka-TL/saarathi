"""Saathi's REST surface.

Responsible for: profile resolution, the terms-of-use gate, session creation and
completion polling.
Used by: SaathiProvider, one client per resolved connection.

WHAT IS NOT HERE, AND WHY:

  * `upsert_profile` -- Saathi derives the profile from the ACCESS TOKEN, so
    there is no (email, company) upsert and no email-derivation trap.
  * `finalize` / `get_report` -- the saathi flow reports `create_story: "none"`;
    there is no story to submit and no PDF to fetch. Adding them "for symmetry"
    would invite a caller to finalise a flow that cannot, which fails by marking
    the session `failed`.

EVERY REQUEST CARRIES TWO CREDENTIALS: the `Origin` header (Saathi gates on it)
and `X-auth-token` (the per-user ELEVATE JWT). Neither is ever logged, repr'd or
included in an exception -- the transport owns both.

A 401 IS HANDLED ONCE, by the transport's `on_unauthorized` hook: the token
provider is invalidated and the request is retried with a freshly minted token.
A second 401 propagates.
"""
from __future__ import annotations

from typing import Tuple

from app.providers.errors import ProviderError
from app.providers.recovery import ChatRow
from app.providers.saathi.spec import SaathiPaths
from app.providers.transport.http import RestTransport


class SaathiRestClient:
    """Synchronous client for the Saathi REST surface. Stateless, no threads."""

    def __init__(self, transport: RestTransport, paths: SaathiPaths) -> None:
        self._http = transport
        self.paths = paths

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def read_profile(self) -> Tuple[str, bool]:
        """This token's Saathi profile.

        :returns: ``(profile_id, has_accepted_tnc)``.
        """
        data = self._http.request("GET", self.paths.profile)
        details = data.get("profile_details") or {}
        profile_id = details.get("profileid")
        if not profile_id:
            raise ProviderError("read_profile: response carried no 'profileid'")
        return str(profile_id), bool(details.get("has_accepted_tnc"))

    def accept_tnc(self, profile_id: str) -> None:
        """Accept the terms of use for this profile.

        Idempotent on Saathi's side, but callers should check
        `has_accepted_tnc` first rather than calling it every turn.
        """
        self._http.request(
            "PATCH", self.paths.accept_tnc, json={"profile_id": int(profile_id)},
        )

    def generate_session(self) -> str:
        data = self._http.request("GET", self.paths.generate_session)
        session_id = data.get("sessionid")
        if not session_id:
            raise ProviderError("generate_session: response missing 'sessionid'")
        return str(session_id)

    def is_session_completed(self, session_id: str) -> bool:
        """Whether Saathi considers this conversation finished.

        The per-turn `finish_reason` on the WebSocket is NOT this signal -- it
        fires at the end of every bot turn. Only this poll is authoritative.
        """
        data = self._http.request("GET", self.paths.chat, params={"session": session_id})
        results = data.get("results", [])
        if not results:
            return False
        return results[-1].get("status") == "COMPLETED"

    def recent_chat(
        self, session_id: str, profile_id: str, tail: int = 10,
    ) -> list[ChatRow]:
        """The tail of this session's transcript, oldest first.

        Paginated the same way Mitra's is, and for the same reason: a bare GET
        returns the FIRST page, so reading ``results[-1]`` off it stops being
        "the latest message" once a conversation passes the page size.
        """
        head = self._http.request(
            "GET", self.paths.chat, params={"session": session_id, "limit": 1},
        )
        count = int(head.get("count") or 0)
        if count == 0:
            return []

        offset = max(0, count - tail)
        page = self._http.request(
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
