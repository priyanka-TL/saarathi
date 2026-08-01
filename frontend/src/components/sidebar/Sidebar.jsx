import { cx } from '../../utils/cx';
import { CloseIcon, PlusIcon } from '../icons';
import AdvancedSection from './AdvancedSection';
import BrandCard from './BrandCard';
import RecentConversationList from './RecentConversationList';

/**
 * The left rail.
 *
 * On mobile (<=768px) it becomes `position: fixed` and slides in FROM THE
 * RIGHT (`right: -100%` -> `right: 0`), which is unusual enough to be worth
 * stating -- the markup order is unchanged, the CSS does all of it.
 *
 * `.chat-history-placeholder` is the flex spacer that pushes the Advanced
 * section to the bottom; it must keep wrapping the New Chat button and the
 * recent list.
 */
export default function Sidebar({
  open,
  onClose,
  conversations,
  activeConversationId,
  onSelectConversation,
  onNewChat,
  agents,
  activeCard,
  activeAgentKey,
  onActivateCapability,
  onSelectDisplayCard,
  onSelectAgent,
  isBusy,
}) {
  return (
    <aside className={cx('sidebar', open && 'active')} id="sidebar">
      <div className="mobile-sidebar-header">
        <span className="mobile-sidebar-title">NAVIGATE FLOWS</span>
        <button
          type="button"
          id="mobile-sidebar-close"
          className="mobile-sidebar-close"
          aria-label="Close menu"
          onClick={onClose}
        >
          <CloseIcon />
        </button>
      </div>

      {/*
        The theme-toggle .sidebar-toolbar block is commented out in the
        original markup, so the toggle never binds and dark mode is
        unreachable from the UI even though the CSS is fully present. Kept
        absent here for the same reason.
      */}

      <BrandCard />

      <div className="chat-history-placeholder">
        <button
          type="button"
          id="new-chat-btn"
          className="agent-item"
          // Inline style preserved verbatim: this is a <button> wearing an
          // <li>'s class, so it inherits .agent-item's card/border/radius and
          // overrides only layout.
          style={{
            width: '100%',
            textAlign: 'left',
            fontSize: '0.9rem',
            fontWeight: 500,
            color: 'var(--text-primary)',
            marginBottom: '12px',
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
          }}
          onClick={onNewChat}
        >
          <PlusIcon />
          New Chat
        </button>
        <RecentConversationList
          conversations={conversations}
          activeId={activeConversationId}
          onSelect={onSelectConversation}
          disabled={isBusy()}
        />
      </div>

      <AdvancedSection
        agents={agents}
        activeCard={activeCard}
        activeAgentKey={activeAgentKey}
        onActivateCapability={onActivateCapability}
        onSelectDisplayCard={onSelectDisplayCard}
        onSelectAgent={onSelectAgent}
        isBusy={isBusy}
      />
    </aside>
  );
}
