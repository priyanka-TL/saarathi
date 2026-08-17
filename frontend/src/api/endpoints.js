/**
 * Every backend path this app calls, in one place.
 *
 * These are RELATIVE and stay that way. The configurable part of a request URL
 * -- origin, service prefix, and the /api mount point -- is resolved once as
 * APPLICATION_API_BASE_URL (src/config/env.js) and handed to axios as
 * `baseURL` (src/api/http.js). A path here is only the route's identity on
 * the backend, matching the decorator in backend/app/routers/*.py.
 *
 * So: to point the app at another environment, change the base URL
 * (config.js or .env). Nothing in this file changes.
 */

export const AGENTS = 'agents';

// Presentation config for the sidebar -- its ONLY source. A 404 here means the
// ADVANCED panel renders no capability cards.
export const UI_CAPABILITIES = 'ui/capabilities';

export const CHAT = 'chat';
export const RESET = 'reset';

// The caller's own ELEVATE profile -- GET reads it, PATCH updates it. No user
// id in the path: the bearer token IS the identity, so a caller can only ever
// reach their own. Answers 503 PROFILE_UNAVAILABLE when the backend has no
// ELEVATE_BASE_URL, which is how the UI knows to hide the Profile section.
export const PROFILE = 'profile';

export const CONVERSATIONS = 'conversations';
export const conversationMessages = (conversationId) =>
  `conversations/${conversationId}/messages`;

// Voice. All three answer 503 VOICE_DISABLED when the backend has
// VOICE_ENABLED=0, which is how the UI knows to hide the mic and speaker.
export const VOICE_UPLOAD_URL = 'voice/upload-url';
export const VOICE_TRANSCRIBE = 'voice/transcribe';
export const VOICE_SPEAK = 'voice/speak';

export const session = (sessionId) => `sessions/${sessionId}`;
export const sessionReport = (sessionId) => `sessions/${sessionId}/report`;
export const sessionResume = (sessionId) => `sessions/${sessionId}/resume`;
