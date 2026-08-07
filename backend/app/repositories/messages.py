"""Persistence for `conversation_messages`.

Responsible for: writing turns and reading back a memory window.
Used by: OrchestrationService and ConversationService.
"""
import uuid
from typing import Optional, List, Any, Dict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.orm import SYSTEM_ACTOR, ConversationMessage, MessageRoleEnum
from app.domain.core import MemorySpec
from app.domain.conversations import MessageDTO, MessagePageDTO


class MessageRepository:
    def __init__(self, session: Session):
        self._session = session

    def insert(
        self,
        conversation_id: uuid.UUID,
        seq: int,
        role: str,
        content: str,
        agent_id: Optional[uuid.UUID] = None,
        agent_session_id: Optional[uuid.UUID] = None,
        route_reason: Optional[str] = None,
        route_confidence: Optional[float] = None,
        options: Optional[List[Dict[str, Any]]] = None,
        selected_option_id: Optional[str] = None,
        model: Optional[str] = None,
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        latency_ms: Optional[int] = None,
        error: Optional[str] = None,
        request_id: Optional[str] = None,
        actor: str = SYSTEM_ACTOR,
    ) -> MessageDTO:
        """
        Inserts a new message into the conversation history.

        `actor` is the user whose turn produced this row -- both the message
        they typed and the reply it drew, since an assistant message exists
        because that user asked for it. `agent_id` is what records WHICH agent
        wrote a reply; the two answer different questions.
        """
        # Convert string role to enum
        role_enum = MessageRoleEnum(role)

        new_msg = ConversationMessage(
            id=uuid.uuid4(),
            conversation_id=conversation_id,
            seq=seq,
            role=role_enum,
            content=content,
            agent_id=agent_id,
            agent_session_id=agent_session_id,
            route_reason=route_reason,
            route_confidence=route_confidence,
            options=options,
            selected_option_id=selected_option_id,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            error=error,
            request_id=request_id,
            created_by=actor,
            updated_by=actor,
        )
        self._session.add(new_msg)
        self._session.flush() # ensure defaults like created_at are generated
        self._session.refresh(new_msg)
        
        return MessageDTO.model_validate(new_msg)

    def recent(self, conversation_id: uuid.UUID, memory: Any) -> List[MessageDTO]:
        """
        Returns the last `N` messages up to `memory.history_turns` (or max_messages for legacy), ordered chronologically (seq ASC).
        """
        limit_val = getattr(memory, "history_turns", getattr(memory, "max_messages", 10))
        if limit_val <= 0:
            return []

        # We need the LAST limit_val, but returned in ASCENDING order.
        # This requires an inner query to get the last N descending, then outer query or just sort in python.
        stmt = (
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
            .order_by(ConversationMessage.seq.desc())
            .limit(limit_val)
        )
        rows = self._session.execute(stmt).scalars().all()
        
        # Reverse to get chronological order (seq ASC)
        rows = list(reversed(rows))
        
        return [MessageDTO.model_validate(row) for row in rows]

    def last_user_content(self, conversation_id: uuid.UUID) -> Optional[str]:
        """Text of the most recent user message, or None.

        Turn recovery needs to ask Mitra "what became of THIS message", and the
        conversation transcript is the only record of what was sent -- the turn
        that timed out never got far enough to store anything else.
        """
        stmt = (
            select(ConversationMessage.content)
            .where(
                ConversationMessage.conversation_id == conversation_id,
                ConversationMessage.role == "user",
            )
            .order_by(ConversationMessage.seq.desc())
            .limit(1)
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def list_page(self, conversation_id: uuid.UUID, after_seq: int, limit: int) -> MessagePageDTO:
        """
        Returns a page of messages for a conversation, using keyset pagination on `seq`.
        """
        stmt = (
            select(ConversationMessage)
            .where(
                ConversationMessage.conversation_id == conversation_id,
                ConversationMessage.seq > after_seq
            )
            .order_by(ConversationMessage.seq.asc())
            .limit(limit + 1)
        )
        rows = self._session.execute(stmt).scalars().all()
        
        has_next = len(rows) > limit
        page_rows = rows[:limit]
        
        next_seq = None
        if has_next and page_rows:
            next_seq = page_rows[-1].seq

        return MessagePageDTO(
            messages=[MessageDTO.model_validate(row) for row in page_rows],
            next_seq=next_seq
        )

    def distinct_agent_sequence(self, conversation_id: uuid.UUID) -> List[uuid.UUID]:
        """
        Returns an ordered list of agent_ids that have participated in the conversation,
        collapsing only consecutive duplicates.
        Used to reconstruct the flow breadcrumbs.
        """
        stmt = (
            select(ConversationMessage.agent_id)
            .where(
                ConversationMessage.conversation_id == conversation_id,
                ConversationMessage.agent_id.is_not(None)
            )
            .order_by(ConversationMessage.seq.asc())
        )
        agent_ids = self._session.execute(stmt).scalars().all()
        
        stops = []
        for aid in agent_ids:
            if not stops or stops[-1] != aid:
                stops.append(aid)
                
        return stops
