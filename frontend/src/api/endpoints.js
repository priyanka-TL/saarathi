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

export const CHAT = 'chat';
export const RESET = 'reset';

export const CONVERSATIONS = 'conversations';
export const conversationMessages = (conversationId) =>
  `conversations/${conversationId}/messages`;

export const session = (sessionId) => `sessions/${sessionId}`;
export const sessionReport = (sessionId) => `sessions/${sessionId}/report`;
export const sessionResume = (sessionId) => `sessions/${sessionId}/resume`;
