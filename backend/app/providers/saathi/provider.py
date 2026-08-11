"""The Saathi provider.

Responsible for: what is specific to Saathi -- a profile read from the access
token, a terms-of-use gate, a per-user authenticated socket, and no artifact.
Used by: resolved through ProviderRegistry from `remote.provider = "saathi"`.

THREE DIFFERENCES FROM THE OTHER PLATFORM IN THIS FAMILY, and only three:

  1. The profile is READ, not upserted. Saathi derives it from the access token,
     so there is no (email, company) pair and no email-derivation trap.
  2. There is a TERMS-OF-USE GATE. A profile that has not accepted them cannot
     converse, and the failure is not obvious from the bot's reply, so the first
     turn accepts on the user's behalf.
  3. Nothing is finalised. `create_story: "none"` -- TurnFinalizer's no-artifact
     path completes the session instead.

Everything else comes from BaseWsFlowProvider. Note what is NOT here: this class
does not subclass, import or reference the Mitra provider. It used to -- via
`SaathiChannel(MitraChannel)` and `SaathiSessionManager(MitraSessionManager)` --
and an import-linter contract now makes that impossible.
"""
from __future__ import annotations

from typing import ClassVar, Optional, Sequence, Tuple

from app.core.logger import get_logger
from app.providers.protocol import SessionInit
from app.providers.registry import register_provider
from app.providers.saathi.auth import SCHEMES, build_token_provider
from app.providers.saathi.frames import quick_reply_chips
from app.providers.saathi.rest import SaathiRestClient
from app.providers.saathi.spec import SaathiOptions
from app.providers.transport.http import RestTransport
from app.providers.ws_flow.base import BaseWsFlowProvider
from app.providers.ws_flow.frames import OptionReader

logger = get_logger("provider.saathi")

#: The per-request credential header Saathi's REST surface expects.
AUTH_HEADER = "X-auth-token"


@register_provider
class SaathiProvider(BaseWsFlowProvider):
    """Saathi: a per-user assistant conversation that produces no artifact."""

    name: ClassVar[str] = "saathi"
    options_model: ClassVar[type] = SaathiOptions
    produces_artifacts: ClassVar[bool] = False
    option_readers: ClassVar[Sequence[OptionReader]] = (quick_reply_chips,)

    def __init__(self, conn, pool) -> None:
        # Built BEFORE the REST client, which needs it for its auth headers.
        # Building it here rather than lazily means a config naming variables
        # that are not set fails on the first turn with a message that names
        # them, instead of a silent 401 loop.
        self._tokens = build_token_provider(conn)
        super().__init__(conn, pool)

    def _build_rest(self) -> SaathiRestClient:
        conn = self._conn
        transport = RestTransport(
            provider="Saathi",
            base_url=conn.base_url,
            origin_url=conn.origin_url,
            user_agent=conn.user_agent,
            connect_timeout=conn.connect_timeout_s,
            read_timeout=conn.read_timeout_s,
            allowed_hosts=conn.allowed_hosts,
            auth_headers=lambda: {AUTH_HEADER: self._tokens.get()},
            on_unauthorized=self._reauthenticate,
            # Saathi's error envelope spells its keys differently from the other
            # platform's, and truncates shorter.
            error_keys=("message", "detail", "error_message"),
            detail_limit=200,
        )
        return SaathiRestClient(transport, self.options.paths)

    def _reauthenticate(self) -> bool:
        """Invalidate the token so the transport's single retry mints a new one.

        ELEVATE ends a token's session server-side, so a 401 is an expected
        mid-life event rather than a bug.
        """
        logger.info(
            "saathi: token rejected, re-minting and retrying once",
            extra={"mechanism": getattr(self._tokens, "mechanism", None)},
        )
        self._tokens.invalidate()
        return True

    def _access_token(self) -> Optional[str]:
        """The real ELEVATE JWT, resolved at connect time.

        Saathi's consumer calls `fetch_elevate_user()` on this and CLOSES the
        connection when it is missing or rejected, deriving the profile from the
        token rather than from `profileid`. Resolving it before the socket
        exists means a minting failure leaves nothing to clean up; and because
        the pool reconnects on a closed channel, `get()` runs again there and an
        expired session heals without the caller knowing.
        """
        return self._tokens.get()

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def open_session(self, remote, session_view, user) -> SessionInit:
        bot_route = self.options.bot_route

        if session_view.remote_session_id:
            return SessionInit(
                remote_session_id=session_view.remote_session_id,
                remote_profile_id=session_view.remote_profile_id,
                remote_bot_route=bot_route,
            )

        # 1. The profile comes FROM THE TOKEN. Saathi's consumer overwrites
        #    whatever `profileid` the authenticate frame carries, so this is read
        #    for the terms gate and for recent_chat's direction detection -- not
        #    to tell Saathi who is connecting.
        profile_id, accepted_tnc = self._rest.read_profile()

        # 2. THE TERMS GATE. An unaccepted profile cannot converse, and the
        #    failure is not obvious from the bot's reply.
        if not accepted_tnc:
            self._rest.accept_tnc(profile_id)
            logger.info(
                "saathi: accepted terms of use for profile",
                extra={"profile_id": profile_id},
            )

        return SessionInit(
            remote_session_id=self._rest.generate_session(),
            remote_profile_id=profile_id,
            remote_bot_route=bot_route,
        )

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def validate_config(self, remote, settings) -> Optional[Tuple[list, str]]:
        """Reject a configuration this provider cannot honour.

        `produces_artifact: true` is the one that matters: it would send the
        session down TurnFinalizer's finalize path, where this provider raises
        and the session lands in `failed` -- a perfectly normal conversation
        reported as an outage.
        """
        if remote.produces_artifact:
            return (
                ["remote", "produces_artifact"],
                "the saathi provider creates no artifact (its own "
                "/api/flow-connection-info/ reports create_story: 'none'), so "
                "produces_artifact must be false",
            )

        scheme = dict(self._conn.auth).get("scheme", "")
        if scheme not in SCHEMES:
            return (
                ["remote", "auth", "scheme"],
                f"scheme {scheme!r} is not supported by the saathi provider. "
                f"Supported: {', '.join(SCHEMES)}.",
            )

        return None
