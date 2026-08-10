import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import Sidebar from '../components/sidebar/Sidebar.jsx';

/**
 * The rail's control hierarchy, asserted structurally.
 *
 * jsdom computes no layout, so "New Chat looks primary" is not directly
 * testable. What IS testable is the ordering and nesting the design depends on,
 * and the spacer chain it must not break.
 */
function sidebar(props = {}) {
  return (
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
  );
}

describe('the rail puts the primary action first', () => {
  it('renders New Chat above the voice-language row', () => {
    const { container } = render(sidebar());
    const spacer = container.querySelector('.chat-history-placeholder');

    const order = [...spacer.children].map((el) => el.id || el.className);
    expect(order[0]).toBe('new-chat-btn');
    expect(order[1]).toContain('voice-language');
  });

  it('keeps voice language INSIDE the spacer, not between it and Chat History', () => {
    // The spacer chain is what pins Advanced to the bottom; inserting a
    // sibling here would break chatHistory.test.jsx and unpin the panel.
    const { container } = render(sidebar());
    const spacer = container.querySelector('.chat-history-placeholder');

    expect(spacer.querySelector('.voice-language')).not.toBeNull();
    expect(spacer.nextElementSibling).toHaveClass('chat-history-section');
  });

  it('drops the voice row entirely when voice is unavailable', () => {
    const { container } = render(sidebar({ voiceAvailable: false }));

    expect(container.querySelector('.voice-language')).toBeNull();
    // New Chat is unaffected.
    expect(container.querySelector('#new-chat-btn')).not.toBeNull();
  });

  it('leaves New Chat only layout inline, so the stylesheet owns its look', () => {
    // An inline `color` outranks any rule; that is what kept the label dark on
    // the filled background before.
    const { container } = render(sidebar());
    const style = container.querySelector('#new-chat-btn').getAttribute('style');

    expect(style).not.toMatch(/color|background|font-/);
  });
});
