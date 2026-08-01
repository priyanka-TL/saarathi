import { useLayoutEffect, useRef } from 'react';

import ErrorWithRetry from './ErrorWithRetry';
import Message from './Message';

/**
 * The scrolling transcript.
 *
 * Auto-scroll is UNCONDITIONAL, matching the original's `scrollToBottom()` --
 * there is no "user has scrolled up" detection, and adding one would be a
 * behaviour change.
 *
 * useLayoutEffect rather than useEffect so the scroll lands in the same frame
 * the new message paints; useEffect would show one frame at the old offset.
 */
export default function MessageList({ items, hasRemoteSession, onSelectOption, onRetry, onResume }) {
  const ref = useRef(null);

  useLayoutEffect(() => {
    const el = ref.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [items.length]);

  return (
    <main id="chat-messages" className="chat-messages" ref={ref}>
      {items.map((item) =>
        item.kind === 'error-retry' ? (
          <ErrorWithRetry
            key={item.id}
            item={item}
            hasRemoteSession={hasRemoteSession}
            onRetry={onRetry}
            onResume={onResume}
          />
        ) : (
          <Message key={item.id} item={item} onSelectOption={onSelectOption} />
        ),
      )}
    </main>
  );
}
