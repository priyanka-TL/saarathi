import * as endpoints from './endpoints';
import { get } from './http';

/**
 * GET /api/agents -> a BARE JSON ARRAY.
 *
 * The first element is synthetic: `{name: "Saarthi", description: ...}` with
 * NO `key`. Callers use the absence of `key` to recognise the "let the server
 * route me" pseudo-agent and skip it when rendering the manual list.
 */
export function listAgents() {
  return get(endpoints.AGENTS);
}
