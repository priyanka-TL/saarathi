"""The contract every remote provider implements.

Responsible for: what the core may ask of a remote conversation platform, and
the shapes it gets back.
Used by: RemoteFlowAgentHandler for a turn, TurnFinalizer for finalisation and
recovery, OrchestrationService for the artifact, the admin route for validation.

A Protocol rather than a base class, matching the rest of the codebase: a
provider is registered by decorator and needs no shared implementation. Providers
that DO share a protocol family are free to inherit from a common base under
`app/providers/ws_flow/` -- but they inherit from the family, never from each
other.

THE CLASSVARS ARE THE POINT. Every `if provider == "..."` the core used to carry
is now one of these declarations, read uniformly:

  stateful_transport  -> the single-worker startup guard (app/core/runtime.py)
  supports_recovery   -> whether a timed-out turn is reconciled
  produces_artifacts  -> whether finalisation can yield a downloadable artifact
  options_model       -> types and validates `remote.options`

Adding a provider therefore adds no branch anywhere above this file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, List, Optional, Protocol, Tuple
from uuid import UUID

from pydantic import BaseModel

from app.providers.recovery import Reconciliation
from app.providers.transport.frames import Attachment, ParsedOption


@dataclass(frozen=True)
class SessionInit:
    """The remote identifiers this turn should run against.

    Returned on EVERY turn, not only the first: a provider decides for itself
    whether a session already exists, which keeps the "is this the first turn?"
    branch out of the handler. `remote_bot_route` is re-supplied each turn so a
    configuration change takes effect on the next one.
    """

    remote_session_id: str
    remote_profile_id: Optional[str] = None
    remote_bot_route: Optional[str] = None


@dataclass(frozen=True)
class ProviderTurn:
    """One completed exchange with the remote platform."""

    text: str
    options: List[ParsedOption] = field(default_factory=list)
    #: Downloadable documents this turn produced. Already filtered against the
    #: connection's allowlist by the provider -- the core never re-checks and
    #: never fetches them; they are handed to the browser as links.
    attachments: List[Attachment] = field(default_factory=list)
    step: Optional[int] = None


@dataclass(frozen=True)
class FinalizeResult:
    """The outcome of finalising a session.

    `result_ref` satisfies ck_agent_sessions_completed_has_result. A provider
    that produces no artifact returns the remote session id, which is honest
    rather than a placeholder: the transcript really is retrievable with it.
    """

    result_ref: str
    artifact_url: Optional[str] = None


class RemoteProvider(Protocol):
    """One external conversation platform, as the core sees it."""

    #: Registry key. Matches `remote.provider` in a config row.
    name: ClassVar[str]

    #: Types `remote.options`. MUST set extra="forbid" -- a typo inside an
    #: untyped options block takes a default silently, and the failure modes
    #: that reaches (a wrong finalize endpoint, a wrong bot route) return
    #: HTTP 200 with blank or misrouted output. A guard test enforces both the
    #: presence of this and its strictness.
    options_model: ClassVar[type[BaseModel]]

    #: Does this provider hold sockets in process memory? Drives the
    #: single-worker startup guard. A deployment running only stateless
    #: providers is legitimately free of it.
    stateful_transport: ClassVar[bool]

    #: Can a timed-out turn be reconciled read-only against the provider?
    #: False means a timeout surfaces as a 504 with no recovery attempt.
    supports_recovery: ClassVar[bool]

    #: Can finalisation yield a downloadable artifact at all? An agent may still
    #: switch it off per config with `remote.produces_artifact`.
    produces_artifacts: ClassVar[bool]

    def open_session(self, remote, session_view, user) -> SessionInit:
        """Ensure a remote session exists for this conversation and return its
        identifiers. Called before every turn; idempotent after the first."""
        ...

    def turn(self, remote, session_view, text: str, user, *, first_turn: bool) -> ProviderTurn:
        """Send one user turn and return the reply.

        `user` is the caller's UserContext -- needed by a per-user platform to
        authenticate the socket/REST calls this turn makes (see
        SaathiProvider._access_token), ignored by a guest one (Mitra).
        """
        ...

    def is_complete(self, remote, session_view, user) -> bool:
        """Whether the remote considers the conversation finished."""
        ...

    def finalize(self, remote, session_view, user) -> FinalizeResult:
        """Submit the completed session. Never called when
        `remote.produces_artifact` is false."""
        ...

    def fetch_artifact(self, remote, session_view) -> Optional[str]:
        """The artifact URL, or None if it is not generated yet."""
        ...

    def reconcile(self, remote, session_view, sent_text: str, user) -> Optional[Reconciliation]:
        """What became of `sent_text`. READ-ONLY -- never re-send."""
        ...

    def close_channel(self, conversation_id: UUID) -> None:
        """Drop this conversation's pooled transport. Best-effort."""
        ...

    def validate_config(self, remote, settings) -> Optional[Tuple[list, str]]:
        """Reject a configuration that would fail SILENTLY at conversation time.

        :returns: ``(path, message)`` for the admin error envelope, or None.
            Returns rather than raises so the admin router keeps its own shape.
        """
        ...


class SupportsOptions(Protocol):
    """Structural helper for the parts of RemoteSpec a provider reads."""

    provider: str
    options: Any
