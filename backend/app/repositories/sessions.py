import uuid
from datetime import datetime
from typing import Optional, Iterable, List

from sqlalchemy import select, update, func
from sqlalchemy.orm import Session

from app.models.orm import SYSTEM_ACTOR, AgentSession, SessionStateEnum
from app.domain.sessions import AgentSessionDTO

_TERMINAL = {SessionStateEnum.completed, SessionStateEnum.failed, SessionStateEnum.abandoned}
_CLAIMABLE = {SessionStateEnum.in_progress, SessionStateEnum.awaiting_user}


class AgentSessionRepository:
    def __init__(self, session: Session):
        self._session = session

    def get(self, session_id: uuid.UUID) -> Optional[AgentSessionDTO]:
        row = self._session.execute(
            select(AgentSession).where(AgentSession.id == session_id)
        ).scalar_one_or_none()
        return AgentSessionDTO.model_validate(row) if row else None

    def get_open_for_conversation(self, conversation_id: uuid.UUID) -> Optional[AgentSessionDTO]:
        """Mirrors uq_agent_sessions_one_open_per_conversation's own predicate exactly -- at most one row."""
        row = self._session.execute(
            select(AgentSession).where(
                AgentSession.conversation_id == conversation_id,
                AgentSession.state.notin_(_TERMINAL),
            )
        ).scalar_one_or_none()
        return AgentSessionDTO.model_validate(row) if row else None

    def get_latest_for_conversation(self, conversation_id: uuid.UUID) -> Optional[AgentSessionDTO]:
        """Most recent session for a conversation in ANY state, terminal included.

        Distinct from get_open_for_conversation, which excludes terminal states
        by design (it backs the one-open-session invariant). Resuming a
        conversation needs the opposite: a *completed* session is exactly the
        one whose report link has to come back after a reload.

        Ordered to match ix_agent_sessions_conversation (conversation_id, created_at DESC).
        """
        row = self._session.execute(
            select(AgentSession)
            .where(AgentSession.conversation_id == conversation_id)
            .order_by(AgentSession.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        return AgentSessionDTO.model_validate(row) if row else None

    def list_for_conversation(self, conversation_id: uuid.UUID) -> List[AgentSessionDTO]:
        """Every session a conversation has held, oldest first.

        A conversation can legitimately hold more than one: the router supports
        switching agents mid-conversation (that is what the flow breadcrumb
        renders), and uq_agent_sessions_one_open_per_conversation only forbids two OPEN sessions,
        not a new one after an earlier one reached a terminal state.

        get_latest_for_conversation answers "what is in flight" and is still
        right for that. It is the wrong question for restoring history: ordering
        by created_at alone let a newer session mask an earlier COMPLETED one,
        so its report_url never reached the client and the Download PDF button
        vanished when the conversation was reopened.
        """
        rows = self._session.execute(
            select(AgentSession)
            .where(AgentSession.conversation_id == conversation_id)
            .order_by(AgentSession.created_at.asc())
        ).scalars().all()
        return [AgentSessionDTO.model_validate(row) for row in rows]

    def create_pending(
        self, conversation_id: uuid.UUID, agent_id: uuid.UUID, language: str = "en",
        actor: str = SYSTEM_ACTOR,
    ) -> AgentSessionDTO:
        """No remote_* columns are known yet at creation time.

        `language` is the agent's configured default. It used to fall through to
        the column's server_default ('en') on every session, which meant
        RemoteSpec.default_language was never read by anything -- an agent
        configured for hi/kn/te still opened its Mitra channel with route='en',
        because MitraChannel._authenticate sends this column's value.
        """
        row = AgentSession(
            conversation_id=conversation_id,
            agent_id=agent_id,
            state=SessionStateEnum.pending,
            language=language,
            created_by=actor,
            updated_by=actor,
        )
        self._session.add(row)
        self._session.flush()
        self._session.refresh(row)
        return AgentSessionDTO.model_validate(row)

    def update_state_and_fields(
        self,
        session_id: uuid.UUID,
        expected_states: Iterable[str],
        new_state: str,
        **field_updates,
    ) -> Optional[AgentSessionDTO]:
        """The shared guarded UPDATE...WHERE state IN (...)...RETURNING primitive
        used by apply(). Returns None if zero rows matched -- an optimistic-
        concurrency signal that the session's state moved between the caller's
        read and this call."""
        values = dict(field_updates)
        values["state"] = SessionStateEnum(new_state)
        stmt = (
            update(AgentSession)
            .where(
                AgentSession.id == session_id,
                AgentSession.state.in_([SessionStateEnum(s) for s in expected_states]),
            )
            .values(**values)
            .returning(AgentSession)
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        return AgentSessionDTO.model_validate(row) if row else None

    def claim_finalizing(self, session_id: uuid.UUID) -> Optional[AgentSessionDTO]:
        """THE idempotency guard (design doc §4.7). Its own dedicated statement,
        not routed through update_state_and_fields -- sets ONLY state, no
        last_activity_at bump, exactly per the doc's literal SQL. Extended to
        RETURNING the whole row (not just id) so a winning caller gets the
        fresh session in one round trip."""
        stmt = (
            update(AgentSession)
            .where(AgentSession.id == session_id, AgentSession.state.in_(_CLAIMABLE))
            .values(state=SessionStateEnum.finalizing)
            .returning(AgentSession)
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        return AgentSessionDTO.model_validate(row) if row else None

    def abandon_open_session(self, conversation_id: uuid.UUID, reason: str) -> Optional[AgentSessionDTO]:
        """Bypasses the strict per-state transition map -- abandon is an escape
        hatch reachable from ANY non-terminal state (design doc §6.5's pin
        lifecycle diagram draws it from the whole composite "pinned" state,
        not a single leaf sub-state)."""
        stmt = (
            update(AgentSession)
            .where(
                AgentSession.conversation_id == conversation_id,
                AgentSession.state.notin_(_TERMINAL),
            )
            .values(state=SessionStateEnum.abandoned, ended_at=func.now(), error=reason)
            .returning(AgentSession)
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        return AgentSessionDTO.model_validate(row) if row else None

    def sweep_abandoned_older_than(self, cutoff: datetime, reason: str) -> List[AgentSessionDTO]:
        """Bulk operation for the periodic job. Matches ix_agent_sessions_sweep's own
        predicate exactly: ANY non-terminal state, not just awaiting_user."""
        stmt = (
            update(AgentSession)
            .where(
                AgentSession.state.notin_(_TERMINAL),
                AgentSession.last_activity_at < cutoff,
            )
            .values(state=SessionStateEnum.abandoned, ended_at=func.now(), error=reason)
            .returning(AgentSession)
        )
        rows = self._session.execute(stmt).scalars().all()
        return [AgentSessionDTO.model_validate(r) for r in rows]
