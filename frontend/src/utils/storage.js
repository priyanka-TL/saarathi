import { STORAGE_KEYS } from '../constants';

/**
 * sessionStorage access, guarded.
 *
 * sessionStorage (not localStorage) is deliberate: conversation identity is
 * per-tab. A reload restores it; a new tab starts a fresh conversation.
 *
 * Wrapped in try/catch because storage throws in private-browsing modes and a
 * chat app should degrade to "no resume on reload", not fail to boot.
 */
export function readConversationId() {
  try {
    return sessionStorage.getItem(STORAGE_KEYS.conversationId);
  } catch {
    return null;
  }
}

export function writeConversationId(id) {
  try {
    if (id) sessionStorage.setItem(STORAGE_KEYS.conversationId, id);
    else sessionStorage.removeItem(STORAGE_KEYS.conversationId);
  } catch {
    /* storage unavailable; conversation simply won't survive a reload */
  }
}

export function readTheme() {
  try {
    return localStorage.getItem(STORAGE_KEYS.theme);
  } catch {
    return null;
  }
}
