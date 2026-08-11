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

  it('leaves New Chat only layout inline, so the stylesheet owns its look', () => {
    // An inline `color` outranks any rule; that is what kept the label dark on
    // the filled background before.
    const { container } = render(sidebar());
    const style = container.querySelector('#new-chat-btn').getAttribute('style');

    expect(style).not.toMatch(/color|background|font-/);
  });
});
