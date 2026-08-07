import { useRef } from 'react';

import { SendIcon } from '../icons';
import MicButton from './MicButton';
import { COPY } from '../../constants';

/**
 * The composer.
 *
 * TWO SUBTLE BEHAVIOURS THAT ARE EASY TO "FIX" INTO SOMETHING ELSE:
 *
 * 1. Enter-vs-Send validation asymmetry. The textarea is `required`, and the
 *    Send button is a real `type="submit"`, so clicking it with an empty box
 *    triggers native constraint validation and shows the browser's "Please
 *    fill out this field" bubble -- the handler never runs. Enter instead
 *    DISPATCHES a synthetic submit event, and a dispatched event skips
 *    constraint validation, so it reaches the handler and is caught by its own
 *    empty check.
 *
 *    That is why this uses `form.dispatchEvent(new Event('submit', ...))` and
 *    NOT `form.requestSubmit()`. requestSubmit() *does* run validation, which
 *    would silently make Enter behave like the button.
 *
 * 2. Autoresize. On input the height is set to 'auto' then to scrollHeight. On
 *    submit it is reset to 'auto' and deliberately NOT re-measured, which is
 *    what makes it snap back to the CSS min-height of one row. Kept as
 *    imperative style writes; a controlled height value rounds differently.
 *
 * The mic sits INSIDE the form but is `type="button"`, so it never submits.
 * Neither behaviour above is affected: a transcript arrives through the same
 * `onChange` a keystroke would, so autoresize and the `required` textarea keep
 * working exactly as before.
 */
export default function ChatInput({ value, onChange, onSubmit, disabled, voice }) {
  const formRef = useRef(null);
  const textareaRef = useRef(null);

  function handleInput(e) {
    onChange(e.target.value);
    e.target.style.height = 'auto';
    e.target.style.height = `${e.target.scrollHeight}px`;
  }

  // Voice is mid-flight: recording, or transcribing what was just recorded.
  // Send stays shut for BOTH. Stopping the recording is not the moment the
  // message exists -- the transcript is still in the air, and the composer is
  // empty until it lands, so enabling Send on stop would offer to send nothing.
  const voiceBusy = Boolean(voice?.isRecording || voice?.isTranscribing);

  function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      // Enter is blocked for the same reason the button is. It has to be
      // checked here as well: a dispatched submit event skips the disabled
      // button entirely, so `disabled` alone would leave the keyboard as an
      // open back door into the exact state we are guarding against.
      if (voiceBusy) return;
      // Synthetic submit: bypasses constraint validation, exactly as before.
      formRef.current?.dispatchEvent(
        new Event('submit', { cancelable: true, bubbles: true }),
      );
    }
    // Shift+Enter falls through and inserts a newline. There are no other
    // keyboard shortcuts anywhere in this app.
  }

  function handleSubmit(e) {
    e.preventDefault();
    onSubmit();
    // Reset WITHOUT re-measuring, so it collapses to one row.
    if (textareaRef.current) textareaRef.current.style.height = 'auto';
  }

  const showMic = voice?.supported && !voice.voiceDisabled;

  return (
    <footer className="chat-input-area">
      {voice?.error && (
        <div className="voice-error" role="status">
          {voice.error}
          <button type="button" className="voice-error-dismiss" onClick={voice.clearError}>
            ✕
          </button>
        </div>
      )}
      <form id="chat-form" ref={formRef} onSubmit={handleSubmit}>
        <textarea
          id="user-input"
          ref={textareaRef}
          rows="1"
          // Nudges the user toward the mic once it is actually usable, and
          // stays byte-identical to the original otherwise.
          placeholder={showMic ? 'Type or speak here.' : 'Type here.'}
          required
          value={value}
          // Held open while transcribing so the user can start typing instead
          // of waiting -- the transcript replaces what is there when it lands.
          disabled={disabled}
          onChange={handleInput}
          onKeyDown={handleKeyDown}
        />
        {showMic && (
          <MicButton
            isRecording={voice.isRecording}
            isTranscribing={voice.isTranscribing}
            disabled={disabled}
            onToggle={voice.toggle}
          />
        )}
        <button
          type="submit"
          id="send-btn"
          disabled={voiceBusy}
          // The button is icon-only at <=768px (style.css:1136), so the label
          // that would otherwise explain the disabled state is not on screen.
          title={voiceBusy ? COPY.sendBlockedByVoice : undefined}
        >
          <SendIcon />
          <span>Send</span>
        </button>
      </form>
    </footer>
  );
}
