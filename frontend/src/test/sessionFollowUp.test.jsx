import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { COPY } from '../constants';
import { ConversationProvider } from '../context/ConversationContext.jsx';
import { useChatMessages } from '../hooks/useChatMessages';
import { usePollRegistry } from '../hooks/usePollRegistry';
import { useSessionLifecycle } from '../hooks/useSessionLifecycle';

// The report poll would otherwise hit the network on the report_url-less path.
vi.mock('../api/sessions', () => ({
  getReport: vi.fn().mockResolvedValue({ status: 202, data: null }),
  getSession: vi.fn().mockResolvedValue({ ok: false, data: null }),
}));

const wrapper = ({ children }) => <ConversationProvider>{children}</ConversationProvider>;

/** The three hooks ChatPage wires together, in the same shape. */
function useLifecycleUnderTest() {
  const messages = useChatMessages();
  const polls = usePollRegistry();
  return { messages, ...useSessionLifecycle({ messages, polls }) };
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe('the post-session follow-up prompt', () => {
  it('asks "anything else?" after a story completes with its report ready', () => {
    const { result } = renderHook(useLifecycleUnderTest, { wrapper });

    act(() => {
      result.current.renderCompleted({
        id: 's1',
        state: 'completed',
        agent_key: 'record_stories',
        report_url: 'https://example.test/story.pdf',
      }, 'Record Stories');
    });

    const contents = result.current.messages.items.map((i) => i.content);
    expect(contents).toEqual([COPY.greeting, COPY.storyReady, COPY.sessionFollowUp]);
  });

  it('asks it for a discussion too, and BELOW the completion bubble even while the PDF lags', () => {
    const { result } = renderHook(useLifecycleUnderTest, { wrapper });

    act(() => {
      result.current.renderCompleted({
        id: 's1',
        state: 'completed',
        agent_key: 'capture_discussion',
        report_url: null,
      }, 'Capture Discussions');
    });

    const items = result.current.messages.items;
    expect(items.map((i) => i.content)).toEqual([
      COPY.greeting,
      COPY.discussionChecking,
      COPY.sessionFollowUp,
    ]);
    // Same speaker as the completion bubble above it, and as the row the
    // backend stores -- otherwise the bubble changes attribution on reload.
    expect(items[2].agentName).toBe('Capture Discussions');
  });

  it('is NOT re-appended on replay -- the stored message carries it instead', () => {
    const { result } = renderHook(useLifecycleUnderTest, { wrapper });

    act(() => {
      result.current.messages.append('agent', { content: 'interview turn', agentSessionId: 's1' });
      result.current.messages.append('agent', { content: 'a later agent' });
    });
    act(() => {
      result.current.renderCompleted(
        { id: 's1', state: 'completed', agent_key: 'record_stories', report_url: 'u' },
        'Record Stories',
        's1',
      );
    });

    const contents = result.current.messages.items.map((i) => i.content);
    expect(contents).toEqual([COPY.greeting, 'interview turn', COPY.storyReady, 'a later agent']);
    // The transcript replay already contains it as a stored assistant message;
    // appending another here would double it on every resumed conversation.
    expect(contents).not.toContain(COPY.sessionFollowUp);
  });
});
