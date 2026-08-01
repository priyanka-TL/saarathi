import { createContext, useCallback, useContext, useMemo, useRef, useState } from 'react';

import { readConversationId, writeConversationId } from '../utils/storage';

/**
 * Conversation-scoped state, ported from main.js's module-level `let`s.
 *
 * WHY MOST OF THESE ARE REFS, NOT STATE
 * -------------------------------------
 * Every one of them is read inside an async callback, mid-turn. React state
 * reads inside a closure see the value from the render that created the
 * closure, so a state-based port would act on stale values in exactly the
 * places that matter:
 *
 *   busy               a stale read lets a second turn through -- and two
 *                      user messages in flight is what Mitra merges,
 *                      DESTROYING an answer with no error anywhere. This one
 *                      must be a synchronous mutex.
 *   currentAgentKey    must be readable and clearable synchronously around the
 *                      POST; it is reset to null after every successful turn
 *                      so routing goes back to the server.
 *   lastAgent          drives the "Switched context to X" pill; a stale read
 *                      emits a spurious one.
 *   lastSessionId      its PRESENCE flips the retry button between
 *                      "Check for reply" and "Retry".
 *
 * `conversationId` is the exception: it is both rendered (to highlight the
 * active sidebar row) and read in callbacks, so it is state PLUS a ref mirror.
 */

const ConversationContext = createContext(null);

export function ConversationProvider({ children }) {
  // Rendered + read in callbacks.
  const [conversationId, setConversationIdState] = useState(() => readConversationId());
  const conversationIdRef = useRef(conversationId);

  // Rendered only.
  const [isBusy, setIsBusy] = useState(false);

  // Never rendered.
  const busyRef = useRef(false);
  const currentAgentKeyRef = useRef(null);
  const lastAgentRef = useRef(null);
  const lastSentTextRef = useRef(null);
  const lastSessionIdRef = useRef(null);
  const lastSessionAgentKeyRef = useRef(null);

  const setConversationId = useCallback((id) => {
    conversationIdRef.current = id;
    setConversationIdState(id);
    writeConversationId(id);
  }, []);

  /** The synchronous mutex. Returns false if a turn is already in flight. */
  const acquireBusy = useCallback(() => {
    if (busyRef.current) return false;
    busyRef.current = true;
    setIsBusy(true);
    return true;
  }, []);

  const releaseBusy = useCallback(() => {
    busyRef.current = false;
    setIsBusy(false);
  }, []);

  const isBusyNow = useCallback(() => busyRef.current, []);

  /** Everything that must be forgotten when the conversation changes. */
  const resetConversationScopedState = useCallback(() => {
    currentAgentKeyRef.current = null;
    lastAgentRef.current = null;
    lastSessionIdRef.current = null;
    lastSessionAgentKeyRef.current = null;
  }, []);

  const value = useMemo(
    () => ({
      conversationId,
      conversationIdRef,
      setConversationId,
      isBusy,
      isBusyNow,
      acquireBusy,
      releaseBusy,
      currentAgentKeyRef,
      lastAgentRef,
      lastSentTextRef,
      lastSessionIdRef,
      lastSessionAgentKeyRef,
      resetConversationScopedState,
    }),
    [
      conversationId,
      setConversationId,
      isBusy,
      isBusyNow,
      acquireBusy,
      releaseBusy,
      resetConversationScopedState,
    ],
  );

  return <ConversationContext.Provider value={value}>{children}</ConversationContext.Provider>;
}

export function useConversationContext() {
  const ctx = useContext(ConversationContext);
  if (!ctx) throw new Error('useConversationContext must be used inside ConversationProvider');
  return ctx;
}
