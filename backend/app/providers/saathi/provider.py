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

NO CREDENTIAL IS MINTED OR CACHED HERE. This provider used to hold a
connection-level TokenProvider (a hardcoded `SAATHI_ACCESS_TOKEN`, or one
shared ELEVATE service login) -- one identity for every user hitting this
tenant's Saathi agent, which contradicted "per-user assistant" in the class
docstring above. Now the credential is `user.token`: the caller's own,
already-verified ELEVATE JWT from their own login (see
app/services/identity.py). A provider INSTANCE is still shared across every
conversation on this connection (ProviderRegistry.get() caches by
`(provider, conn.checksum)`, not per user), so the token is threaded through as
an explicit per-call argument -- never stored on `self` -- which is what keeps
this safe under concurrent turns from different logged-in users.
"""
from __future__ import annotations

from typing import ClassVar, Optional, Sequence, Tuple

from app.core.logger import get_logger
from app.providers.errors import ProviderAuthError
from app.providers.protocol import SessionInit
from app.providers.registry import register_provider
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
            # No auth_headers/on_unauthorized: the credential varies by CALLER,
            # not by connection, so it travels as an explicit per-call
            # `extra_headers` argument instead (see SaathiRestClient). A 401
            # here means that specific user's ELEVATE session expired -- there
            # is nothing for this provider to re-mint, so it propagates as a
            # plain ProviderHTTPError carrying ELEVATE's own message.
            error_keys=("message", "detail", "error_message"),
            detail_limit=200,
        )
        return SaathiRestClient(transport, self.options.paths)

    def _access_token(self, user) -> Optional[str]:
        """The caller's OWN ELEVATE JWT -- never minted or cached here.

        Saathi's consumer calls `fetch_elevate_user()` on this and CLOSES the
        connection when it is missing or rejected, deriving the profile from
        the token rather than from `profileid`.

        Raises when the caller has none: unlike Mitra's guest flow, a missing
        token is not a degraded mode for Saathi -- there is no shared or
        hardcoded fallback identity to converse as instead.
        """
        if not user.token:
            raise ProviderAuthError(
                "Saathi requires a logged-in ELEVATE user; this caller has no "
                "session token.",
                mechanism="user_token",
            )
        return user.token

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

        token = self._access_token(user)

        # 1. The profile comes FROM THE TOKEN. Saathi's consumer overwrites
        #    whatever `profileid` the authenticate frame carries, so this is read
        #    for the terms gate and for recent_chat's direction detection -- not
        #    to tell Saathi who is connecting.
        profile_id, accepted_tnc = self._rest.read_profile(token)

        # 2. THE TERMS GATE. An unaccepted profile cannot converse, and the
        #    failure is not obvious from the bot's reply.
        if not accepted_tnc:
            self._rest.accept_tnc(profile_id, token)
            logger.info(
                "saathi: accepted terms of use for profile",
                extra={"profile_id": profile_id},
            )

        return SessionInit(
            remote_session_id=self._rest.generate_session(token),
            remote_profile_id=profile_id,
            remote_bot_route=bot_route,
        )

    # ------------------------------------------------------------------
    # Per-user credential seam for REST calls the base class makes generically
    # ------------------------------------------------------------------

    def _is_session_completed(self, session_id: str, user) -> bool:
        return self._rest.is_session_completed(session_id, self._access_token(user))

    def _recent_chat(self, session_id: str, profile_id: str, user):
        return self._rest.recent_chat(session_id, profile_id, self._access_token(user))

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

        return None
