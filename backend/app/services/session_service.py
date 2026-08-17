"""Agent session lifecycle.

Responsible for: the state machine over `agent_sessions` -- open, transition,
abandon -- and auditing each change.
Used by: OrchestrationService and the sessions router.

A session's state IS the pin: `uq_agent_sessions_one_open_per_conversation`
allows one open row per conversation, so opening is pinning and reaching a
terminal state is unpinning. There is no second column to keep in step.
"""
import uuid
from datetime import datetime
from typing import Callable, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.agents.protocol import SessionDelta
from app.models.orm import SYSTEM_ACTOR, AgentSession
from app.exceptions.domain import ConcurrentModificationError, InvalidTransitionError
from app.domain.sessions import AgentSessionDTO
from app.repositories.audit import AuditLogRepository
from app.repositories.sessions import AgentSessionRepository

# Lifecycle table (design doc §4.7). Same-state entries are field-only updates
# (bumping step/turn_count); terminal states accept no apply() at all.
#
# 'pending' allows 'awaiting_user'/'finalizing' DIRECTLY because
# RemoteFlowAgentHandler.handle() does profile creation, the handshake and the
# first turn in one call, reporting only the post-turn outcome -- 'in_progress'
# is never set on turn 1. Without these, a session's first turn raises
# InvalidTransitionError before the reply is ever persisted.
ALLOWED = {
    "pending": {"pending", "authenticating", "awaiting_user", "finalizing", "failed"},
    "authenticating": {"authenticating", "in_progress", "failed"},
    "in_progress": {"in_progress", "awaiting_user", "finalizing", "failed"},
    "awaiting_user": {"awaiting_user", "in_progress", "finalizing", "abandoned"},
    "finalizing": {"finalizing", "completed", "failed"},
    "completed": set(),
    "failed": set(),
    "abandoned": set(),
}
TERMINAL = {"completed", "failed", "abandoned"}


# InvalidTransitionError / ConcurrentModificationError are imported above and
# re-exported for existing callers; they live in app/exceptions/domain.py.


class SessionService:
    def __init__(self, session: Session):
        self._session = session
        self._sessions = AgentSessionRepository(session)
        self._audit = AuditLogRepository(session)

    def open_for(
        self,
        conversation_id: uuid.UUID,
        agent,
        on_displace: Optional[Callable[[uuid.UUID], None]] = None,
        actor: str = SYSTEM_ACTOR,
    ) -> Optional[AgentSessionDTO]:
        """Attach this conversation's open session if it belongs to `agent`,
        else open a new one -- but only if the agent declares pin_session.

        A SESSION IS NEVER SHARED ACROSS AGENTS. The unique index allows one
        open session per conversation but says nothing about whose it is, and
        handing back whichever was found was a genuine cross-agent hijack: the
        next turn went to the other agent's remote_session_id, and a later
        finalize submitted the wrong flow upstream for that story.

        A conversation spanning several agents is the intended product model, so
        a mismatch ABANDONS the other session rather than refusing the turn.
        `on_displace` then lets the caller close the orphaned channel --
        this layer owns no socket.
        """
        existing = self._sessions.get_open_for_conversation(conversation_id)
        if existing is not None:
            agent_id = self._as_uuid(agent.id)
            if existing.agent_id == agent_id:
                return existing

            # Different agent: retire the old session before opening a new one.
            self.abandon(conversation_id, reason="agent_switch")
            if on_displace is not None:
                on_displace(conversation_id)

        if not agent.spec.routing.pin_session:
            return None

        remote = getattr(agent.spec, "remote", None)
        language = getattr(remote, "default_language", "en") if remote else "en"
        return self._sessions.create_pending(
            conversation_id, self._as_uuid(agent.id), language=language, actor=actor,
        )

    @staticmethod
    def _as_uuid(value) -> uuid.UUID:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))

    def _check_transition(self, current_state: str, target_state: str, session_id: uuid.UUID) -> None:
        if target_state not in ALLOWED.get(current_state, set()):
            raise InvalidTransitionError(session_id, current_state, target_state)

    def apply(self, session: AgentSessionDTO, delta: SessionDelta) -> AgentSessionDTO:
        """State transition plus field updates, validated against ALLOWED.

        Terminal targets set ended_at; 'completed' also sets finalized_at.
        ck_agent_sessions_completed_has_result is deliberately NOT pre-checked --
        a caller bug should surface as a real IntegrityError, not be swallowed.
        """
        target_state = delta.state.value
        self._check_transition(session.state, target_state, session.id)

        fields = {}
        if delta.remote_session_id is not None:
            fields["remote_session_id"] = delta.remote_session_id
        if delta.remote_profile_id is not None:
            fields["remote_profile_id"] = delta.remote_profile_id
        if delta.remote_flow is not None:
            fields["remote_flow"] = delta.remote_flow
        if delta.remote_bot_route is not None:
            fields["remote_bot_route"] = delta.remote_bot_route
        if delta.step is not None:
            fields["step"] = delta.step
        if delta.result_ref is not None:
            fields["result_ref"] = delta.result_ref
        if delta.report_url is not None:
            fields["report_url"] = delta.report_url
        if delta.error is not None:
            fields["error"] = delta.error
        if delta.state_data is not None:
            fields["state_data"] = delta.state_data

        # Every apply() call represents one turn of interaction.
        fields["turn_count"] = AgentSession.turn_count + 1
        fields["last_activity_at"] = func.now()

        if target_state in TERMINAL:
            fields["ended_at"] = func.now()
            if target_state == "completed":
                fields["finalized_at"] = func.now()

        updated = self._sessions.update_state_and_fields(
            session.id, expected_states={session.state}, new_state=target_state, **fields,
        )
        if updated is None:
            raise ConcurrentModificationError(session.id)
        return updated

    def claim_finalizing(self, session_id: uuid.UUID) -> Optional[AgentSessionDTO]:
        """THE idempotency mechanism. None means another request already owns
        finalisation; that caller should separately self.get(session_id) (or
        the repository's .get()) for the cached result_ref."""
        return self._sessions.claim_finalizing(session_id)

    def get(self, session_id: uuid.UUID) -> Optional[AgentSessionDTO]:
        return self._sessions.get(session_id)

    def abandon(self, conversation_id: uuid.UUID, reason: str, actor: str = SYSTEM_ACTOR) -> Optional[AgentSessionDTO]:
        """Terminal + audit, against the same Session/transaction, so a single
        commit makes both changes atomic. Bypasses apply()'s strict transition
        map: abandon is an escape hatch reachable from ANY non-terminal state --
        required so a crash mid-'in_progress' or an exit keyword arriving during
        'authenticating' can still be abandoned.

        MOVING THE SESSION TO A TERMINAL STATE IS THE UNPIN. There is no second
        `conversations.unpin()` call any more: RouterService._pin_for reads the
        open session, and this statement is what stops it being open. Returning
        None when there is no open session is therefore now exactly right --
        there is nothing left that a caller would still have to clean up.
        """
        session = self._sessions.get_open_for_conversation(conversation_id)
        if session is None:
            return None

        updated = self._sessions.abandon_open_session(conversation_id, reason)
        if updated is None:
            raise ConcurrentModificationError(session.id)

        self._audit.insert(
            action="session_abandon",
            entity_type="agent_session",
            entity_id=updated.id,
            actor=actor,
            before=session.model_dump(mode="json"),
            after=updated.model_dump(mode="json"),
            note=reason,
        )
        return updated

    def abandon_and_close(
        self,
        conversation_id: uuid.UUID,
        reason: str,
        actor: str,
        on_abandoned: Optional[Callable[[uuid.UUID], None]] = None,
    ) -> Optional[AgentSessionDTO]:
        """`abandon`, plus whatever has to be torn down alongside it.

        The pairing matters and is easy to half-do: abandoning the session
        without closing its socket orphans the channel, and the pool then
        hands the next agent a channel still authenticated against the old
        remote session.

        `on_abandoned` fires ONLY when a session was actually abandoned. Calling
        it unconditionally would close a live channel belonging to a session
        this call did not touch.

        :param on_abandoned: same shape as `open_for`'s `on_displace`, and for
            the same reason -- it keeps this service free of any knowledge of
            the provider, whose channel pool lives on the container.
        """
        updated = self.abandon(conversation_id, reason=reason, actor=actor)
        if updated is not None and on_abandoned is not None:
            on_abandoned(conversation_id)
        return updated

    def sweep_abandoned(self, older_than: datetime) -> List[AgentSessionDTO]:
        """For the periodic job. Bulk-abandons every non-terminal session idle
        since before `older_than`, writing one audit row per swept session.

        The bulk UPDATE is the whole state change -- each swept session becomes
        terminal, which is what releases its conversation back to the router.
        """
        swept = self._sessions.sweep_abandoned_older_than(
            older_than, reason=f"idle sweep: no activity before {older_than.isoformat()}",
        )
        for row in swept:
            self._audit.insert(
                action="session_abandon",
                entity_type="agent_session",
                entity_id=row.id,
                actor=SYSTEM_ACTOR,
                after=row.model_dump(mode="json"),
                note="idle sweep",
            )
        return swept
