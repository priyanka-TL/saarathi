/**
 * RUNTIME configuration. Overwritten at deploy time; NOT bundled.
 *
 * Vite copies public/ to the dist root verbatim, so this file sits next to
 * index.html in the built artifact and is loaded before the app. That is what
 * lets ONE build serve development, QA and production: point it at the right
 * backend and reload -- no rebuild.
 *
 *   API_BASE_URL   complete backend base URL, INCLUDING the service prefix
 *                  and /api/, e.g. 'https://qa.example.org/saarathi-service/api/'.
 *                  '' => same origin as the page (relative requests).
 *
 * A blank value falls through to the build-time APPLICATION_API_BASE_URL
 * value from .env; deleting the file entirely is also safe (the app just uses
 * the build-time value). See src/config/env.js.
 */
window.__APP_CONFIG__ = {
  API_BASE_URL: '',
};
