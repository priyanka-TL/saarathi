/**
 * The two login modes ELEVATE supports, and how a raw value (env var or
 * ELEVATE's own `configuration.allowed_auth_mode`) turns into a validated
 * list of them.
 *
 * ONE PARSER, TWO CALLERS: src/config/env.js (the static frontend default)
 * and src/api/elevateAuth.js::extractBranding (the tenant's live setting).
 * Neither owns the mode strings or the validation -- this file does, so the
 * two can never drift into recognising a different set of names.
 */
export const AUTH_MODE = { PASSWORD: 'password', OTP: 'otp' };

const KNOWN_MODES = [AUTH_MODE.PASSWORD, AUTH_MODE.OTP];

/** Both modes -- today's behaviour, and the fallback when nothing else says
 * otherwise (unset config, or a tenant with no `allowed_auth_mode` at all). */
export const DEFAULT_AUTH_MODES = KNOWN_MODES;

/**
 * Normalises a raw modes value into the known, deduped list, in the order
 * given. Accepts either a comma-separated string (env vars) or an array
 * (ELEVATE's own JSON). Unrecognised entries are dropped rather than kept as
 * opaque strings -- a typo in config should mean "not enabled", never a
 * mode the rest of the app doesn't know how to render.
 *
 * Empty or entirely-unrecognised input returns `[]`. Deliberately no
 * fallback here -- each caller decides its OWN default (env.js falls back to
 * DEFAULT_AUTH_MODES; extractBranding does not, since "[]" is exactly what
 * tells LoginPage "the tenant said nothing, keep the static default").
 */
export function parseAuthModes(raw) {
  const list = Array.isArray(raw) ? raw : String(raw || '').split(',');
  const seen = new Set();
  const out = [];
  for (const item of list) {
    const mode = String(item ?? '').trim().toLowerCase();
    if (KNOWN_MODES.includes(mode) && !seen.has(mode)) {
      seen.add(mode);
      out.push(mode);
    }
  }
  return out;
}
