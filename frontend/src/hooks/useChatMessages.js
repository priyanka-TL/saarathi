import { useCallback, useReducer, useRef } from 'react';

import { COPY } from '../constants';

/**
 * The chat transcript, as a reducer over an array of discriminated items.
 *
 * main.js built this by appending DOM nodes imperatively, which allowed one
 * thing a naive React port loses: `_renderCompletedUI` called
 * `anchorEl.after(msg)` to place a completion notice at the point in the
 * timeline where THAT session ended, not at the bottom. INSERT_AFTER_SESSION
 * reproduces it. Without it, reopening a conversation where an early story
 * finished and a later agent kept talking puts the Download-PDF link below the
 * later agent's messages.
 *
 * Item shape:
 *   { id, kind, content, agentName, timestamp, options, selectedOptionId,
 *     agentSessionId, readOnly, reportUrl, retryText, sessionId }
 *
 * kind is one of:
 *   'user' | 'agent' | 'system' | 'context-switch' | 'error-retry'
 *   | 'session-finalizing' | 'session-complete' | 'session-checking'
 */

let seq = 0;
const nextId = () => `item-${++seq}`;

export function makeItem(kind, fields = {}) {
  return {
    id: nextId(),
    kind,
    content: '',
    agentName: null,
    timestamp: new Date(),
    options: null,
    selectedOptionId: null,
    attachments: null,
    agentSessionId: null,
    readOnly: false,
    reportUrl: null,
    retryText: null,
    sessionId: null,
    ...fields,
  };
}

/** The seed greeting, which lived as static markup in the Flask template. */
export function greetingItem() {
  return makeItem('system', { content: COPY.greeting });
}

function reducer(state, action) {
  switch (action.type) {
    case 'APPEND':
      return [...state, action.item];

    case 'INSERT_AFTER_SESSION': {
      // Find the LAST item belonging to that session and splice in after it.
      // Falls back to appending when the session has no messages in view.
      let index = -1;
      for (let i = state.length - 1; i >= 0; i -= 1) {
        if (state[i].agentSessionId === action.sessionId) {
          index = i;
          break;
        }
      }
      if (index === -1) return [...state, action.item];
      return [...state.slice(0, index + 1), action.item, ...state.slice(index + 1)];
    }

    case 'REPLACE':
      return state.map((it) => (it.id === action.id ? { ...it, ...action.patch } : it));

    case 'REMOVE':
      return state.filter((it) => it.id !== action.id);

    // The original found the finalizing notice by a fixed DOM id
    // (#session-finalizing-notice) because there is only ever one at a time.
    // Removing by kind is the same operation without needing a DOM handle.
    case 'REMOVE_KIND':
      return state.filter((it) => it.kind !== action.kind);

    // Ported from _disableAllOptions(): marks EVERY options group in the pane
    // read-only, not just the most recent one. Called at the top of
    // sendMessage AND again inside the option click handler -- both, exactly
    // as the original did, which is what closes the click -> POST window.
    case 'DISABLE_ALL_OPTIONS':
      return state.map((it) => (it.options ? { ...it, readOnly: true } : it));

    case 'RESET_TO_GREETING':
      return [greetingItem()];

    case 'SET':
      return action.items;

    default:
      return state;
  }
}

export function useChatMessages() {
  const [items, dispatch] = useReducer(reducer, undefined, () => [greetingItem()]);

  // Lets async callbacks read the current transcript without re-subscribing.
  const itemsRef = useRef(items);
  itemsRef.current = items;

  const append = useCallback((kind, fields) => {
    const item = makeItem(kind, fields);
    dispatch({ type: 'APPEND', item });
    return item;
  }, []);

  const insertAfterSession = useCallback((sessionId, kind, fields) => {
    const item = makeItem(kind, { ...fields, agentSessionId: sessionId });
    dispatch({ type: 'INSERT_AFTER_SESSION', sessionId, item });
    return item;
  }, []);

  const replace = useCallback((id, patch) => dispatch({ type: 'REPLACE', id, patch }), []);
  const remove = useCallback((id) => dispatch({ type: 'REMOVE', id }), []);
  const removeKind = useCallback((kind) => dispatch({ type: 'REMOVE_KIND', kind }), []);
  const disableAllOptions = useCallback(() => dispatch({ type: 'DISABLE_ALL_OPTIONS' }), []);
  const resetToGreeting = useCallback(() => dispatch({ type: 'RESET_TO_GREETING' }), []);
  const setItems = useCallback((next) => dispatch({ type: 'SET', items: next }), []);

  return {
    items,
    itemsRef,
    append,
    insertAfterSession,
    replace,
    remove,
    removeKind,
    disableAllOptions,
    resetToGreeting,
    setItems,
  };
}
