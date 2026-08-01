import { useCallback, useState } from 'react';

import { getCapabilities } from '../api/ui';
import { normalizeCapabilities } from '../config/capabilitySchema';

/**
 * The capability catalogue, from `GET /api/ui/capabilities` and nowhere else.
 *
 * ONE SOURCE, ON PURPOSE. The frontend carries no bundled copy and reads no
 * override out of config.js, so there is exactly one place a capability can be
 * added, reordered, hidden or disabled: the backend's
 * app/config/ui/capabilities.yaml. A second source would be a second truth --
 * an operator edits the YAML, sees no change, and cannot tell whether the
 * backend is stale or the bundle is winning.
 *
 * THE COST, STATED PLAINLY: when the request fails the panel renders NO
 * capability cards, and the Mitra interview entry points are unreachable until
 * the backend answers. That is the intended trade -- an empty panel is honest
 * about the backend being unavailable, where a bundled fallback would draw
 * buttons whose POST /api/reset is going to fail anyway.
 *
 * `agents` and `conversations` behave the same way on failure (empty list, no
 * error surfaced), so this is the sidebar's existing convention, not a new one.
 */
export function useCapabilities() {
  const [capabilities, setCapabilities] = useState([]);

  const loadCapabilities = useCallback(async () => {
    try {
      const { ok, data } = await getCapabilities();
      if (!ok) {
        // 404 => the backend serves no capability document. Nothing to draw.
        setCapabilities([]);
        return;
      }
      // null means the body was not a capability document at all; an empty
      // array is a valid instruction ("show none"). Both render nothing, but
      // only the second one is intentional on the server's part.
      setCapabilities(normalizeCapabilities(data) ?? []);
    } catch {
      // Server down, CORS, DNS.
      setCapabilities([]);
    }
  }, []);

  return { capabilities, loadCapabilities };
}
