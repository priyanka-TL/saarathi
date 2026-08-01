import { useCallback, useEffect, useRef } from 'react';

/**
 * A registry of live interval ids, ported from main.js's `_pollSessionTimers`.
 *
 * The original used a Set rather than a single id on purpose: a report poll
 * and a session-state poll can be in flight at the same time, and
 * `_pollReport`'s own stop must cancel only its own timer while
 * `_clearSessionPoll()` cancels everything.
 *
 * Clearing ALL of them on conversation switch is load-bearing. Both
 * resetConversation and loadConversationHistory do it, and both also null the
 * remembered session ids. Miss either and a timeout in the NEW conversation
 * POSTs /api/sessions/{old_id}/resume, writing a recovered turn into the
 * conversation the user just left.
 */
export function usePollRegistry() {
  const timers = useRef(new Set());

  const track = useCallback((timerId) => {
    timers.current.add(timerId);
    return timerId;
  }, []);

  const stopOne = useCallback((timerId) => {
    clearInterval(timerId);
    timers.current.delete(timerId);
  }, []);

  const clearAll = useCallback(() => {
    timers.current.forEach(clearInterval);
    timers.current.clear();
  }, []);

  /**
   * Starts a tracked setInterval and returns a stop() that cancels only this
   * timer -- the same "let timer; const stop = () => stopOne(timer); timer =
   * track(setInterval(...))" shape callers otherwise have to write by hand.
   */
  const trackedInterval = useCallback(
    (fn, ms) => {
      let timerId;
      const stop = () => stopOne(timerId);
      timerId = track(setInterval(fn, ms));
      return stop;
    },
    [track, stopOne],
  );

  // The original leaked on navigate-away because vanilla JS had nowhere to
  // hang teardown. Here there is.
  useEffect(() => () => {
    timers.current.forEach(clearInterval);
    timers.current.clear();
  }, []);

  return { track, stopOne, clearAll, trackedInterval };
}
