import * as endpoints from './endpoints';
import { get, patch } from './http';

/**
 * The caller's own profile, from ELEVATE via Saarthi's backend.
 *
 * Unlike login (src/api/elevateAuth.js, which talks to ELEVATE DIRECTLY), these
 * go through Saarthi's own API. The backend holds the ELEVATE base URL, decides
 * which fields are mandatory, and normalises ELEVATE's attribute shapes -- so
 * this layer never learns ELEVATE's schema and the completeness rule lives in
 * exactly one place.
 *
 * Both return `{profile, is_complete, missing_fields}` on success -- ONE shape
 * from both routes, so a caller never has to merge a PATCH echo into what it
 * already held. The PATCH response is a fresh read, not an echo: ELEVATE can
 * normalise or silently drop a value, and the UI must not report a field as
 * filled when it did not stick.
 *
 * Nothing here treats a non-2xx as exceptional: `http.request` returns the
 * status rather than throwing, and the caller decides. The status that matters
 * is 503 PROFILE_UNAVAILABLE -- the backend has no ELEVATE_BASE_URL configured,
 * which means hide the Profile section rather than show an error.
 */
export function getProfile() {
  return get(endpoints.PROFILE);
}

/** Update one or more of name/role/school_name/district/state. Sparse: keys
 * left out are not sent, so they cannot be blanked upstream. */
export function updateProfile(fields) {
  return patch(endpoints.PROFILE, fields);
}
