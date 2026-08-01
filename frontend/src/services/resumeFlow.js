import { resumeSession } from '../api/sessions';
import { RESUME_DEFAULT_RETRY_S, RESUME_MAX_ATTEMPTS } from '../constants';

/**
 * Drive POST /api/sessions/{id}/resume to a conclusion.
 *
 * This is the SAFE recovery path after a turn timeout during a Mitra
 * interview: it asks Mitra what actually happened instead of re-sending. A
 * blind re-send is what merges two consecutive user messages inside Mitra and
 * destroys an answer.
 *
 * Resolves to one of:
 *   { settled: true,  outcome: 'answered',       text, session }
 *   { settled: true,  outcome: 'not_delivered',  canResend: true }
 *   { settled: false }   -- still pending after RESUME_MAX_ATTEMPTS, or an error
 *
 * `settled: false` is what puts the button back into a re-checkable state
 * rather than declaring failure.
 */
export async function runResume(sessionId, attempt = 0) {
  if (attempt >= RESUME_MAX_ATTEMPTS) return { settled: false };

  let result;
  try {
    result = await resumeSession(sessionId);
  } catch {
    return { settled: false };
  }

  const { status, data } = result;

  // 202: Mitra is still generating. The server tells us how long to wait.
  if (status === 202) {
    const delayMs = (data?.retry_after || RESUME_DEFAULT_RETRY_S) * 1000;
    await new Promise((resolve) => {
      setTimeout(resolve, delayMs);
    });
    return runResume(sessionId, attempt + 1);
  }

  if (status !== 200 || !data) return { settled: false };

  if (data.outcome === 'answered') {
    return { settled: true, outcome: 'answered', text: data.response, session: data.session };
  }

  if (data.can_resend) {
    // The ONLY case where re-submitting the user's text is safe.
    return { settled: true, outcome: 'not_delivered', canResend: true };
  }

  return { settled: false };
}
