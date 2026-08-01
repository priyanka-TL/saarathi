import { MenuIcon } from '../components/icons';
import { cx } from '../utils/cx';

/**
 * The two-column shell: sidebar + chat column, plus the mobile overlay.
 *
 * The DOM order matters -- `.sidebar-overlay` is the LAST child of
 * `.app-container`, after the chat column, so its z-index stacking works
 * without any explicit ordering rules.
 *
 * `.mobile-menu-btn.standalone` is absolutely positioned at the TOP-RIGHT of
 * the chat column (not the top-left, as a hamburger usually is) because the
 * sidebar slides in from the right on mobile.
 */
export default function AppLayout({ sidebar, banner, chatCard, sidebarOpen, onToggleSidebar }) {
  return (
    <div className="app-container">
      {sidebar}

      <div className="chat-container">
        <button
          type="button"
          id="mobile-menu-btn"
          className="mobile-menu-btn standalone"
          aria-label="Toggle menu"
          onClick={onToggleSidebar}
        >
          <MenuIcon />
        </button>

        <div className="chat-inner-box">
          {banner}
          <div className="chat-window-card">{chatCard}</div>
        </div>
      </div>

      <div
        id="sidebar-overlay"
        className={cx('sidebar-overlay', sidebarOpen && 'active')}
        onClick={onToggleSidebar}
      />
    </div>
  );
}
