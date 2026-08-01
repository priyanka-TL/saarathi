import { COPY } from '../../constants';
import { cx } from '../../utils/cx';
import { formatRelativeTime } from '../../utils/time';
import { AgentIcon } from '../icons';

/**
 * The recent-conversations list.
 *
 * The list is fetched ONCE at boot and then patched locally after each turn
 * (see useRecentConversations' upsert) -- it is deliberately never refetched,
 * which is why a fresh turn moves its conversation to the top without a
 * network round trip.
 *
 * Titles are server-supplied user content and render as text children, so
 * React escapes them. (The original used textContent here for the same reason,
 * while using innerHTML for agent names -- an asymmetry React removes.)
 */
export default function RecentConversationList({ conversations, activeId, onSelect, disabled }) {
  return (
    <ul id="recent-conversations-list" className="agent-list">
      {conversations.map((conv) => (
        <li
          key={conv.id}
          className={cx('agent-item', conv.id === activeId && 'active')}
          data-id={conv.id}
          data-last-message-at={conv.last_message_at || ''}
          onClick={() => {
            if (disabled) return;
            onSelect(conv.id);
          }}
        >
          {/*
            The .agent-item-title wrapper is load-bearing: it is the flex row
            that sits the icon beside the name. Dropping it (or promoting
            .agent-name from a <span> to a block) changes the row's height and
            shifts every row below it.
          */}
          <div className="agent-item-title">
            <AgentIcon />
            <span className="agent-name">{conv.title || COPY.newConversation}</span>
          </div>
          <div className="agent-desc">
            {conv.last_message_at
              ? `${COPY.lastActivePrefix}${formatRelativeTime(new Date(conv.last_message_at))}`
              : COPY.noMessages}
          </div>
        </li>
      ))}
    </ul>
  );
}
