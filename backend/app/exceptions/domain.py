"""Domain exceptions: the failures this application knows how to describe.

Before this module the nine domain exceptions were declared in seven different
packages -- two in `services/orchestration.py`, two in `services/session_service.py`,
one each in `services/router_service.py`, `services/identity.py`,
`tools/registry.py` and `agents/factory.py`. Each was a bare `Exception`, so
nothing could catch "an expected failure" as a category; the chat router ended
up importing one of them INSIDE an `except Exception` block and dispatching on
`isinstance` to tell a 404 from a 500.

A common base fixes that: `app/exceptions/handlers.py` registers one handler for
`SaarthiError` and each subclass names its own status and code.

FRAMEWORK-FREE, and it must stay that way. `app.agents` imports from here and
`.importlinter` forbids anything under `app.agents` from reaching FastAPI or
Starlette -- so nothing in this module may import `fastapi`, and the HTTP
mapping lives on plain integers rather than on `starlette.status` constants.

NOT TO BE CONFUSED WITH THE INTEGRATION HIERARCHIES. `MitraError`,
`BhashiniError` and `StorageError` describe an EXTERNAL system failing and
already translate correctly at their own boundaries. These describe THIS
application refusing or being unable to do something.
"""
from __future__ import annotations

from typing import Any, Optional
from uuid import UUID


class SaarthiError(Exception):
    """Base for every expected, mapped domain failure.

    `status_code` and `error_code` are class attributes rather than constructor
    arguments so that a subclass declares its HTTP meaning once, next to the
    reason it exists, instead of at each raise site where the two could drift.

    The defaults are deliberately 500/INTERNAL: a subclass that forgets to name
    its mapping degrades to the generic error rather than accidentally telling a
    client that something unexpected was a clean 400.
    """

    status_code: int = 500
    error_code: str = "INTERNAL"

    #: What the client is told. `str(exc)` by default; override for anything
    #: whose message would leak an id, a key or an internal path.
    @property
    def public_message(self) -> str:
        return str(self)


# ---------------------------------------------------------------------------
# Turn pipeline
# ---------------------------------------------------------------------------


class ConcurrentTurnError(SaarthiError):
    """Another request is already running a turn on this conversation.

    Surfaced as HTTP 409, never retried automatically: the whole point is that
    the duplicate must not reach Mitra, where two user messages in a row merge
    into one and destroy an answer (§1.6).
    """

    status_code = 409
    error_code = "CONCURRENT_TURN"

    def __init__(self, conversation_id: Any) -> None:
        super().__init__(f"a turn is already in flight for conversation {conversation_id}")
        self.conversation_id = conversation_id

    @property
    def public_message(self) -> str:
        return "A reply is already on its way. Please wait for it before sending again."


class TurnLimitExceeded(SaarthiError):
    """The agent's configured limits refuse this turn. Surfaced as HTTP 429."""

    status_code = 429
    error_code = "RATE_LIMITED"

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail

    @property
    def public_message(self) -> str:
        return self.detail


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


class AgentNotFound(SaarthiError):
    """No enabled, selectable agent answers to this key."""

    status_code = 404
    error_code = "AGENT_NOT_FOUND"

    def __init__(self, key: Optional[str]) -> None:
        super().__init__(f"agent not found or not selectable: {key!r}")
        self.key = key

    @property
    def public_message(self) -> str:
        # Not the key back: this is reachable with client-supplied input, and
        # echoing it turns the endpoint into a probe for which agents exist.
        return "Agent not found"


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class InvalidTransitionError(SaarthiError):
    """A session was asked to move to a state its current state cannot reach."""

    status_code = 409
    error_code = "INVALID_TRANSITION"

    def __init__(self, session_id: UUID, current_state: str, target_state: str) -> None:
        super().__init__(
            f"session {session_id}: cannot transition {current_state!r} -> {target_state!r}"
        )
        self.session_id = session_id
        self.current_state = current_state
        self.target_state = target_state


class ConcurrentModificationError(SaarthiError):
    """A guarded UPDATE matched zero rows: the session's state changed between
    the caller's read and this call."""

    status_code = 409
    error_code = "CONCURRENT_MODIFICATION"

    def __init__(self, session_id: UUID) -> None:
        super().__init__(f"session {session_id} was modified concurrently")
        self.session_id = session_id


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


class InvalidTokenError(SaarthiError):
    """The env token could not be decoded at all (malformed / not a JWT)."""

    status_code = 401
    error_code = "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class UnknownToolError(SaarthiError):
    """An AgentSpec referenced tools no registry entry provides.

    Raised while VALIDATING a config (POST /api/agents/{key}/config) and while
    building a handler, so it must name the missing tools -- an admin fixing a
    config needs to know which ones.
    """

    status_code = 422
    error_code = "CONFIG_INVALID"

    def __init__(self, missing: list) -> None:
        super().__init__(f"Unknown tools: {', '.join(missing)}")
        self.missing = missing


class UnknownAgentType(SaarthiError):
    """No handler is registered for this spec's `agent_type`."""

    status_code = 422
    error_code = "CONFIG_INVALID"
