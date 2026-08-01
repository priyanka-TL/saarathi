import uuid
from typing import Optional, List, Dict, Any, Set

from sqlalchemy.orm import Session
from sqlalchemy import text

from app.repositories.conversations import ConversationRepository
from app.repositories.messages import MessageRepository
from app.domain.core import UserContext, MemorySpec
from app.domain.conversations import ConversationDTO, ConversationPageDTO, MessageDTO


class ConversationService:
    def __init__(self, session: Session):
        self._session = session
        self._conv_repo = ConversationRepository(session)
        self._msg_repo = MessageRepository(session)

    def resolve(self, conversation_id: Optional[uuid.UUID], user: UserContext) -> ConversationDTO:
        """
        Gets or creates a conversation.
        """
        return self._conv_repo.get_or_create(conversation_id, user)

    def history(self, conversation_id: uuid.UUID, memory_spec: MemorySpec) -> List[Dict[str, str]]:
        """
        Returns recent messages bounded by the memory spec in chronological order.
        """
        msgs = self._msg_repo.recent(conversation_id, memory_spec)
        return [{"role": m.role, "content": m.content} for m in msgs]

    def resolve_agent_names(self, agent_ids: Set[uuid.UUID]) -> Dict[uuid.UUID, str]:
        """
        Batch-resolves agent ids to their current display name, straight from
        the `agents` table (not the registry, which only holds enabled
        agents -- historical messages must still show the name of an agent
        that's since been disabled).
        """
        if not agent_ids:
            return {}
        ids = list(agent_ids)
        placeholders = ", ".join(f":id{i}" for i in range(len(ids)))
        params = {f"id{i}": aid for i, aid in enumerate(ids)}
        q = text(f"SELECT id, name FROM agents WHERE id IN ({placeholders})")
        return {row.id: row.name for row in self._session.execute(q, params).fetchall()}

    def flow_payload(self, conversation_id: uuid.UUID) -> Dict[str, Any]:
        """
        Reproduces the legacy module-global flow_payload exactly.
        """
        stmt = self._session.execute(
            text("SELECT title FROM conversations WHERE id = :id"),
            {"id": conversation_id}
        )
        row = stmt.fetchone()
        title = row[0] if row else None

        agent_ids = self._msg_repo.distinct_agent_sequence(conversation_id)
        agent_names = self.resolve_agent_names(set(agent_ids))
        stops = [agent_names[aid] for aid in agent_ids if aid in agent_names]

        return {
            "title": title,
            "stops": stops,
            "current_index": len(stops) - 1 if stops else -1
        }

    def list_recent(self, user: UserContext, limit: int) -> ConversationPageDTO:
        """
        Returns the user's most recently active conversations, newest first.
        """
        return self._conv_repo.list_for_user(user, cursor=None, limit=limit)

    def list_messages(self, conversation_id: uuid.UUID) -> List[MessageDTO]:
        """
        Returns the full message history for a conversation, chronological
        (seq ASC). A single generous page -- no pagination UI exists to drive
        keyset paging further, and 500 messages is far beyond any realistic
        POC conversation.
        """
        return self._msg_repo.list_page(conversation_id, after_seq=0, limit=500).messages

    def find_current(
        self, conversation_id: Optional[uuid.UUID], user: UserContext
    ) -> Optional[ConversationDTO]:
        """The conversation the user is currently in, or None -- never creates.

        resolve() is get_or_create; callers that only want to ACT on an existing
        conversation (reset abandoning its session) must not conjure one.
        """
        if conversation_id:
            return self._conv_repo.get_scoped(conversation_id, user)
        page = self._conv_repo.list_for_user(user, cursor=None, limit=1)
        return page.conversations[0] if page.conversations else None

    def start_new(self, user: UserContext) -> ConversationDTO:
        """Begins a fresh conversation, leaving the previous one in the user's
        history. See ConversationRepository.create_new.

        Reuses an existing empty conversation when the user already has one, so
        pressing "New chat" (or clicking a capability button) several times in a
        row does not leave a dead row behind each time. An empty conversation is
        already a clean slate -- there is nothing to erase.
        """
        existing = self._conv_repo.find_empty_for_user(user)
        if existing is not None:
            return existing
        return self._conv_repo.create_new(user)

    def reset(self, conversation_id: uuid.UUID) -> None:
        """
        Archives the given conversation.

        NOTE: /api/reset no longer calls this -- archiving on "New chat" is what
        hid every finished conversation from the recent list. Kept for a real,
        user-initiated archive action; `archived` now means only that.
        """
        self._conv_repo.archive(conversation_id)
