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
from app.providers.protocol import CompletionPoll
from app.providers.recovery import ChatRow
from app.providers.transport.http import RestTransport
from app.providers.mitra.spec import MitraPaths

#: Mitra's DRF LimitOffsetPagination page size. NOT a tuning knob -- it is what
#: the server does, and asking for more returns this many anyway. Named because
#: two methods below depend on the value being the real one: `recent_chat`'s
#: docstring explains why a bare GET returns the OLDEST rows, and
#: `is_session_completed` uses a full page as its over-seek window, which only
#: reaches the tail from a stale offset if this is not smaller than the server's.
PAGE_SIZE = 100


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

    def is_session_completed(
        self, session_id: str, known_count: Optional[int] = None,
    ) -> CompletionPoll:
        """Whether the last CompanyChat row reports COMPLETED.

        The per-turn `finish_reason` on the WebSocket is NOT this signal -- it
        fires at the end of every bot turn. Only this poll is authoritative.

        PAGINATED, and it cannot be avoided. A bare GET returns the FIRST page,
        so `results[-1]` is the last row of page one -- not the last row of the
        conversation. Past the page size the status being read is an OLD row's,
        and the session can never be seen to complete: no artifact, the session
        stuck open, nothing logged.

        AN ORDERING PARAMETER WOULD MAKE THIS TRIVIAL, AND THERE ISN'T ONE.
        Verified against the live API: `ordering=-id`, `-created`, `-created_at`,
        `-timestamp` and `-pk` are all ignored -- every one returns row 0 while
        the true tail sits at `offset = count - 1`. So the last row is not
        directly addressable, and this used to cost TWO round trips every turn:
        one to learn `count`, one to seek to it. Measured at ~350 ms, on the
        critical path, on every turn of every interview.

        ONE REQUEST, BY OVER-SEEKING FROM THE LAST KNOWN COUNT. `known_count` is
        whatever this method returned last turn. Seeking to `known_count - 1`
        with a FULL PAGE of limit lands at-or-before the tail and sweeps up
        everything appended since -- so `results[-1]` is the true tail even
        though the offset was stale, which it always is (this platform appends
        roughly two rows per turn).

        SELF-VERIFYING, which is what makes a stale cache safe rather than
        merely likely-to-work: the response carries its own `count`, so
        `offset + len(results) == count` PROVES the tail was reached. When it
        does not -- more than a page appended since, or a cache from another
        session -- this re-seeks with the fresh count and pays the second call it
        used to pay every time. A stale cache costs a round trip; it never
        yields a wrong answer.

        FALLS BACK TO THE OLD TWO-CALL PATH WITH NO CACHE. `offset=0` with a full
        page would drag the whole transcript back (measured: 37 KB / 459 ms on a
        20-row session, i.e. WORSE than the two small calls), so a first poll
        asks for `count` cheaply and seeks, exactly as before.
        """
        offset = max(0, known_count - 1) if known_count else None

        if offset is None:
            # No cache: learn `count` with a one-row page, then seek.
            head = self._http.request(
                "GET", self.paths.chat, params={"session": session_id, "limit": 1},
            )
            count = int(head.get("count") or 0)
            if count == 0:
                return CompletionPoll(done=False, count=0)
            offset = count - 1

        page = self._page(session_id, offset)
        count = int(page.get("count") or 0)
        if count == 0:
            return CompletionPoll(done=False, count=0)

        results = page.get("results") or []
        if offset + len(results) != count:
            # The cache was too far behind to reach the tail in one page. Seek
            # again with the count this response just told us.
            page = self._page(session_id, count - 1)
            results = page.get("results") or []
            count = int(page.get("count") or count)

        if not results:
            return CompletionPoll(done=False, count=count)
        return CompletionPoll(
            done=results[-1].get("status") == "COMPLETED", count=count,
        )

    def _page(self, session_id: str, offset: int) -> dict:
        """One page of the transcript from `offset`, a full page wide.

        `PAGE_SIZE` rather than 1 is what lets a stale offset still reach the
        tail: the rows appended since the cached count was taken are in the same
        response. The payload stays small in the steady state because only a
        handful of rows sit past the offset (measured: ~2.2 KB / 159 ms).
        """
        return self._http.request(
            "GET", self.paths.chat,
            params={"session": session_id, "limit": PAGE_SIZE, "offset": offset},
        )

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
