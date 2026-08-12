import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import ChatHistorySection from '../components/sidebar/ChatHistorySection.jsx';
import Sidebar from '../components/sidebar/Sidebar.jsx';
import { AuthProvider } from '../context/AuthContext.jsx';
import { ProfileProvider } from '../context/ProfileContext.jsx';

const never = () => false;

const conversations = [
  { id: 'c1', title: 'First', last_message_at: null },
  { id: 'c2', title: 'Second', last_message_at: null },
];

const section = (over = {}) => (
  <ChatHistorySection
    conversations={conversations}
    activeId="c1"
    onSelect={vi.fn()}
    disabled={false}
    {...over}
  />
);

/**
 * The full rail. Sidebar pulls from no router, so it renders with plain
 * props plus the two providers it reads -- empty capabilities/agents keep
 * AdvancedSection inert without stubbing it.
 *
 * AuthProvider: LogoutRow reads useAuth(). ProfileProvider: ProfileRow
 * reads useProfile(). Both are what ChatPage gets via App.jsx in production.
 *
 * NO NETWORK STUB NEEDED, and that is by design rather than luck:
 * ProfileProvider only fetches when authenticated, and there is no token in
 * localStorage here -- so the rail renders with an empty profile and makes no
 * request. Keep that property; a provider that fetched unconditionally would
 * fire unmocked axios into jsdom from every test in this file.
 */
const sidebar = (over = {}) => (
  <AuthProvider>
    <ProfileProvider>
    <Sidebar
      open
      onClose={vi.fn()}
      conversations={conversations}
      activeConversationId="c1"
      onSelectConversation={vi.fn()}
      onNewChat={vi.fn()}
      agents={[]}
      capabilities={[]}
      activeCard={null}
      activeAgentKey={null}
      onRunAction={vi.fn()}
      onSelectAgent={vi.fn()}
      isBusy={never}
      toast={null}
      {...over}
    />
    </ProfileProvider>
  </AuthProvider>
);

describe('the Chat History disclosure', () => {
  it('starts collapsed, like the Advanced panel', () => {
    const { container } = render(section());

    expect(container.querySelector('.chat-history-content')).toHaveClass('collapsed');
    expect(screen.getByRole('button')).toHaveAttribute('aria-expanded', 'false');
  });

  it('mirrors `collapsed` onto the wrapper, which is what closes the gap', async () => {
    // `.chat-history-section.collapsed .advanced-toggle { margin-bottom: 0 }`
    // is the rule that removes the 16px of dead space between the two panels.
    // The toggle precedes the content, so the content's own class cannot reach
    // it -- drop this one and the gap silently comes back.
    const { container } = render(section());
    const wrapper = container.querySelector('.chat-history-section');

    expect(wrapper).toHaveClass('collapsed');

    await userEvent.click(screen.getByRole('button'));
    expect(wrapper).not.toHaveClass('collapsed');
  });

  it('expands on click and collapses again on the next one', async () => {
    const { container } = render(section());
    const toggle = screen.getByRole('button');

    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(container.querySelector('.chat-history-content')).not.toHaveClass('collapsed');

    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(container.querySelector('.chat-history-content')).toHaveClass('collapsed');
  });

  it('keeps aria-expanded a real attribute, which is what rotates the chevron', () => {
    // style.css drives the rotation from `.advanced-toggle[aria-expanded="false"]
    // .chevron`. A boolean prop, a dropped attribute or a renamed class would
    // freeze the chevron pointing one way in both states, with no error
    // anywhere -- so the assertion is the SELECTOR, not the attribute value.
    const { container } = render(section());
    expect(
      container.querySelector('.advanced-toggle[aria-expanded="false"] .chevron'),
    ).not.toBeNull();
  });

  it('takes the collapsed list out of the tab order, not just out of reach of the mouse', async () => {
    // .advanced-content settles for `pointer-events: none`, which leaves its
    // buttons Tab-focusable while invisible. `inert` is what actually removes
    // them, and it has to track the state in BOTH directions -- React renders
    // inert="false" as a present (and therefore active) attribute, which is
    // why the component passes `collapsed || undefined`.
    const { container } = render(section());
    const inner = () => container.querySelector('.chat-history-inner');

    expect(inner()).toHaveAttribute('inert');

    await userEvent.click(screen.getByRole('button'));
    expect(inner()).not.toHaveAttribute('inert');
  });

  it('collapses without unmounting the list', async () => {
    // Conditional rendering would satisfy every class assertion above and
    // still be wrong: with no element to animate, the panel would snap shut
    // instead of easing over 0.3s.
    const { container } = render(section());
    await userEvent.click(screen.getByRole('button'));
    await userEvent.click(screen.getByRole('button'));

    expect(container.querySelector('#recent-conversations-list')).not.toBeNull();
    expect(container.querySelectorAll('.agent-item')).toHaveLength(2);
  });

  it('still selects a conversation, and still refuses while a turn is in flight', async () => {
    const onSelect = vi.fn();
    const { rerender } = render(section({ onSelect }));
    await userEvent.click(screen.getByRole('button', { name: /CHAT HISTORY/ }));

    await userEvent.click(screen.getByText('First'));
    expect(onSelect).toHaveBeenCalledWith('c1');

    // `disabled` is the one prop that changed hands in this refactor.
    onSelect.mockClear();
    rerender(section({ onSelect, disabled: true }));
    await userEvent.click(screen.getByText('First'));
    expect(onSelect).not.toHaveBeenCalled();
  });
});

describe('the Chat History section inside the rail', () => {
  it('leaves New Chat outside the collapsible, always reachable', () => {
    const { container } = render(sidebar());

    expect(container.querySelector('#new-chat-btn')).not.toBeNull();
    expect(container.querySelector('.chat-history-section #new-chat-btn')).toBeNull();
  });

  it('keeps the flex spacer that pins Advanced to the bottom', () => {
    // .chat-history-placeholder { flex: 1 0 auto } is what absorbs the rail's
    // free space. Folding it into the new section (or dropping it now that it
    // only wraps one button) unpins Advanced from the bottom of the rail --
    // invisible in jsdom, so it is asserted structurally.
    const { container } = render(sidebar());
    const spacer = container.querySelector('.chat-history-placeholder');

    expect(spacer).not.toBeNull();
    expect(spacer.nextElementSibling).toHaveClass('chat-history-section');
    expect(spacer.nextElementSibling.nextElementSibling).toHaveClass('advanced-section');
  });

  it('does not borrow the Advanced panel\'s capped collapse', () => {
    // .advanced-content stops at max-height: 1500px. MAX_ROWS is 20 and a full
    // history is ~1850px, so reusing that class would silently clip the oldest
    // rows. There must be exactly one .advanced-content in the rail.
    const { container } = render(sidebar());

    expect(container.querySelectorAll('.advanced-content')).toHaveLength(1);
    expect(container.querySelector('.chat-history-content')).not.toBeNull();
    expect(container.querySelector('.chat-history-section .collapsible-inner')).toBeNull();
  });

  it('gives the two toggles distinct ids and independent state', async () => {
    const { container } = render(sidebar());

    expect(container.querySelectorAll('#advanced-toggle')).toHaveLength(1);
    expect(container.querySelectorAll('#chat-history-toggle')).toHaveLength(1);

    await userEvent.click(container.querySelector('#chat-history-toggle'));

    expect(container.querySelector('#chat-history-toggle')).toHaveAttribute('aria-expanded', 'true');
    expect(container.querySelector('#advanced-toggle')).toHaveAttribute('aria-expanded', 'false');
    expect(container.querySelector('#advanced-content')).toHaveClass('collapsed');
  });
});
