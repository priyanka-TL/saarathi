import { useState } from 'react';

import { cx } from '../../utils/cx';
import { ChevronDownIcon } from '../icons';
import RecentConversationList from './RecentConversationList';

/**
 * The collapsible "CHAT HISTORY" panel, directly above the Advanced one.
 *
 * HEADER: reuses .advanced-toggle / .advanced-toggle-left / .advanced-title /
 * .advanced-subtitle from style.css VERBATIM, so the two section headers are
 * identical by construction -- same card, radius, hover, both dark-mode
 * overrides, and the same UA button font (.advanced-toggle deliberately never
 * sets font-family, and a hand-written lookalike would silently "fix" that and
 * render the two headers in different typefaces). The chevron rotation comes
 * free for the same reason, which is why `aria-expanded` must stay a real
 * string attribute: the rule driving it is the attribute selector
 * `.advanced-toggle[aria-expanded="false"] .chevron`.
 *
 * COLLAPSE MECHANISM: grid 1fr -> 0fr, NOT the max-height technique Advanced
 * uses -- see root.css for why the ceiling makes that class unusable here.
 *
 * The New Chat button is deliberately NOT in here. It stays in
 * .chat-history-placeholder above, always visible: starting a new chat is a
 * primary action and must not need a disclosure to reach.
 */
export default function ChatHistorySection({
  conversations,
  activeId,
  onSelect,
  disabled,
}) {
  // Collapsed by default, like AdvancedSection.
  const [collapsed, setCollapsed] = useState(true);

  return (
    // `collapsed` is mirrored onto the wrapper as well as the content: the
    // toggle PRECEDES the content, so there is no sibling selector that could
    // reach back to zero its dead margin-bottom from the content's class.
    <div className={cx('chat-history-section', collapsed && 'collapsed')}>
      <button
        type="button"
        className="advanced-toggle"
        id="chat-history-toggle"
        aria-expanded={collapsed ? 'false' : 'true'}
        aria-controls="chat-history-content"
        onClick={() => setCollapsed((c) => !c)}
      >
        <div className="advanced-toggle-left">
          <span className="advanced-title">CHAT HISTORY</span>
          <div className="advanced-subtitle">Recent conversations</div>
        </div>
        <ChevronDownIcon className="chevron" />
      </button>

      <div
        className={cx('chat-history-content', collapsed && 'collapsed')}
        id="chat-history-content"
      >
        {/*
          `inert` takes the collapsed list out of the tab order AND the
          accessibility tree. .advanced-content only has `pointer-events: none`,
          which stops the mouse but NOT Tab -- its capability buttons are still
          focusable while invisible. That bug is not reproduced here.

          `collapsed || undefined` because React omits the attribute for
          undefined but would render inert="false" as a PRESENT attribute.
        */}
        <div className="chat-history-inner" inert={collapsed || undefined}>
          <RecentConversationList
            conversations={conversations}
            activeId={activeId}
            onSelect={onSelect}
            disabled={disabled}
          />
        </div>
      </div>
    </div>
  );
}
