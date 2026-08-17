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

EVERY REQUEST CARRIES TWO CREDENTIALS: the `Origin` header (Saathi gates on it,
fixed on the transport) and `X-auth-token` (the per-user ELEVATE JWT, passed
into EVERY method here explicitly). Neither is ever logged, repr'd or included
in an exception.

THE TOKEN IS A PER-CALL ARGUMENT, NOT TRANSPORT STATE. It varies by which
logged-in user's turn this is, and the transport (and the SaathiProvider
instance holding it) is shared across every conversation on this connection --
so there is nothing to cache here and nothing to invalidate on a 401. A 401
means that specific user's ELEVATE session ended; it propagates as a plain
ProviderHTTPError carrying ELEVATE's own message rather than triggering a retry.
"""
from __future__ import annotations

from typing import Optional, Tuple

from app.providers.errors import ProviderError
from app.providers.protocol import CompletionPoll
from app.providers.recovery import ChatRow
from app.providers.saathi.spec import SaathiPaths
from app.providers.transport.http import RestTransport

#: The per-request credential header Saathi's REST surface expects. Matches
#: app/providers/saathi/provider.py::AUTH_HEADER.
AUTH_HEADER = "X-auth-token"

#: Saathi's DRF LimitOffsetPagination page size -- the same Django app Mitra
#: runs, behind a different route (see `spec.py`), so the same server-side value.
#: Declared here rather than imported from the other platform's package: these
#: two are PEERS and `.importlinter` forbids either importing the other, which is
#: what keeps one platform's server change from silently altering the other's.
PAGE_SIZE = 100


def _auth(token: str) -> dict:
    return {AUTH_HEADER: token}


class SaathiRestClient:
    """Synchronous client for the Saathi REST surface. Stateless, no threads."""

    def __init__(self, transport: RestTransport, paths: SaathiPaths) -> None:
        self._http = transport
        self.paths = paths

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def read_profile(self, token: str) -> Tuple[str, bool]:
        """This token's Saathi profile.

        :returns: ``(profile_id, has_accepted_tnc)``.
        """
        data = self._http.request("GET", self.paths.profile, extra_headers=_auth(token))
        details = data.get("profile_details") or {}
        profile_id = details.get("profileid")
        if not profile_id:
            raise ProviderError("read_profile: response carried no 'profileid'")
        return str(profile_id), bool(details.get("has_accepted_tnc"))

    def accept_tnc(self, profile_id: str, token: str) -> None:
        """Accept the terms of use for this profile.

        Idempotent on Saathi's side, but callers should check
        `has_accepted_tnc` first rather than calling it every turn.
        """
        self._http.request(
            "PATCH", self.paths.accept_tnc,
            json={"profile_id": int(profile_id)}, extra_headers=_auth(token),
        )

    def generate_session(self, token: str) -> str:
        data = self._http.request(
            "GET", self.paths.generate_session, extra_headers=_auth(token),
        )
        session_id = data.get("sessionid")
        if not session_id:
            raise ProviderError("generate_session: response missing 'sessionid'")
        return str(session_id)

    def is_session_completed(
        self, session_id: str, token: str, known_count: Optional[int] = None,
    ) -> CompletionPoll:
        """Whether Saathi considers this conversation finished.

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
        one to learn `count`, one to seek to it. `saathi` is the slowest remote
        agent on record, and this was ~350 ms of every one of its turns.

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
        page would drag the whole transcript back, i.e. worse than the two small
        calls, so a first poll asks for `count` cheaply and seeks, as before.

        DELIBERATELY THE SAME ALGORITHM AS MITRA'S, NOT A SHARED ONE. Both
        platforms are the same Django app today, but they are peers: neither
        package may import the other, so a divergence on one server is a change
        to one file rather than a silent change to two agents.
        """
        offset = max(0, known_count - 1) if known_count else None

        if offset is None:
            # No cache: learn `count` with a one-row page, then seek.
            head = self._http.request(
                "GET", self.paths.chat,
                params={"session": session_id, "limit": 1}, extra_headers=_auth(token),
            )
            count = int(head.get("count") or 0)
            if count == 0:
                return CompletionPoll(done=False, count=0)
            offset = count - 1

        page = self._page(session_id, offset, token)
        count = int(page.get("count") or 0)
        if count == 0:
            return CompletionPoll(done=False, count=0)

        results = page.get("results") or []
        if offset + len(results) != count:
            # The cache was too far behind to reach the tail in one page. Seek
            # again with the count this response just told us.
            page = self._page(session_id, count - 1, token)
            results = page.get("results") or []
            count = int(page.get("count") or count)

        if not results:
            return CompletionPoll(done=False, count=count)
        return CompletionPoll(
            done=results[-1].get("status") == "COMPLETED", count=count,
        )

    def _page(self, session_id: str, offset: int, token: str) -> dict:
        """One page of the transcript from `offset`, a full page wide.

        `PAGE_SIZE` rather than 1 is what lets a stale offset still reach the
        tail: the rows appended since the cached count was taken are in the same
        response. The payload stays small in the steady state because only a
        handful of rows sit past the offset.
        """
        return self._http.request(
            "GET", self.paths.chat,
            params={"session": session_id, "limit": PAGE_SIZE, "offset": offset},
            extra_headers=_auth(token),
        )

    def recent_chat(
        self, session_id: str, profile_id: str, token: str, tail: int = 10,
    ) -> list[ChatRow]:
        """The tail of this session's transcript, oldest first.

        Paginated the same way Mitra's is, and for the same reason: a bare GET
        returns the FIRST page, so reading ``results[-1]`` off it stops being
        "the latest message" once a conversation passes the page size.
        """
        head = self._http.request(
            "GET", self.paths.chat,
            params={"session": session_id, "limit": 1}, extra_headers=_auth(token),
        )
        count = int(head.get("count") or 0)
        if count == 0:
            return []

        offset = max(0, count - tail)
        page = self._http.request(
            "GET", self.paths.chat,
            params={"session": session_id, "limit": tail, "offset": offset},
            extra_headers=_auth(token),
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
