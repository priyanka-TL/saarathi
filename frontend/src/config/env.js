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

import { DEFAULT_AUTH_MODES, parseAuthModes } from '../utils/authModes';

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

/**
 * Login/register/OTP go DIRECTLY to ELEVATE's user service from the browser
 * -- never proxied through Saarthi's own backend, so ELEVATE's base URL and
 * tenant id are config here, not a Saarthi API path. Saarthi's backend only
 * ever validates the JWT this produces; it never issues or mints one.
 * See src/api/elevateAuth.js.
 */
export const ELEVATE_BASE_URL = readConfig('ELEVATE_BASE_URL');
export const ELEVATE_TENANT_ID = readConfig('ELEVATE_TENANT_ID') || 'saarthi';

/**
 * The STATIC default for which login modes LoginPage shows -- used before
 * ELEVATE's branding response loads, and again if it fails or the tenant
 * declares none. Once branding loads, its own `allowed_auth_mode` (see
 * api/elevateAuth.js::extractBranding) takes over as the source of truth;
 * this is only ever the fallback, never re-consulted after that.
 */
const configuredAuthModes = parseAuthModes(readConfig('AUTH_MODES'));
export const AUTH_MODES = configuredAuthModes.length ? configuredAuthModes : DEFAULT_AUTH_MODES;

/** A boolean from config. Unset ('') keeps `fallback` -- see PROFILE_POPUP_ENABLED. */
function readFlag(key, fallback) {
  const raw = readConfig(key).toLowerCase();
  if (!raw) return fallback;
  return !['false', '0', 'off', 'no'].includes(raw);
}

/**
 * Whether to POP UP the profile form after login when mandatory fields are
 * missing. DEFAULT ON: an incomplete profile is the case this exists for, so
 * an operator who configures nothing gets the intended behaviour.
 *
 * OFF DOES NOT MEAN "NO PROFILE FEATURE". The sidebar's Profile section and its
 * Update button are unaffected -- this flag governs the unprompted dialog only,
 * so turning it off makes updating voluntary rather than impossible.
 *
 * Deliberately frontend-only: there is no backend `PROFILE_ENABLED` beside it
 * (see the note in backend/app/core/settings.py). Whether to interrupt someone
 * is a decision about this UI, and two switches could disagree. Whether the
 * feature can work at all is a different question, answered by the backend's
 * ELEVATE_BASE_URL -- unset, /api/profile returns 503 and the section hides
 * itself regardless of this value.
 */
export const PROFILE_POPUP_ENABLED = readFlag('PROFILE_POPUP_ENABLED', true);
