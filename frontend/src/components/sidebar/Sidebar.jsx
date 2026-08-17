import { cx } from '../../utils/cx';
import Toast from '../common/Toast.jsx';
import { CloseIcon, PlusIcon } from '../icons';
import AccountActions from './AccountActions';
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

      {/*
        ABOVE the brand card and right-aligned: a preference the user sets once
        and then ignores, so it sits out of the way of the rail's actual
        controls rather than competing with New Chat for attention.

        Before the .chat-history-placeholder spacer, deliberately. The spacer
        carries flex: 1 0 auto and is what pins Advanced to the bottom, so a new
        sibling BETWEEN it and Chat History would unpin that panel --
        chatHistory.test.jsx pins the chain. Sitting ahead of the whole chain
        leaves it untouched.
      */}
      <LanguageSelect
        value={voiceLanguage}
        onChange={onVoiceLanguageChange}
        visible={voiceAvailable}
      />

      <BrandCard />

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
        THE ACCOUNT BLOCK, at the end of the menu: View Profile and Logout on
        one row. Past everything a user might actually want to do, with the
        destructive action last.

        After AdvancedSection, which is OUTSIDE the pinned spacer chain, so both
        adjacencies sidebarLayout.test.jsx and chatHistory.test.jsx assert are
        untouched: .voice-language -> .brand-card, and
        .chat-history-placeholder -> .chat-history-section. AdvancedSection's own
        `margin-top: auto` (style.css) still pins the group to the bottom; this
        row simply sits beneath it.
      */}
      <AccountActions />

      {/*
        Last child. Note the distinction now that LogoutRow exists: Logout is
        the last ACTION, Toast is the last NODE. Toast is a status region, not a
        menu item, and it renders nothing when there is no message -- which is
        why it can sit in the flex column without reserving space, and why
        keeping it here does not put anything visible below Logout.
      */}
      <Toast message={toast?.message} onDismiss={toast?.dismiss} />
    </aside>
  );
}
