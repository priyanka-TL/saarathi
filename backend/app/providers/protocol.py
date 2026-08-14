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
class CompletionCheck:
    """The answer to "has the remote finished?", plus what to remember.

    A RESULT OBJECT RATHER THAN A BARE BOOL, and the reason is the second field.
    Answering this question costs a paginated round trip -- the platform's
    transcript endpoint offers only `limit`/`offset` and honours no ordering
    parameter (verified against the live API), so "read the last row" means
    learning `count` first and then seeking to it. That was two HTTP calls on
    every single turn.

    `state_data` is how it becomes one: the provider hands back an OPAQUE patch
    to remember on the session, and reads it again next turn to seek straight to
    roughly the right place. The handler merges it WITHOUT INTERPRETING IT --
    the provider owns its own cache format, and `app.agents` is forbidden from
    knowing a platform's name, let alone its pagination.

    NEVER TEST THIS OBJECT FOR TRUTHINESS. A dataclass instance is always truthy,
    so `if provider.is_complete(...)` would read as "finished" on every turn and
    finalise the interview on the first one. Read `.done`.
    """

    done: bool
    #: Opaque to every caller. Merged into `agent_sessions.state_data` when
    #: present; None means "nothing to remember from this check".
    state_data: Optional[dict] = None


@dataclass(frozen=True)
class CompletionPoll:
    """What one poll of a platform's transcript endpoint learned.

    THE LAYER BELOW `CompletionCheck`, and the split is deliberate. A REST client
    answers in the terms it actually works in -- "is the last row COMPLETED, and
    how many rows are there now" -- and the provider translates that into the
    protocol-level `CompletionCheck`, whose cache is opaque. So pagination stays
    a platform's own business and never reaches `app.agents`, which the
    `.importlinter` contracts forbid from knowing a platform exists.

    Shared rather than declared twice because both platforms today are the SAME
    Django app behind different routes (`saathi/spec.py`), so `count` means the
    same thing to both. It lives here for the same reason `ChatRow` lives in
    `recovery.py`: a result shape two peer packages both return, owned by
    neither. A platform whose transcript endpoint works differently returns its
    own type -- nothing here requires this one.

    `count` IS THE CACHE. It is fed back as the next poll's `known_count`, and
    it comes from the response envelope rather than from anything inferred, so a
    stale value costs an extra round trip and never a wrong answer.

    READ `.done`, NEVER THE OBJECT -- see `CompletionCheck` for the trap.
    """

    done: bool
    count: int


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

    def is_complete(self, remote, session_view, user) -> CompletionCheck:
        """Whether the remote considers the conversation finished.

        Returns a `CompletionCheck`, not a bool -- read `.done`. See that class
        for why, and for the trap a bare truthiness check falls into.
        """
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
