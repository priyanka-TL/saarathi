import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ConversationProvider, useConversationContext } from '../context/ConversationContext.jsx';
import { useChatMessages } from '../hooks/useChatMessages';
import { useSendMessage } from '../hooks/useSendMessage';

// The one network boundary this hook has.
vi.mock('../api/chat', () => ({ sendTurn: vi.fn() }));
import { sendTurn } from '../api/chat';

const wrapper = ({ children }) => <ConversationProvider>{children}</ConversationProvider>;

/** Wires the hook the way ChatPage does, and exposes the pieces we assert on. */
function useHarness() {
  const messages = useChatMessages();
  const ctx = useConversationContext();
  const send = useSendMessage({
    messages,
    onFlow: vi.fn(),
    onUpsertConversation: vi.fn(),
    onSession: vi.fn(),
  });
  return { messages, ctx, send };
}

const okResponse = (overrides = {}) => ({
  status: 200,
  ok: true,
  data: {
    status: 'success',
    agent_name: 'General Support Agent',
    agent_key: 'general_support',
    agent_type: 'llm',
    response: 'hello back',
    conversation_id: 'conv-1',
    flow: { title: null, stops: [], current_index: -1 },
    options: [],
    session: null,
    ...overrides,
  },
});

beforeEach(() => vi.clearAllMocks());

describe('the _busy mutex', () => {
  it('admits exactly one turn when two are fired back to back', async () => {
    // A turn that stays in flight until we release it.
    let release;
    sendTurn.mockImplementation(
      () => new Promise((resolve) => { release = () => resolve(okResponse()); }),
    );

    const { result } = renderHook(useHarness, { wrapper });

    // Both calls happen in the SAME synchronous block -- no await between
    // them. A state-based guard would let both through, because a setState
    // has not been applied yet. This is the exact double-submit that makes
    // Mitra merge two user messages and destroy an answer.
    act(() => {
      result.current.send('first');
      result.current.send('second');
    });

    expect(sendTurn).toHaveBeenCalledTimes(1);
    expect(sendTurn.mock.calls[0][0].message).toBe('first');

    await act(async () => { release(); });
    await waitFor(() => expect(result.current.ctx.isBusy).toBe(false));

    // Once the first turn settles, a further turn IS admitted.
    sendTurn.mockResolvedValue(okResponse());
    await act(async () => { await result.current.send('third'); });
    expect(sendTurn).toHaveBeenCalledTimes(2);
  });

  it('releases the mutex even when the request throws', async () => {
    sendTurn.mockRejectedValue(new Error('network down'));
    const { result } = renderHook(useHarness, { wrapper });

    await act(async () => { await result.current.send('boom'); });

    expect(result.current.ctx.isBusy).toBe(false);
    expect(result.current.messages.items.at(-1).content).toBe('Network error. Please try again.');
  });
});

describe('routing handback', () => {
  it('clears currentAgentKey after a successful turn', async () => {
    sendTurn.mockResolvedValue(okResponse());
    const { result } = renderHook(useHarness, { wrapper });

    act(() => { result.current.ctx.currentAgentKeyRef.current = 'record_stories'; });
    await act(async () => { await result.current.send('hi'); });

    // The pinned key is sent with the turn...
    expect(sendTurn.mock.calls[0][0].agentKey).toBe('record_stories');
    // ...and then dropped, so the NEXT turn is routed by the server.
    expect(result.current.ctx.currentAgentKeyRef.current).toBeNull();
  });
});

describe('the context-switch pill', () => {
  it('appears when the responding agent changes, and not when it repeats', async () => {
    sendTurn.mockResolvedValue(okResponse());
    const { result } = renderHook(useHarness, { wrapper });

    await act(async () => { await result.current.send('one'); });
    let pills = result.current.messages.items.filter((i) => i.kind === 'context-switch');
    expect(pills).toHaveLength(1);
    expect(pills[0].content).toBe('Switched context to General Support Agent');

    await act(async () => { await result.current.send('two'); });
    pills = result.current.messages.items.filter((i) => i.kind === 'context-switch');
    expect(pills).toHaveLength(1); // same agent -- no second pill
  });
});

describe('error branches', () => {
  it('renders a recoverable error for UPSTREAM_TIMEOUT and a plain one otherwise', async () => {
    sendTurn.mockResolvedValue({
      status: 504, ok: false,
      data: { status: 'error', error: 'timed out', error_code: 'UPSTREAM_TIMEOUT' },
    });
    const { result } = renderHook(useHarness, { wrapper });
    await act(async () => { await result.current.send('slow'); });

    const last = result.current.messages.items.at(-1);
    expect(last.kind).toBe('error-retry');
    expect(last.retryText).toBe('slow');

    sendTurn.mockResolvedValue({
      status: 502, ok: false,
      data: { status: 'error', error: 'upstream is down', error_code: 'UPSTREAM_UNAVAILABLE' },
    });
    await act(async () => { await result.current.send('again'); });

    const next = result.current.messages.items.at(-1);
    expect(next.kind).toBe('system');
    expect(next.content).toBe('upstream is down');
  });
});
