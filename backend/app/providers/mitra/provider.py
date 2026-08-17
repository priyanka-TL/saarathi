"""The Mitra provider.

Responsible for: what is specific to Mitra -- an (email, company) profile upsert,
a guest socket, and finalisation into a story plus a PDF.
Used by: resolved through ProviderRegistry from `remote.provider = "mitra"`.

Everything else -- the channel pool, the turn loop, the one re-establishment
attempt, the completion poll, reconciliation -- comes from BaseWsFlowProvider,
because it belongs to the PROTOCOL and not to Mitra.

DOES NOT DECIDE WHEN TO FINALIZE. `end-story` plus report retrieval run under the
`finalizing` state claim, which only the transaction owner (TurnFinalizer) can
make. This class performs finalisation; it never triggers it.

DOES NOT SEE HISTORY. Delegated flows set `memory.strategy: none` -- Mitra
reconstructs interview state from its own records, and supplying history would
corrupt it. Nothing here takes a history argument, which is what structurally
guarantees that.
"""
from __future__ import annotations

from typing import ClassVar, Dict, Optional, Tuple

from app.core.logger import get_logger
from app.providers.errors import ProviderError
from app.providers.mitra.rest import MitraRestClient
from app.providers.mitra.spec import MitraOptions
from app.providers.protocol import FinalizeResult, SessionInit
from app.providers.registry import register_provider
from app.providers.transport.http import RestTransport
from app.providers.ws_flow.base import BaseWsFlowProvider

logger = get_logger("provider.mitra")


def _non_blank(fields: Dict[str, str]) -> Dict[str, str]:
    """Drop the keys with nothing in them.

    The REST client drops them again -- deliberately, since it is the layer that
    owns the wire body and a second caller must not be able to reintroduce the
    bug. Doing it here too keeps the logged/returned shape honest about what was
    actually resolved.
    """
    return {key: value for key, value in fields.items() if value and value.strip()}


@register_provider
class MitraProvider(BaseWsFlowProvider):
    """Mitra: a guest interview that finalises into a story and a PDF."""

    name: ClassVar[str] = "mitra"
    options_model: ClassVar[type] = MitraOptions
    produces_artifacts: ClassVar[bool] = True

    def _build_rest(self) -> MitraRestClient:
        conn = self._conn
        transport = RestTransport(
            provider="Mitra",
            base_url=conn.base_url,
            origin_url=conn.origin_url,
            user_agent=conn.user_agent,
            connect_timeout=conn.connect_timeout_s,
            read_timeout=conn.read_timeout_s,
            allowed_hosts=conn.allowed_hosts,
        )
        return MitraRestClient(transport, self.options.paths)

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def open_session(self, remote, session_view, user) -> SessionInit:
        """Upsert the profile and allocate a session, once per conversation.

        Idempotent after the first turn: a session that already has a remote id
        is handed straight back, with `bot_route` re-read from the current
        config so a change takes effect on the next turn.
        """
        bot_route = self.options.bot_route

        if session_view.remote_session_id:
            return SessionInit(
                remote_session_id=session_view.remote_session_id,
                remote_profile_id=session_view.remote_profile_id,
                remote_bot_route=bot_route,
            )

        profile_id = self._rest.upsert_profile(
            email=user.email,
            latest_flow_used=remote.flow_name,
            company=self.options.company,
            # REACHED ONLY ON THE FIRST TURN. The early return above is what
            # makes that true, so the ELEVATE read below happens once per
            # interview rather than once per turn -- it is a blocking outbound
            # call on a thread that already holds a pooled DB connection.
            extra=self._user_fields(user),
        )
        return SessionInit(
            remote_session_id=self._rest.generate_session(),
            remote_profile_id=profile_id,
            remote_bot_route=bot_route,
        )

    def _user_fields(self, user) -> Dict[str, str]:
        """Who the caller is, in Mitra's Profile column names.

        Empty unless this agent opts in with `remote.options.send_user_profile`.
        The other agent on this class does not, so its upsert body is unchanged
        -- see MitraOptions for why the flag is per-agent.

        WHY MITRA WANTS THIS AT ALL. A guest interview authenticates with
        `access_token: None`, so Mitra learns nothing about the caller beyond
        the derived email. Its MOM renderer reads `profile.first_name` and only
        falls back to a name scraped out of the transcript when that is blank
        (chatbot/pdf/shiksha_chaupal/mom_report.py), so an author line that is
        currently at the mercy of what the user typed becomes the name they
        actually registered with.

        TWO TIERS, AND THE SECOND IS BEST-EFFORT:

          display_name  is a JWT claim -- already in hand, no call, always there.
          the rest      needs ELEVATE, which needs the caller's OWN token.

        The token is the caller's, passed per call and never stored on self:
        one instance of this provider is shared by every conversation on this
        connection (ProviderRegistry caches by (name, checksum)), so a token
        held on the instance would serve one user's profile to whoever opened a
        session next.

        A FAILED READ IS NOT A FAILED INTERVIEW. There is no token at all under
        AUTH_CHECK=false, ELEVATE may be unconfigured or down, and none of that
        is a reason to refuse to capture a discussion -- the fields are an
        improvement on what Mitra would otherwise infer, not a precondition. So
        every failure degrades to the JWT name and logs. The exception type is
        deliberately not narrowed: `_profile_reader` is duck-typed (this package
        must not import app.integrations for one method), so its hierarchy is
        not knowable from here.
        """
        if not self.options.send_user_profile:
            return {}

        # The JWT's name, which needs nothing and is always present.
        fields: Dict[str, str] = {"first_name": user.display_name or ""}

        if self._profile_reader is None or not user.token:
            logger.info(
                "mitra: sending the JWT display name only -- %s",
                "no profile reader is configured" if self._profile_reader is None
                else "this caller has no ELEVATE token (AUTH_CHECK=false?)",
            )
            return _non_blank(fields)

        try:
            profile = self._profile_reader.read_profile(user.token)
        except Exception as e:  # noqa: BLE001 - see the docstring
            logger.warning(
                "mitra: profile read failed (%s); continuing with the JWT "
                "display name only", e,
            )
            return _non_blank(fields)

        # Mitra's column names on the left, Saarthi's profile field names on the
        # right (app/integrations/elevate/mapping.py owns the latter).
        fields["first_name"] = str(profile.get("name") or user.display_name or "")
        fields["designation"] = str(profile.get("role") or "")
        fields["org_associated"] = str(profile.get("school_name") or "")
        fields["location"] = ", ".join(
            part for part in (
                str(profile.get("district") or "").strip(),
                str(profile.get("state") or "").strip(),
            ) if part
        )

        # NOT `preferred_route`. Mitra's post_profile treats its presence as a
        # request to TRANSLITERATE first_name through AI4Bharat, so sending the
        # user's language here would quietly rewrite their name.
        return _non_blank(fields)

    # ------------------------------------------------------------------
    # Finalisation
    # ------------------------------------------------------------------

    def finalize(self, remote, session_view, user) -> FinalizeResult:
        story_id, _content = self._rest.finalize(
            session_id=session_view.remote_session_id,
            profile_id=session_view.remote_profile_id,
            flow=remote.flow_name,
            language=session_view.language,
            token=user.token,
            # Per-agent, because v1 and v2 resolve the story bot from different
            # Mitra tables. Sending a flow to the endpoint that cannot resolve it
            # is a deterministic HTTP 500.
            path=self.options.finalize_path,
            # Also per-agent: Mitra turns token presence into auth=True and picks
            # the PDF template's user_type from it, so a guest flow finalised
            # with a token renders a BLANK pdf rather than failing. Must match
            # the socket's `access_token: None`.
            as_guest=self.options.finalize_as_guest,
        )

        # NON-FATAL, and it must stay that way. finalize() above already
        # succeeded and is IRREVERSIBLE -- Mitra's Story.session is UNIQUE, so
        # the story cannot be submitted a second time. Letting a report-URL
        # problem propagate left the session in 'finalizing' forever, with a
        # story that exists in Mitra and can never be re-fetched. Observed live:
        # Mitra serves report PDFs from a different host than base_url, so an
        # incomplete allowed_hosts made every successful story raise here.
        #
        # The artifact is designed to be absent at this point anyway --
        # generation lags, and GET /api/sessions/{id}/report polls for it later,
        # where the identical failure is already treated as "not ready yet".
        artifact_url = None
        try:
            artifact_url = self._rest.get_report(
                session_view.remote_session_id, media_type=remote.report_media_type,
            )
        except ProviderError as e:
            logger.warning(
                "finalize: report fetch failed for session %s (%s); completing "
                "without an artifact URL -- the report route will retry",
                session_view.id, e,
            )

        return FinalizeResult(result_ref=story_id, artifact_url=artifact_url)

    def fetch_artifact(self, remote, session_view) -> Optional[str]:
        if not session_view.remote_session_id:
            return None
        return self._rest.get_report(
            session_view.remote_session_id, media_type=remote.report_media_type,
        )

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def validate_config(self, remote, settings) -> Optional[Tuple[list, str]]:
        """Reject a configuration that would fail SILENTLY at interview time.

        There is no startup sync any more, so a config reaches Mitra exactly as
        it was written. `bot_route` and `company` are non-empty by schema; what
        the schema cannot check is `finalize_path` against `produces_artifact`,
        because the endpoints are themselves configurable.

        Getting it wrong is not a loud failure: anything unrecognised falls
        through to the v1 branch and finalises with the wrong body shape, which
        Mitra ACCEPTS -- returning a story, a story_media row, a 200 from
        get-story and a downloadable, completely blank PDF, with nothing logged
        anywhere.
        """
        options = self.options
        base = ["remote", "options", "finalize_path"]

        if not remote.produces_artifact:
            # A flow that produces no artifact never calls finalize, so it has
            # no endpoint to validate -- and naming one would imply otherwise.
            if options.finalize_path:
                return (
                    base,
                    "finalize_path must be null when remote.produces_artifact is "
                    "false -- a flow that creates no story never finalises, and "
                    "naming an endpoint here would imply otherwise",
                )
            return None

        if not options.finalize_path:
            return (base, "finalize_path is required when remote.produces_artifact is true")

        # Against the endpoints THIS config resolves to -- it may carry its own
        # `paths`, and checking against the defaults would both reject correct
        # configs and accept wrong ones.
        if not options.paths.is_known_finalize(options.finalize_path):
            return (
                base,
                f"finalize_path {options.finalize_path!r} matches neither the "
                f"resolved v1 endpoint ({options.paths.finalize_v1!r}) nor the "
                f"resolved v2 endpoint ({options.paths.finalize_v2!r})",
            )

        return None
