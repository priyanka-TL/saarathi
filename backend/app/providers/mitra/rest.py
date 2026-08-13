"""Mitra's REST surface: the five endpoints, and nothing else.

Responsible for: profile upsert, session creation, completion polling,
finalisation and report fetch.
Used by: MitraProvider, one client per resolved connection.

Transport -- the session, the fixed headers, the redirect refusal, the safe error
detail and the SSRF check -- lives in `app/providers/transport/http.py`. What is
left here is the part that is genuinely Mitra: which paths, which payload keys,
and which response key carries the answer.

THE ORIGIN HEADER IS A CREDENTIAL and is held by the transport, which never
logs, reprs or raises it.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.providers.errors import ProviderError
from app.providers.recovery import ChatRow
from app.providers.transport.http import RestTransport
from app.providers.mitra.spec import MitraPaths


class MitraRestClient:
    """Synchronous client for the Mitra REST surface. Stateless, no threads."""

    def __init__(self, transport: RestTransport, paths: MitraPaths) -> None:
        self._http = transport
        self.paths = paths

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def upsert_profile(
        self,
        email: str,
        latest_flow_used: str,
        company: str,
        extra: Optional[Dict[str, str]] = None,
    ) -> str:
        """Create or retrieve this user's Mitra profile. Idempotent.

        `email` MUST be the derived email (user_id + JWT_EMAIL_SUFFIX): a
        different derivation creates a SECOND profile and splits the user's story
        history. `company` is per-agent, not a global.

        `extra` carries additional Profile columns -- who the user is, rather
        than which profile this is. Only agents with
        `remote.options.send_user_profile` supply it; for everyone else the body
        is byte-for-byte what it has always been, which is what keeps a second
        agent on this same client unaffected.

        THE THREE IDENTIFYING KEYS ARE WRITTEN LAST and therefore win. Mitra
        resolves the profile by (email, company) and `extra` is assembled from a
        different source, so letting it reach either key would move the write to
        a DIFFERENT profile -- silently, since the response shape is identical.

        BLANK VALUES ARE DROPPED, NOT SENT AS "". Mitra runs a NON-partial
        serializer over an existing profile, so an omitted key keeps its stored
        value while an empty string overwrites a good one with a blank.
        """
        payload: Dict[str, Any] = {
            key: value
            for key, value in (extra or {}).items()
            if value is not None and str(value).strip()
        }
        payload.update(
            {"email": email, "latest_flow_used": latest_flow_used, "company": company}
        )

        data = self._http.request("POST", self.paths.profile, json=payload)
        # The response has ``id``; ``profileid`` is a legacy fallback key.
        profile_id = data.get("id") or data.get("profileid")
        if not profile_id:
            raise ProviderError(
                "upsert_profile: response missing both 'id' and 'profileid' keys"
            )
        return str(profile_id)

    def generate_session(self) -> str:
        """Allocate a new Mitra session id."""
        data = self._http.request("GET", self.paths.generate_session)
        session_id = data.get("sessionid")
        if not session_id:
            raise ProviderError("generate_session: response missing 'sessionid' key")
        return str(session_id)

    def is_session_completed(self, session_id: str) -> bool:
        """Whether the last CompanyChat row reports COMPLETED.

        The per-turn `finish_reason` on the WebSocket is NOT this signal -- it
        fires at the end of every bot turn. Only this poll is authoritative.

        PAGINATED, for the same reason `recent_chat` below is. A bare GET
        returns the FIRST page, so `results[-1]` is the last row of page one --
        not the last row of the conversation. Past the page size the status
        being read is an OLD row's, and the session can never be seen to
        complete: no artifact, the session stuck open, nothing logged. Masked
        today only because a flow finishes well inside one page.

        Two requests, like `recent_chat`: one to learn `count`, one to fetch the
        true tail.
        """
        head = self._http.request(
            "GET", self.paths.chat, params={"session": session_id, "limit": 1},
        )
        count = int(head.get("count") or 0)
        if count == 0:
            return False

        tail = self._http.request(
            "GET", self.paths.chat,
            params={"session": session_id, "limit": 1, "offset": count - 1},
        )
        results = tail.get("results", [])
        if not results:
            return False
        return results[-1].get("status") == "COMPLETED"

    def recent_chat(
        self, session_id: str, profile_id: str, tail: int = 10,
    ) -> list[ChatRow]:
        """The last ``tail`` CompanyChat rows, oldest first.

        PAGINATION MATTERS HERE. Mitra uses LimitOffsetPagination with
        PAGE_SIZE=100, so a bare GET returns the FIRST 100 rows -- the oldest
        ones. Reading ``results[-1]`` off that page silently stops being "the
        latest message" once an interview passes 100 rows, so this asks for
        ``count`` first and then offsets to the true tail.

        Direction comes from ``sender.id`` vs the caller's own profile id rather
        than a hardcoded AI profile, so it stays correct across environments.
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

        ``path`` comes from `remote.options.finalize_path` and selects the
        endpoint -- see the spec module for why that is per-agent.

        v2 reads the token from the ``Authorization: Bearer`` header; v1 reads it
        from the request body (``access_token``). The placement follows ``path``
        automatically: sending the v1 body shape to v2 leaves the call
        unauthenticated and vice versa, so these two must never be set
        separately.

        ``as_guest`` suppresses the token entirely for flows Mitra treats as
        guest flows. ``token`` is still accepted and simply unused, so callers
        need no branching.

        Idempotency: ``Story.session`` is UNIQUE in Mitra's database, so calling
        this twice for one session fails on Mitra's side. The `finalizing` claim
        in SessionService is Saarthi's own guard.
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

        data = self._http.request("POST", path, json=payload, extra_headers=extra_headers)
        story_id = str(data.get("id", ""))
        content = str(data.get("content", ""))
        if not story_id:
            raise ProviderError("finalize: response missing 'id' (story id) key")
        return story_id, content

    def get_report(self, session_id: str, media_type: str) -> Optional[str]:
        """The generated report URL for a completed session, if it exists yet.

        Walks ``results[0].story_media[]`` for the entry matching ``media_type``.
        The URL is validated (https + allowlist) before being returned. Returns
        None when generation is still lagging -- poll until non-None.
        """
        data = self._http.request(
            "GET", self.paths.get_story, params={"session": session_id},
        )
        results = data.get("results", [])
        if not results:
            return None
        story_media = results[0].get("story_media", [])
        for entry in story_media:
            if entry.get("media_type") == media_type:
                url = entry.get("public_url")
                if url:
                    self._http.validate_url(url)  # raises ProviderSSRFError
                    return url
        return None
