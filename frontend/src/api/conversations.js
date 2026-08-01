import * as endpoints from './endpoints';
import { get } from './http';

/** GET /api/conversations?limit= -> {conversations: [...]}. Server clamps to 1..20. */
export function listRecent(limit = 20) {
  return get(endpoints.CONVERSATIONS, { params: { limit } });
}

/**
 * GET /api/conversations/{id}/messages -> {conversation_id, flow, sessions, messages}.
 *
 * A 404 here means "this conversation is gone or is not yours", and the caller
 * treats it as a signal to forget the stored id rather than as an error.
 */
export function getMessages(conversationId) {
  return get(endpoints.conversationMessages(conversationId));
}
