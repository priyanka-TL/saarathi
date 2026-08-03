import uuid
from datetime import datetime
from typing import Callable, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.agents.protocol import SessionDelta
from app.models.orm import AgentSession
from app.domain.sessions import AgentSessionDTO
from app.repositories.audit import AuditLogRepository
from app.repositories.conversations import ConversationRepository
from app.repositories.sessions import AgentSessionRepository

# Lifecycle table, design doc §4.7. Same-state entries are allowed (a delta
# that doesn't change state is a field-only update -- e.g. bumping step/
# turn_count while remaining in 'awaiting_user'); terminal states accept no
# apply() call at all, including a same-state one, since they're final.
#
# 'pending' additionally allows 'awaiting_user'/'finalizing' directly:
# RemoteFlowAgentHandler.handle() (src/agents/remote_flow_handler.py) is one
# synchronous call that does profile+session creation, the WS handshake, AND
# the first turn, then returns a single delta reporting the outcome -- per
# the design doc's own §8.3 sequence diagram, the first turn's real state
# path is pending -> authenticating -> awaiting_user, with 'in_progress'
# never actually set on turn 1 (it's only meaningful as "a turn is in
# flight" on a RETURNING turn, i.e. awaiting_user -> in_progress ->
# awaiting_user -- and that same-state round trip already works today via
# the existing awaiting_user -> awaiting_user entry below, since the handler
# only reports the post-turn outcome, never the momentary in-flight state).
# Without this, a session's very first turn -- or one that completes the
# interview in a single turn -- raises InvalidTransitionError before the
# reply is ever persisted.
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


class InvalidTransitionError(Exception):
    def __init__(self, session_id: uuid.UUID, current_state: str, target_state: str):
        super().__init__(f"session {session_id}: cannot transition {current_state!r} -> {target_state!r}")
        self.session_id = session_id
        self.current_state = current_state
        self.target_state = target_state


class ConcurrentModificationError(Exception):
    """A guarded UPDATE matched zero rows: the session's state changed between
    the caller's read and this call."""
    def __init__(self, session_id: uuid.UUID):
        super().__init__(f"session {session_id} was modified concurrently")
        self.session_id = session_id


class SessionService:
    def __init__(self, session: Session):
        self._session = session
        self._sessions = AgentSessionRepository(session)
        self._conversations = ConversationRepository(session)
        self._audit = AuditLogRepository(session)

    def open_for(
        self,
        conversation_id: uuid.UUID,
        agent,
        on_displace: Optional[Callable[[uuid.UUID], None]] = None,
    ) -> Optional[AgentSessionDTO]:
        """Attach the existing open session for this conversation if it belongs
        to `agent`; otherwise create one in 'pending', but only if the agent
        declares pin_session (stateless agents get no session at all).

        A SESSION IS NEVER SHARED ACROSS AGENTS. uq_sess_one_open_per_conv
        allows at most one open session per conversation but says nothing about
        whose it is, and this method used to hand back whichever one it found --
        its docstring delegated the agent check to the caller, but RouterService
        Gate 1 (explicit selection from the UI) returns an agent without ever
        consulting the pin, so nothing enforced it.

        The effect was a genuine cross-agent hijack: with `currentAgentKey`
        still set to capture_discussion, opening a Record Stories conversation
        from the sidebar sent the next turn to record_stories'
        remote_session_id, while RemoteFlowAgentHandler overwrote
        remote_bot_route with capture_discussion's `remote.bot_route` and apply() stamped
        remote_flow='guest-discussion' onto a 'guest-mi-story' session. A later
        finalize would then submit the wrong flow to Mitra for that story.

        A conversation legitimately spanning several agents is the intended
        product model, so a mismatch ABANDONS the other agent's session and
        starts a fresh one rather than refusing the turn. `on_displace` is
        invoked with the conversation id when that happens, so the caller can
        close the orphaned Mitra channel (this layer owns no socket).
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
            conversation_id, self._as_uuid(agent.id), language=language,
        )

    @staticmethod
    def _as_uuid(value) -> uuid.UUID:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))

    def _check_transition(self, current_state: str, target_state: str, session_id: uuid.UUID) -> None:
        if target_state not in ALLOWED.get(current_state, set()):
            raise InvalidTransitionError(session_id, current_state, target_state)

    def apply(self, session: AgentSessionDTO, delta: SessionDelta) -> AgentSessionDTO:
        """State transition + field updates, validated against the lifecycle
        table. Terminal targets set ended_at (required by ck_sess_terminal);
        'completed' additionally sets finalized_at -- ck_sess_completed_has_result
        is enforced by the database itself and is deliberately not pre-checked
        here, so a caller bug (transitioning to completed with no result_ref
        anywhere) surfaces as a real IntegrityError rather than being swallowed.
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

    def abandon(self, conversation_id: uuid.UUID, reason: str, actor: str = "system") -> Optional[AgentSessionDTO]:
        """Terminal, unpin, audit -- all against the same Session/transaction,
        so a single commit makes all three changes atomic. Bypasses apply()'s
        strict transition map: abandon is an escape hatch reachable from ANY
        non-terminal state (design doc §6.5's pin lifecycle diagram draws it
        from the whole composite 'pinned' state, not one leaf sub-state) --
        required so a crash mid-'in_progress' or an exit keyword arriving
        during 'authenticating' can still be abandoned.
        """
        session = self._sessions.get_open_for_conversation(conversation_id)
        if session is None:
            return None

        updated = self._sessions.abandon_open_session(conversation_id, reason)
        if updated is None:
            raise ConcurrentModificationError(session.id)

        self._conversations.unpin(conversation_id)

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

    def sweep_abandoned(self, older_than: datetime) -> List[AgentSessionDTO]:
        """For the periodic job. Bulk-abandons every non-terminal session idle
        since before `older_than`, unpinning each affected conversation and
        writing one audit row per swept session."""
        swept = self._sessions.sweep_abandoned_older_than(
            older_than, reason=f"idle sweep: no activity before {older_than.isoformat()}",
        )
        for row in swept:
            self._conversations.unpin(row.conversation_id)
            self._audit.insert(
                action="session_abandon",
                entity_type="agent_session",
                entity_id=row.id,
                actor="system",
                after=row.model_dump(mode="json"),
                note="idle sweep",
            )
        return swept
