import { useCallback, useState } from 'react';

import { listAgents } from '../api/agents';

/**
 * The agent catalogue.
 *
 * GET /api/agents returns a BARE ARRAY whose first element is synthetic
 * (`{name: "Saarthi", description}` with NO `key`) and means "let the server
 * route me". Entries without a `key` are dropped here, which is what removes
 * it from the manual list.
 */
export function useAgents() {
  const [agents, setAgents] = useState([]);

  const loadAgents = useCallback(async () => {
    try {
      const { data } = await listAgents();
      setAgents(Array.isArray(data) ? data.filter((a) => a.key) : []);
    } catch {
      setAgents([]);
    }
  }, []);

  return { agents, loadAgents };
}
