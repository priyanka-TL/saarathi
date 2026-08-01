import * as endpoints from './endpoints';
import { get, post } from './http';

/** GET /api/sessions/{id} -- polled while a session is `finalizing`. */
export function getSession(sessionId) {
  return get(endpoints.session(sessionId));
}

/**
 * GET /api/sessions/{id}/report.
 *
 * 200 -> {report_url, media_type, story_id}
 * 202 -> {retry_after: 5}   (no envelope; keep polling)
 */
export function getReport(sessionId) {
  return get(endpoints.sessionReport(sessionId));
}

/**
 * POST /api/sessions/{id}/resume -- recover a turn we stopped listening for,
 * WITHOUT re-sending it.
 *
 * Three outcomes with three different key sets:
 *   200 {outcome: 'answered',       response, session}
 *   202 {outcome: 'pending',        retry_after}      -- no session, no response
 *   200 {outcome: 'not_delivered',  can_resend: true} -- no session
 */
export function resumeSession(sessionId) {
  return post(endpoints.sessionResume(sessionId), undefined, { headers: {} });
}
