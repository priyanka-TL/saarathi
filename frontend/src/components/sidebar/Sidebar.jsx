import { cx } from '../../utils/cx';
import Toast from '../common/Toast.jsx';
import { CloseIcon, PlusIcon } from '../icons';
import AdvancedSection from './AdvancedSection';
import BrandCard from './BrandCard';
import ChatHistorySection from './ChatHistorySection';

/**
 * The left rail.
 *
 * On mobile (<=768px) it becomes `position: fixed` and slides in FROM THE
 * RIGHT (`right: -100%` -> `right: 0`), which is unusual enough to be worth
 * stating -- the markup order is unchanged, the CSS does all of it.
 *
 * `.chat-history-placeholder` is the flex spacer (`flex: 1 0 auto`) that
 * absorbs the rail's free space, which is what pushes the two collapsible
 * panels to the bottom as a pair. It now wraps the New Chat button ALONE --
 * the recent list moved into ChatHistorySection below it -- but it still has
 * to be here and still has to grow, or Advanced unpins from the bottom.
 */
export default function Sidebar({
  open,
  onClose,
  conversations,
  activeConversationId,
  onSelectConversation,
  onNewChat,
  agents,
  capabilities,
  activeCard,
  activeAgentKey,
  onRunAction,
  onSelectAgent,
  isBusy,
  toast,
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
      </div>

      <ChatHistorySection
        conversations={conversations}
        activeId={activeConversationId}
        onSelect={onSelectConversation}
        disabled={isBusy()}
      />

      <AdvancedSection
        capabilities={capabilities}
        agents={agents}
        activeCard={activeCard}
        activeAgentKey={activeAgentKey}
        onRunAction={onRunAction}
        onSelectAgent={onSelectAgent}
        isBusy={isBusy}
      />

      {/*
        Last child, so it sits at the bottom of the rail under the Advanced
        panel. Renders nothing when there is no message, which is why it can
        live in the flex column without reserving space.
      */}
      <Toast message={toast?.message} onDismiss={toast?.dismiss} />
    </aside>
  );
}
