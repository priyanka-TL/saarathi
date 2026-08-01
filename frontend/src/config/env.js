/**
 * Application configuration. The ONE place a backend URL is resolved.
 *
 * Under Flask the SPA was served by the same origin as the API, so every fetch
 * was a bare path. The React app is a separate origin in development, so the
 * base URL becomes configuration.
 *
 * TWO SOURCES, IN PRIORITY ORDER
 * ------------------------------
 * 1. `window.__APP_CONFIG__`, set by config.js -- see public/config.js.
 *    Read at RUNTIME, so one built bundle can be promoted dev -> QA -> prod by
 *    editing a one-line file next to it.
 * 2. `import.meta.env.APPLICATION_API_BASE_URL`, from .env at BUILD time.
 *    Vite inlines this into the bundle, which is why it alone cannot repoint
 *    a deployed artifact.
 *
 * The value is the COMPLETE backend base URL, including the service prefix
 * and `/api/`, e.g. 'http://127.0.0.1:8000/saarathi-service/api/'. Every path
 * in src/api/endpoints.js is relative to it -- nothing appends a further
 * prefix. Empty from both leaves API_BASE_URL as '', i.e. relative paths --
 * correct when the bundle is served from the API's own origin.
 */

const runtimeConfig =
  (typeof window !== 'undefined' && window.__APP_CONFIG__) || {};

/** Runtime config wins; a blank value in either source falls through. */
function readConfig(key) {
  const runtimeValue = runtimeConfig[key];
  if (typeof runtimeValue === 'string' && runtimeValue.trim() !== '') {
    return runtimeValue.trim();
  }
  const buildValue = import.meta.env[`APPLICATION_${key}`];
  return typeof buildValue === 'string' ? buildValue.trim() : '';
}

/** What axios uses as its baseURL. Every request is built from this. */
export const API_BASE_URL = readConfig('API_BASE_URL');
