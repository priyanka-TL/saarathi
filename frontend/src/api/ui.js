import * as endpoints from './endpoints';
import { get } from './http';

/**
 * GET /api/ui/capabilities -> the capability document the sidebar renders.
 *
 * THE ONLY SOURCE for the ADVANCED panel's cards; the frontend keeps no copy.
 * A backend that does not serve this route answers 404 and the panel shows no
 * capabilities -- see src/hooks/useCapabilities.js.
 *
 * Nothing here treats a non-2xx as exceptional: `http.request` returns the
 * status rather than throwing, and the caller decides.
 */
export function getCapabilities() {
  return get(endpoints.UI_CAPABILITIES);
}
