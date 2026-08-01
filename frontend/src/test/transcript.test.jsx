import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { act, renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import ErrorWithRetry from '../components/chat/ErrorWithRetry.jsx';
import MessageOptions from '../components/chat/MessageOptions.jsx';
import { useChatMessages } from '../hooks/useChatMessages';

describe('the retry-button label branch', () => {
  // Which label appears decides whether the click RE-SENDS the user's text or
  // merely asks Mitra what happened. Re-sending mid-interview merges two
  // consecutive user messages inside Mitra and destroys an answer, so this
  // branch is a data-safety control, not a cosmetic one.
  const item = { id: 'e1', content: 'The request timed out.', retryText: 'my answer' };

  it('offers "Retry" (re-send) when no remote session is in play', async () => {
    const onRetry = vi.fn();
    const onResume = vi.fn();
    render(
      <ErrorWithRetry item={item} hasRemoteSession={false} onRetry={onRetry} onResume={onResume} />,
    );
    const btn = screen.getByRole('button', { name: 'Retry' });
    await userEvent.click(btn);
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(onResume).not.toHaveBeenCalled();
  });

  it('offers "Check for reply" (read-only resume) during an interview', async () => {
    const onRetry = vi.fn();
    const onResume = vi.fn().mockResolvedValue(true);
    render(
      <ErrorWithRetry item={item} hasRemoteSession onRetry={onRetry} onResume={onResume} />,
    );
    const btn = screen.getByRole('button', { name: 'Check for reply' });
    await userEvent.click(btn);
    expect(onResume).toHaveBeenCalledTimes(1);
    expect(onRetry).not.toHaveBeenCalled();
  });

  it('falls back to "Check again" when the resume did not settle', async () => {
    const onResume = vi.fn().mockResolvedValue(false);
    render(
      <ErrorWithRetry item={item} hasRemoteSession onRetry={vi.fn()} onResume={onResume} />,
    );
    await userEvent.click(screen.getByRole('button', { name: 'Check for reply' }));
    expect(await screen.findByRole('button', { name: 'Check again' })).toBeInTheDocument();
  });
});

describe('option groups', () => {
  it('are inert and marked when replayed read-only', async () => {
    const onSelect = vi.fn();
    render(
      <MessageOptions
        options={[{ id: 'a', label: 'Yes', value: 'yes' }, { id: 'b', label: 'No', value: 'no' }]}
        readOnly
        selectedOptionId="a"
        onSelect={onSelect}
      />,
    );
    const yes = screen.getByRole('button', { name: 'Yes' });
    expect(yes).toBeDisabled();
    expect(yes).toHaveClass('option-btn--used', 'option-btn--selected');
    expect(screen.getByRole('button', { name: 'No' })).not.toHaveClass('option-btn--selected');
    await userEvent.click(yes);
    expect(onSelect).not.toHaveBeenCalled();
  });
});

describe('the transcript reducer', () => {
  it('anchors a completion notice to the session that produced it', () => {
    // Reproduces the original's anchorEl.after(msg): a story that finished
    // early in a conversation which later moved to another agent must keep its
    // Download-PDF link at ITS point in the timeline, not at the bottom.
    const { result } = renderHook(() => useChatMessages());

    act(() => {
      result.current.append('agent', { content: 'interview turn', agentSessionId: 's1' });
      result.current.append('agent', { content: 'later agent, unrelated' });
      result.current.append('agent', { content: 'later agent again' });
    });
    act(() => {
      result.current.insertAfterSession('s1', 'session-complete', { content: 'story ready' });
    });

    const contents = result.current.items.map((i) => i.content);
    expect(contents).toEqual([
      'Namaste. How can I help you today?',
      'interview turn',
      'story ready',            // <- anchored, NOT appended at the end
      'later agent, unrelated',
      'later agent again',
    ]);
  });

  it('disables EVERY option group at once, not just the newest', () => {
    const { result } = renderHook(() => useChatMessages());
    act(() => {
      result.current.append('agent', { options: [{ id: '1', label: 'A', value: 'a' }] });
      result.current.append('agent', { options: [{ id: '2', label: 'B', value: 'b' }] });
    });
    act(() => result.current.disableAllOptions());
    expect(result.current.items.filter((i) => i.options).every((i) => i.readOnly)).toBe(true);
  });
});
