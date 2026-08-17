import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import ChatInput from '../components/chat/ChatInput.jsx';

/**
 * The composer, and specifically the two ways a message can leave it.
 *
 * Send being disabled is only half a guard: Enter dispatches a synthetic submit
 * event that goes straight to the form and never consults the button's
 * `disabled` state. Both paths are pinned here, because fixing one and
 * forgetting the other is the obvious way this regresses.
 */

const voiceState = (over = {}) => ({
  supported: true,
  voiceDisabled: false,
  isRecording: false,
  isTranscribing: false,
  error: null,
  clearError: vi.fn(),
  toggle: vi.fn(),
  ...over,
});

function renderInput(over = {}) {
  const onSubmit = vi.fn();
  const utils = render(
    <ChatInput
      value="hello"
      onChange={vi.fn()}
      onSubmit={onSubmit}
      disabled={false}
      voice={voiceState(over)}
    />,
  );
  return { ...utils, onSubmit, send: document.querySelector('#send-btn') };
}

const pressEnter = () =>
  fireEvent.keyDown(document.querySelector('#user-input'), { key: 'Enter', shiftKey: false });

describe('ChatInput send gating', () => {
  it('Send is enabled when voice is idle', () => {
    const { send, onSubmit } = renderInput();
    expect(send.disabled).toBe(false);

    fireEvent.click(send);
    expect(onSubmit).toHaveBeenCalled();
  });

  it('Send is disabled while recording', () => {
    const { send } = renderInput({ isRecording: true });
    expect(send.disabled).toBe(true);
  });

  it('Send stays disabled while the transcript is still coming back', () => {
    /* Stopping the recording is not the moment the message exists -- the
       composer is empty until the transcript lands, so enabling Send on stop
       would offer to send nothing. */
    const { send } = renderInput({ isRecording: false, isTranscribing: true });
    expect(send.disabled).toBe(true);
  });

  it('Send is re-enabled once the transcript has landed', () => {
    const { send } = renderInput({ isRecording: false, isTranscribing: false });
    expect(send.disabled).toBe(false);
  });

  it('Enter cannot submit while recording', () => {
    /* The back door: a dispatched submit event skips the disabled button
       entirely, so `disabled` alone would leave the keyboard wide open. */
    const { onSubmit } = renderInput({ isRecording: true });
    pressEnter();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('Enter cannot submit while transcribing', () => {
    const { onSubmit } = renderInput({ isTranscribing: true });
    pressEnter();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('Enter still submits when voice is idle', () => {
    const { onSubmit } = renderInput();
    pressEnter();
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it('Shift+Enter never submits, recording or not', () => {
    const { onSubmit } = renderInput();
    fireEvent.keyDown(document.querySelector('#user-input'), { key: 'Enter', shiftKey: true });
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('the disabled Send carries a tooltip explaining why', () => {
    /* At <=768px the button is icon-only, so the label that would otherwise
       explain the state is not on screen. */
    const { send } = renderInput({ isRecording: true });
    expect(send.getAttribute('title')).toBe('Finish recording first');
  });

  it('gating does not apply when voice is unavailable at all', () => {
    const { send, onSubmit } = renderInput({ supported: false });
    expect(document.querySelector('#mic-btn')).toBeNull();
    expect(send.disabled).toBe(false);

    fireEvent.click(send);
    expect(onSubmit).toHaveBeenCalled();
  });

  it('renders with no voice prop at all', () => {
    /* ChatInput is used without the hook in tests and any caller that has not
       wired voice up; `voice?.isRecording` must not throw. */
    render(<ChatInput value="hi" onChange={vi.fn()} onSubmit={vi.fn()} disabled={false} />);
    expect(screen.getAllByText('Send').length).toBeGreaterThan(0);
  });
});
