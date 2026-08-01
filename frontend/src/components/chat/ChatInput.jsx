import { useRef } from 'react';

import { SendIcon } from '../icons';

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
 */
export default function ChatInput({ value, onChange, onSubmit, disabled }) {
  const formRef = useRef(null);
  const textareaRef = useRef(null);

  function handleInput(e) {
    onChange(e.target.value);
    e.target.style.height = 'auto';
    e.target.style.height = `${e.target.scrollHeight}px`;
  }

  function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
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

  return (
    <footer className="chat-input-area">
      <form id="chat-form" ref={formRef} onSubmit={handleSubmit}>
        <textarea
          id="user-input"
          ref={textareaRef}
          rows="1"
          placeholder="Type here."
          required
          value={value}
          disabled={disabled}
          onChange={handleInput}
          onKeyDown={handleKeyDown}
        />
        <button type="submit" id="send-btn">
          <SendIcon />
          <span>Send</span>
        </button>
      </form>
    </footer>
  );
}
