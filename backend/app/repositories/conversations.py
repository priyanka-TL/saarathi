import json
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Text, cast, func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.models.orm import Conversation, ConversationStatusEnum
from app.domain.core import UserContext
from app.domain.conversations import ConversationDTO, ConversationPageDTO


class ConversationRepository:
    def __init__(self, session: Session):
        self._session = session

    def get_or_create(self, conversation_id: Optional[uuid.UUID], user: UserContext) -> ConversationDTO:
        """
        Gets an existing conversation by ID scoped to the tenant and external user.
        If the conversation_id is None or the conversation is not found for this user/tenant,
        creates a new conversation and returns it.
        """
        if conversation_id:
            stmt = select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.tenant_code == user.tenant_code,
                Conversation.user_id == user.user_id,
            )
        else:
            # Resolve the most recent active conversation for the user.
            # COALESCE for the same reason as list_for_user: nullslast() sent a
            # freshly created, not-yet-touched conversation to the BOTTOM, so
            # "continue where I left off" could resolve to an older thread
            # instead of the current one.
            stmt = select(Conversation).where(
                Conversation.tenant_code == user.tenant_code,
                Conversation.user_id == user.user_id,
                Conversation.status == ConversationStatusEnum.active
            ).order_by(
                func.coalesce(Conversation.last_message_at, Conversation.created_at).desc()
            ).limit(1)

        conv = self._session.execute(stmt).scalar_one_or_none()
        if conv:
            return ConversationDTO.model_validate(conv)

        # Not found -> Create new, ALWAYS with a server-generated id.
        #
        # This used to reuse the caller's `conversation_id` as the primary key,
        # which made the id a client-controlled value: a stale
        # sessionStorage.saarthi_cid (different login, rebuilt database, an id
        # copied from elsewhere) was silently resurrected as a brand-new
        # conversation, and an id that already existed under another tenant hit
        # the primary key and surfaced as an IntegrityError 500 instead of a
        # clean miss.
        new_conv = Conversation(
            id=uuid.uuid4(),
            tenant_code=user.tenant_code,
            organization_id=user.active_org_id,
            user_id=user.user_id,
            locale=user.locale,
            status=ConversationStatusEnum.active,
            message_count=0,
            metadata_={},
            # The conversation is the user's, so the user is its author. Every
            # later write on this row (touch, title, archive) restamps
            # updated_by with whoever made it.
            created_by=user.user_id,
            updated_by=user.user_id,
        )
        self._session.add(new_conv)
        self._session.flush() # flush to get defaults like created_at populated by db
        self._session.refresh(new_conv)
        return ConversationDTO.model_validate(new_conv)

    def create_new(self, user: UserContext) -> ConversationDTO:
        """Unconditionally start a fresh conversation.

        get_or_create(None) deliberately RESUMES the most recent active
        conversation, so /api/reset could only force a new one by archiving the
        old one first -- and archived conversations are excluded from the recent
        list. Every "New chat" therefore hid the conversation the user had just
        finished, which is why the sidebar could never show more than the
        current one. This gives reset a way to start fresh without erasing
        history from the list.
        """
        new_conv = Conversation(
            id=uuid.uuid4(),
            tenant_code=user.tenant_code,
            organization_id=user.active_org_id,
            user_id=user.user_id,
            locale=user.locale,
            status=ConversationStatusEnum.active,
            message_count=0,
            metadata_={},
            # The conversation is the user's, so the user is its author. Every
            # later write on this row (touch, title, archive) restamps
            # updated_by with whoever made it.
            created_by=user.user_id,
            updated_by=user.user_id,
        )
        self._session.add(new_conv)
        self._session.flush()
        self._session.refresh(new_conv)
        return ConversationDTO.model_validate(new_conv)

    def find_empty_for_user(self, user: UserContext) -> Optional[ConversationDTO]:
        """The user's newest conversation that has never held a message, or None.

        "New chat" pressed repeatedly used to leave one dead row behind per
        press -- 47 of 68 active conversations on the dev database were empty
        shells. They are invisible (list_for_user filters message_count > 0) but
        they accumulate forever and every one of them is a row that "most recent
        active" resolution has to sort past.

        /api/reset tried to solve this by scanning list_for_user for a
        conversation with no user messages, which can never match: that query
        already requires message_count > 0, and a conversation only gets there
        by way of a user message. So the reuse path never once executed -- and
        the dead code inside it ran `DELETE FROM messages`, a table that does
        not exist (it is `conversation_messages`), so the day it did match it
        would have 500ed. Asking the right question instead makes both problems
        go away: an empty conversation needs no clean-up at all.
        """
        stmt = select(Conversation).where(
            Conversation.tenant_code == user.tenant_code,
            Conversation.user_id == user.user_id,
            Conversation.status == ConversationStatusEnum.active,
            Conversation.message_count == 0,
        ).order_by(Conversation.created_at.desc()).limit(1)

        conv = self._session.execute(stmt).scalar_one_or_none()
        return ConversationDTO.model_validate(conv) if conv else None

    def get_scoped(self, conversation_id: uuid.UUID, user: UserContext) -> Optional[ConversationDTO]:
        """Like get_or_create's lookup half, but never creates on a miss --
        session routes need a genuine 404 on an unknown id or a wrong tenant,
        not a silently-created new conversation."""
        stmt = select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.tenant_code == user.tenant_code,
            Conversation.user_id == user.user_id,
        )
        conv = self._session.execute(stmt).scalar_one_or_none()
        return ConversationDTO.model_validate(conv) if conv else None

    def next_seq_for_update(self, conversation_id: uuid.UUID) -> int:
        """
        Locks the conversation row (FOR UPDATE) and increments the message_count.
        Returns the new sequence number (which is the incremented message_count).
        """
        stmt = (
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(message_count=Conversation.message_count + 1)
            .returning(Conversation.message_count)
        )
        result = self._session.execute(stmt).scalar_one()
        return result

    # Marks a title that was generated for the user rather than taken from
    # something they said, so the first real user message may still replace it.
    # Without this, an autostart interview's placeholder ("Capture Discussions —
    # 30 Jul 07:48") would win permanently, because touch() COALESCEs.
    PLACEHOLDER_TITLE_FLAG = "title_is_placeholder"

    def set_placeholder_title(self, id: uuid.UUID, title: str) -> None:
        """Give an untitled conversation a provisional name, flagged so the
        first thing the user actually types can take it over."""
        stmt = (
            update(Conversation)
            .where(Conversation.id == id, Conversation.title.is_(None))
            .values(
                title=title,
                # json.dumps, not the dict itself: the bind has to reach psycopg
                # as text for the ::jsonb cast to adapt it.
                metadata_=Conversation.metadata_.op("||")(
                    cast(literal(json.dumps({self.PLACEHOLDER_TITLE_FLAG: True})), JSONB)
                ),
            )
        )
        self._session.execute(stmt)

    def replace_placeholder_title(self, id: uuid.UUID, title: str) -> None:
        """Promote the user's own words over a placeholder, once.

        A no-op when the title is real (the flag is absent) or when there is no
        title yet, so it composes with touch()'s COALESCE rather than fighting
        it: touch() sets a first real title, this upgrades a placeholder, and
        neither can overwrite a title the user's own words already produced.
        """
        stmt = (
            update(Conversation)
            .where(
                Conversation.id == id,
                Conversation.metadata_[self.PLACEHOLDER_TITLE_FLAG].astext == "true",
            )
            .values(
                title=title,
                metadata_=Conversation.metadata_.op("-")(
                    cast(literal(self.PLACEHOLDER_TITLE_FLAG), Text)
                ),
            )
        )
        self._session.execute(stmt)

    def touch(self, id: uuid.UUID, title_from: Optional[str] = None) -> None:
        """
        Updates last_message_at and optionally sets the title if it's currently null.
        """
        # func.now(), NOT datetime.utcnow(). last_message_at is timestamptz and
        # utcnow() returns a NAIVE datetime, which Postgres interprets in the
        # server's TimeZone -- on an Asia/Kolkata server that stored every
        # conversation 5h30m in the PAST. Visible as "Last active 5 hours ago"
        # on a chat that just happened, and it mis-sorts the recent list against
        # created_at (whose server_default is now(), i.e. correct). Measured
        # skew on the dev database: 5:29:59.99.
        values_to_update = {"last_message_at": func.now()}

        if title_from:
            # COALESCE keeps the FIRST title this conversation ever got -- the
            # title is set once and never replaced.
            stmt = (
                update(Conversation)
                .where(Conversation.id == id)
                .values(
                    last_message_at=values_to_update["last_message_at"],
                    title=func.coalesce(Conversation.title, title_from),
                )
            )
        else:
            stmt = (
                update(Conversation)
                .where(Conversation.id == id)
                .values(**values_to_update)
            )

        self._session.execute(stmt)

    def archive(self, id: uuid.UUID) -> None:
        """
        Sets the status of the conversation to archived.
        """
        stmt = (
            update(Conversation)
            .where(Conversation.id == id)
            .values(status=ConversationStatusEnum.archived)
        )
        self._session.execute(stmt)

    # There is no pin()/unpin() here any more, and nothing needs one. A
    # conversation's current agent is its one non-terminal agent_sessions row
    # (uq_agent_sessions_one_open_per_conversation), so opening a session IS
    # pinning and abandoning it IS unpinning. The two used to be written
    # separately under identical conditions with nothing enforcing that they
    # agreed -- see RouterService._pin_for.

    def list_for_user(self, user: UserContext, cursor: Optional[datetime], limit: int) -> ConversationPageDTO:
        """
        Returns a page of active conversations for the user using keyset pagination.
        """
        stmt = select(Conversation).where(
            Conversation.tenant_code == user.tenant_code,
            Conversation.user_id == user.user_id,
            Conversation.status == ConversationStatusEnum.active,
            # Empty shells are not history. get_or_create() makes a row per turn
            # attempt, so a failed turn (or a "New chat" nobody typed into)
            # leaves a 0-message conversation behind. Listed, they render as
            # "New conversation / No messages yet" and push real conversations
            # out of the top 5.
            Conversation.message_count > 0,
        ).order_by(
            # COALESCE, because last_message_at is only set by touch() at the
            # END of a successful turn. A conversation whose turn errored after
            # the user's message was committed keeps a NULL here and sorted to
            # the very bottom, below conversations far older than it.
            func.coalesce(Conversation.last_message_at, Conversation.created_at).desc()
        )

        if cursor:
            stmt = stmt.where(
                func.coalesce(Conversation.last_message_at, Conversation.created_at) < cursor
            )

        # Fetch limit + 1 to check if there is a next page
        stmt = stmt.limit(limit + 1)
        
        rows = self._session.execute(stmt).scalars().all()
        
        has_next = len(rows) > limit
        page_rows = rows[:limit]
        
        next_cursor = None
        if has_next and page_rows:
            # Must match the ORDER BY/WHERE expression above, or paging past a
            # NULL last_message_at would skip rows.
            last = page_rows[-1]
            next_cursor = last.last_message_at or last.created_at

        return ConversationPageDTO(
            conversations=[ConversationDTO.model_validate(row) for row in page_rows],
            next_cursor=next_cursor
        )
