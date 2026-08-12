import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import Sidebar from '../components/sidebar/Sidebar.jsx';
import { AuthProvider } from '../context/AuthContext.jsx';
import { ProfileProvider } from '../context/ProfileContext.jsx';

/**
 * The rail's control hierarchy, asserted structurally.
 *
 * jsdom computes no layout, so "New Chat looks primary" is not directly
 * testable. What IS testable is the ordering and nesting the design depends on,
 * and the spacer chain it must not break.
 *
 * Wrapped in AuthProvider (LogoutRow reads useAuth) and ProfileProvider
 * (ProfileRow reads useProfile), same as ChatPage gets via App.jsx in
 * production. No network stub is needed: ProfileProvider only fetches when
 * authenticated and there is no token in localStorage here, so the rail
 * renders with an empty profile and issues no request.
 */
function sidebar(props = {}) {
  return (
    <AuthProvider>
      <ProfileProvider>
      <Sidebar
        open={false}
        onClose={() => {}}
        conversations={[]}
        activeConversationId={null}
        onSelectConversation={() => {}}
        onNewChat={() => {}}
        agents={[]}
        capabilities={[]}
        activeCard={null}
        activeAgentKey={null}
        onRunAction={() => {}}
        onSelectAgent={() => {}}
        isBusy={() => false}
        toast={null}
        voiceLanguage="en"
        onVoiceLanguageChange={() => {}}
        voiceAvailable
        {...props}
      />
      </ProfileProvider>
    </AuthProvider>
  );
}

describe('the rail puts the primary action first', () => {
  it('puts the language selector ABOVE the brand card', () => {
    // A preference set once, kept out of the way of the rail's actions rather
    // than sitting under New Chat competing with it.
    const { container } = render(sidebar());
    const language = container.querySelector('.voice-language');

    expect(language.nextElementSibling).toHaveClass('brand-card');
  });

  it('leaves New Chat as the first thing in the spacer', () => {
    const { container } = render(sidebar());
    const spacer = container.querySelector('.chat-history-placeholder');

    const order = [...spacer.children].map((el) => el.id || el.className);
    expect(order[0]).toBe('new-chat-btn');
    expect(spacer.querySelector('.voice-language')).toBeNull();
  });

  it('does not break the spacer chain that pins Advanced to the bottom', () => {
    // .chat-history-placeholder { flex: 1 0 auto } is what holds Advanced down,
    // so a sibling BETWEEN it and Chat History would unpin the panel. The
    // selector sits ahead of the whole chain, which leaves it intact.
    const { container } = render(sidebar());
    const spacer = container.querySelector('.chat-history-placeholder');

    expect(spacer.nextElementSibling).toHaveClass('chat-history-section');
  });

  it('reads "Preferred language", not "Voice language"', () => {
    const { getByLabelText } = render(sidebar());
    expect(getByLabelText('Preferred language')).not.toBeNull();
  });

  it('drops the voice row entirely when voice is unavailable', () => {
    const { container } = render(sidebar({ voiceAvailable: false }));

    expect(container.querySelector('.voice-language')).toBeNull();
    // New Chat is unaffected.
    expect(container.querySelector('#new-chat-btn')).not.toBeNull();
  });

  it('gives the language select an accessible name, not a bare dropdown', () => {
    // The <label> was commented out in 031c737 and stayed that way, so the
    // control rendered as an unexplained dropdown reading "English" with no
    // accessible name at all. The rail looked like it had no voice option.
    const { container } = render(sidebar());

    expect(container.querySelector('.voice-language-label')).not.toBeNull();
    expect(screen.getByLabelText('Preferred language').tagName).toBe('SELECT');
  });

  it('leaves the brand card leading straight into the spacer chain', () => {
    // Nothing sits between them any more. A collapsible PROFILE panel briefly
    // did; it moved to the account block at the bottom, which is what keeps the
    // spacer chain asserted above intact.
    const { container } = render(sidebar());

    expect(container.querySelector('.brand-card').nextElementSibling)
      .toHaveClass('chat-history-placeholder');
  });

  it('groups the account actions at the END, on one row', () => {
    // Both used to sit third from the top in one row with the raw account
    // identifier -- ahead of New Chat and both panels. Account actions belong
    // past everything a user might actually want to do, destructive one last.
    const { container } = render(sidebar());
    const advanced = container.querySelector('.advanced-section');
    const actions = container.querySelector('.account-actions');

    expect(advanced.nextElementSibling).toBe(actions);
  });

  it('puts View Profile and Logout in the same row, in that order', () => {
    // They were briefly two separate rows with two different button shapes,
    // which read as unrelated chrome that happened to be adjacent.
    const { container } = render(sidebar());
    const buttons = [...container.querySelectorAll('.account-actions button')];

    expect(buttons.map((b) => b.textContent)).toEqual(['View Profile', 'Logout']);
  });

  it('gives both account buttons the same class, so they cannot drift apart', () => {
    const { container } = render(sidebar());
    const buttons = [...container.querySelectorAll('.account-actions button')];

    expect(buttons).toHaveLength(2);
    buttons.forEach((b) => expect(b).toHaveClass('account-action-btn'));
  });

  it('makes Logout the last control in the rail', () => {
    const { container } = render(sidebar());

    // Toast stays the last NODE (it renders null with no message, so nothing
    // visible follows Logout); Logout is the last ACTION.
    const buttons = [...container.querySelectorAll('aside.sidebar button')];
    expect(buttons.at(-1)).toHaveTextContent('Logout');
  });

  it('offers View Profile as a menu entry, always', () => {
    // The whole point of the entry: it must never disappear. The panel it
    // replaced returned null when /api/profile answered 503, so on a backend
    // without ELEVATE_BASE_URL the profile entry silently vanished from the
    // menu -- indistinguishable from a broken feature. Nothing is stubbed here,
    // so the profile has not loaded, and the entry is still present.
    render(sidebar());

    expect(screen.getByRole('button', { name: /View Profile/ })).toBeTruthy();
  });

  it('no longer renders the raw account identifier', () => {
    // The whole reason the row was replaced: `user.name || user.phone ||
    // user.email` resolved to a bare id for a derived-email tenant, so the rail
    // displayed something like "78bcc996f24761c3220718...".
    const { container } = render(sidebar());

    expect(container.querySelector('.account-row')).toBeNull();
    expect(container.querySelector('.account-name')).toBeNull();
  });

  it('leaves New Chat only layout inline, so the stylesheet owns its look', () => {
    // An inline `color` outranks any rule; that is what kept the label dark on
    // the filled background before.
    const { container } = render(sidebar());
    const style = container.querySelector('#new-chat-btn').getAttribute('style');

    expect(style).not.toMatch(/color|background|font-/);
  });
});
