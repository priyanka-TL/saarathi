import { cx } from '../../utils/cx';
import Toast from '../common/Toast.jsx';
import { CloseIcon, PlusIcon } from '../icons';
import AdvancedSection from './AdvancedSection';
import BrandCard from './BrandCard';
import ChatHistorySection from './ChatHistorySection';
import LanguageSelect from './LanguageSelect';

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
  voiceLanguage,
  onVoiceLanguageChange,
  voiceAvailable,
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

      {/*
        Above the flex spacer, so it sits with the brand card rather than being
        pushed to the bottom with the two collapsible panels -- it is a setting
        the user reaches for before speaking, not an advanced option.
      */}
      <div className="chat-history-placeholder">
        <button
          type="button"
          id="new-chat-btn"
          className="agent-item"
          // Still wearing .agent-item for its box model; #new-chat-btn in
          // style.css restyles it as the rail's PRIMARY action.
          //
          // The presentational half of this inline block moved to that rule.
          // It had to: an inline `color` outranks any stylesheet, so the text
          // stayed dark on the new filled background. What remains is layout
          // that belongs on the element rather than in a themeable rule.
          style={{
            width: '100%',
            textAlign: 'left',
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
          }}
          onClick={onNewChat}
        >
          <PlusIcon />
          New Chat
        </button>

        {/*
          BELOW New Chat, and inside the spacer, on purpose.

          The rail's primary action should lead; voice language is a
          preference someone sets once. It cannot be a sibling BETWEEN the
          spacer and Chat History -- chatHistory.test.jsx pins that chain
          because .chat-history-placeholder { flex: 1 0 auto } is what keeps
          Advanced at the bottom -- so it nests here instead, which leaves that
          chain intact.
        */}
        <LanguageSelect
          value={voiceLanguage}
          onChange={onVoiceLanguageChange}
          visible={voiceAvailable}
        />
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
