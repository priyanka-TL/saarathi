import axios from 'axios';

import { ELEVATE_BASE_URL, ELEVATE_TENANT_ID } from '../config/env';
import { parseAuthModes } from '../utils/authModes';

/**
 * The ELEVATE user service, called DIRECTLY from the browser.
 *
 * Login, registration and OTP are never proxied through Saarthi's own
 * backend -- Saarthi's only job is validating the JWT this produces (see
 * backend's `ELEVATE_JWT_SECRET`), never issuing or minting one. Kept as a
 * separate axios instance from `./http.js`: different origin, and it must
 * never carry Saarthi's own Authorization header.
 *
 * `tenantid` is fixed once here rather than passed per call -- every ELEVATE
 * call this app makes is against the one configured tenant.
 */
const elevateHttp = axios.create({
  baseURL: ELEVATE_BASE_URL,
  headers: { 'Content-Type': 'application/json', tenantid: ELEVATE_TENANT_ID },
  validateStatus: () => true,
});

/**
 * REFUSED, NOT DEFAULTED. Axios treats an empty `baseURL` as "resolve against
 * the current page" -- so a missing ELEVATE_BASE_URL would otherwise send
 * every login/OTP/register call to wherever the SPA itself is hosted
 * (observed live: a "Send OTP" click POSTing to
 * `http://localhost:5173/user/v1/account/generateOtp`, the Vite dev server,
 * which naturally 404s with no clue why). Failing loudly here turns that into
 * an immediate, readable error on the login page instead.
 */
function missingBaseUrlResult() {
  return {
    status: 0,
    ok: false,
    data: {
      message:
        'ELEVATE_BASE_URL is not configured (set APPLICATION_ELEVATE_BASE_URL ' +
        'in frontend/.env, or ELEVATE_BASE_URL in public/config.js).',
    },
  };
}

async function call(config) {
  if (!ELEVATE_BASE_URL) return missingBaseUrlResult();
  const response = await elevateHttp.request(config);
  return {
    status: response.status,
    ok: response.status >= 200 && response.status < 300,
    data: response.data ?? null,
  };
}

/**
 * ELEVATE's `/user/v1/account/*` surface. `LOGIN_PATH` and
 * `REGISTRATION_OTP_PATH` are pinned from a live network capture against
 * qa.elevate-saathi.shikshalokam.org. There is no SEPARATE login-OTP
 * endpoint: ELEVATE mints an OTP for a phone number the same way whether the
 * caller ends up logging in or auto-registering, so login's "Send OTP" also
 * goes to `registrationOtp` -- confirmed live, not a guess.
 * `REGISTER_PATH` was NOT captured live -- confirm it against a real signup
 * before relying on it past this POC.
 */
const LOGIN_PATH = '/user/v1/account/login';
const REGISTRATION_OTP_PATH = '/user/v1/account/registrationOtp';
const REGISTER_PATH = '/user/v1/account/create';
const BRANDING_PATH = '/user/v1/public/branding';

export function sendLoginOtp({ phone, phone_code }) {
  return call({ method: 'POST', url: REGISTRATION_OTP_PATH, data: { phone, phone_code } });
}

export function sendRegistrationOtp({ phone, phone_code }) {
  return call({ method: 'POST', url: REGISTRATION_OTP_PATH, data: { phone, phone_code } });
}

export function loginWithPassword({ identifier, password }) {
  return call({ method: 'POST', url: LOGIN_PATH, data: { identifier, password } });
}

export function loginWithOtp({ identifier, otp, phone_code }) {
  return call({ method: 'POST', url: LOGIN_PATH, data: { identifier, otp: Number(otp), phone_code } });
}

export function register({ phone, phone_code, otp, name, password }) {
  const data = { phone, phone_code, otp, name };
  if (password) data.password = password;
  return call({ method: 'POST', url: REGISTER_PATH, data });
}

/** The tenant's public branding (name, logo, description) -- no auth needed. */
export function getBranding() {
  return call({ method: 'GET', url: BRANDING_PATH });
}

/**
 * Pulls the access token out of ELEVATE's response envelope.
 *
 * Several key spellings are tried because the envelope is ELEVATE's, not
 * ours: `result.access_token` / `.accessToken` / `.token`, or nested under
 * `result.tokens`. Some builds return the JWT wrapped in quotes.
 */
export function extractToken(data) {
  const result = (data && typeof data.result === 'object' && data.result) || data || {};
  const tokens = (result && typeof result.tokens === 'object' && result.tokens) || {};
  const raw =
    result.access_token || result.accessToken || result.token ||
    tokens.access || tokens.access_token || tokens.accessToken || '';
  if (typeof raw !== 'string') return '';
  const trimmed = raw.trim();
  if (trimmed.length >= 2 && trimmed.startsWith('"') && trimmed.endsWith('"')) {
    return trimmed.slice(1, -1);
  }
  return trimmed;
}

/** ELEVATE's own error text, when a call fails. */
export function extractMessage(data) {
  return (data && (data.message || data.error_message || data.detail)) || '';
}

/** The tenant's name, logo URL and allowed login modes, for the login page.
 * `name`/`logoUrl` are blank (never null/undefined) so a caller can `||` a
 * static fallback in either failure case -- ELEVATE unreachable, or a
 * tenant with no logo configured. `allowedAuthModes` is `[]` rather than a
 * default for the same reason `parseAuthModes` itself has no default: `[]`
 * IS the signal "the tenant declared nothing here" -- LoginPage reads that
 * as "keep the static config default" rather than "show no modes at all". */
export function extractBranding(data) {
  const result = (data && typeof data.result === 'object' && data.result) || {};
  const configuration = (result && typeof result.configuration === 'object' && result.configuration) || {};
  return {
    name: result.name || '',
    logoUrl: result.logo || '',
    allowedAuthModes: parseAuthModes(configuration.allowed_auth_mode),
  };
}

/** A small, display-only summary -- never the whole envelope. */
export function extractUserSummary(data) {
  const result = (data && typeof data.result === 'object' && data.result) || data || {};
  const user = (result && typeof result.user === 'object' && result.user) || result;
  return {
    name: user.name || '',
    phone: user.phone || '',
    email: user.email || '',
  };
}
