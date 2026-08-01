import * as endpoints from './endpoints';
import { post } from './http';

/**
 * POST /api/chat -- one conversational turn.
 *
 * `agent_name` duplicates `agent_key` for one release of backward compat, as
 * the original client did; the server reads `agent_key` first.
 *
 * Optional fields are omitted rather than sent as null, matching the original
 * request bodies exactly.
 */
export function sendTurn({ message, agentKey, conversationId, optionId, autostart }) {
  const body = {
    message,
    agent_key: agentKey ?? null,
    agent_name: agentKey ?? null,
  };
  if (optionId) body.option_id = optionId;
  if (autostart) body.autostart = true;
  if (conversationId) body.conversation_id = conversationId;
  return post(endpoints.CHAT, body);
}

/**
 * POST /api/reset -- abandon any open session, close its Mitra channel, and
 * start a fresh conversation.
 *
 * Sent with NO body, exactly as the original `fetch('/api/reset', {method:
 * 'POST'})` did; the server's body parsing is deliberately tolerant of that.
 */
export function resetConversation() {
  return post(endpoints.RESET, undefined, { headers: {} });
}
