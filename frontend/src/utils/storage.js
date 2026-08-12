import { DEFAULT_VOICE_LANGUAGE, STORAGE_KEYS, VOICE_LANGUAGES } from '../constants';

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

/**
 * The voice language, in localStorage rather than sessionStorage.
 *
 * Unlike conversation identity, this is a preference about the PERSON, not the
 * tab: someone who speaks Kannada speaks it in every tab and on every visit,
 * and re-picking it each time would be the main friction in using voice at all.
 *
 * Validated on read against the list the app actually supports, so a stale
 * value left by an older build (or edited by hand) falls back instead of being
 * sent to the backend and rejected.
 */
export function readVoiceLanguage() {
  try {
    const stored = localStorage.getItem(STORAGE_KEYS.voiceLanguage);
    return VOICE_LANGUAGES.some((l) => l.value === stored) ? stored : DEFAULT_VOICE_LANGUAGE;
  } catch {
    return DEFAULT_VOICE_LANGUAGE;
  }
}

export function writeVoiceLanguage(language) {
  try {
    localStorage.setItem(STORAGE_KEYS.voiceLanguage, language);
  } catch {
    /* storage unavailable; the choice simply won't survive a reload */
  }
}

/**
 * The ELEVATE access token from login, and the small user summary alongside
 * it. localStorage, like the voice language: a login is a preference about
 * the PERSON, surviving a new tab and a reload -- not per-tab like the
 * conversation id.
 */
export function readAuthToken() {
  try {
    return localStorage.getItem(STORAGE_KEYS.authToken);
  } catch {
    return null;
  }
}

export function readAuthUser() {
  try {
    const raw = localStorage.getItem(STORAGE_KEYS.authUser);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

/** Writes both together; clears both when `token` is falsy. */
export function writeAuth(token, user) {
  try {
    if (token) {
      localStorage.setItem(STORAGE_KEYS.authToken, token);
      localStorage.setItem(STORAGE_KEYS.authUser, JSON.stringify(user ?? null));
    } else {
      localStorage.removeItem(STORAGE_KEYS.authToken);
      localStorage.removeItem(STORAGE_KEYS.authUser);
    }
  } catch {
    /* storage unavailable; the session simply won't survive a reload */
  }
}
