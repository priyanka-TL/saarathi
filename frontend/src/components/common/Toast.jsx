import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * A transient, non-intrusive notice pinned to the bottom of the sidebar.
 *
 * Used for capabilities that cannot be entered yet (status 'coming_soon').
 * Deliberately NOT a chat message: the transcript is conversation content, and
 * UI chrome does not belong in it -- a "coming soon" line there would survive
 * in the scrollback and be indistinguishable from something an agent said.
 */

const DEFAULT_DURATION_MS = 3000;

/**
 * Toast state. `show` restarts the timer on every call, so clicking a
 * coming-soon card repeatedly keeps the notice up rather than letting an
 * earlier timer dismiss it mid-sequence.
 */
export function useToast(durationMs = DEFAULT_DURATION_MS) {
  const [message, setMessage] = useState(null);
  const timerRef = useRef(null);

  const clearTimer = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const dismiss = useCallback(() => {
    clearTimer();
    setMessage(null);
  }, [clearTimer]);

  const show = useCallback(
    (text) => {
      if (!text) return;
      clearTimer();
      setMessage(text);
      timerRef.current = setTimeout(() => {
        timerRef.current = null;
        setMessage(null);
      }, durationMs);
    },
    [clearTimer, durationMs],
  );

  // A pending timer would call setState on an unmounted component.
  useEffect(() => clearTimer, [clearTimer]);

  return { message, show, dismiss };
}

/**
 * `role="status"` + aria-live="polite" so a screen reader announces the notice
 * without interrupting whatever it is reading -- the announcement has to match
 * how unobtrusive the visual is.
 *
 * Renders nothing at all when there is no message, so it takes no space in the
 * sidebar's flex column.
 */
export default function Toast({ message, onDismiss }) {
  if (!message) return null;

  return (
    <div className="sidebar-toast" role="status" aria-live="polite" onClick={onDismiss}>
      {message}
    </div>
  );
}
