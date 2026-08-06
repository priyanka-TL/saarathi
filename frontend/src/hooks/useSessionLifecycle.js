import { useCallback } from 'react';

import { getReport, getSession } from '../api/sessions';
import {
  COPY,
  REPORT_MAX_ATTEMPTS,
  REPORT_POLL_MS,
  SESSION_POLL_MS,
} from '../constants';
import { useConversationContext } from '../context/ConversationContext.jsx';

/**
 * Renders and drives the remote_flow (Mitra) session lifecycle:
 *
 *   finalizing -> spinner notice + poll GET /api/sessions/{id} every 2s
 *   completed  -> completion bubble, with a Download-PDF link or a report poll,
 *                 then a follow-up prompt (live completions only)
 *   failed     -> plain system error bubble
 *   abandoned  -> same
 *
 * The completion copy branches on session.agent_key, NOT on the display name.
 * It used to compare against the literal string 'Capture Discussions', and
 * that agent has already been renamed once -- the next rename would silently
 * revert discussions to "Your story is ready." with nothing to catch it.
 */
export function useSessionLifecycle({ messages, polls }) {
  const { lastAgentRef, lastSessionAgentKeyRef } = useConversationContext();

  /** Which wording this session gets. */
  const copyFor = useCallback(
    (session) => {
      const agentKey = session.agent_key || lastSessionAgentKeyRef.current;
      return agentKey === 'capture_discussion'
        ? {
            ready: COPY.discussionReady,
            checking: COPY.discussionChecking,
            failed: COPY.discussionFailed,
          }
        : { ready: COPY.storyReady, checking: COPY.storyChecking, failed: COPY.storyFailed };
    },
    [lastSessionAgentKeyRef],
  );

  /**
   * Poll GET /api/sessions/{id}/report until the PDF exists.
   *
   * Stops ONLY its own timer: a conversation can hold several completed
   * sessions still waiting on their PDF, and clearing all polls the moment the
   * first report lands would strand the rest.
   */
  const pollReport = useCallback(
    (sessionId, placeholderId, readyText) => {
      let attempts = 0;

      const check = async () => {
        attempts += 1;
        if (attempts > REPORT_MAX_ATTEMPTS) {
          stop();
          // Written into the message TEXT, not the whole content node -- the
          // latter also holds the timestamp/agent line.
          messages.replace(placeholderId, { content: COPY.reportPending });
          return;
        }
        try {
          const { status, data } = await getReport(sessionId);
          if (status === 200 && data?.report_url) {
            stop();
            messages.replace(placeholderId, {
              content: readyText,
              reportUrl: data.report_url,
            });
          }
          // 202 -> keep polling
        } catch {
          /* network hiccup -- try again next tick */
        }
      };

      const stop = polls.trackedInterval(check, REPORT_POLL_MS);
      // Check IMMEDIATELY as well. On a history replay the report is usually
      // long since generated and only the session row's report_url is stale,
      // so waiting a full tick would show "checking for the PDF report…" for
      // three seconds about a file that already exists.
      check();
    },
    [messages, polls],
  );

  /**
   * Render the "completed" state.
   *
   * `anchorSessionId` places the notice where that session actually ENDED
   * rather than at the bottom of a conversation that has since moved on to
   * another agent. Its ABSENCE is also what marks this as the live end of an
   * interview rather than a history replay -- see the follow-up below.
   */
  const renderCompleted = useCallback(
    (session, agentName = null, anchorSessionId = null) => {
      messages.removeKind('session-finalizing');
      // Attribute the bubble to the interview agent, like every bubble above
      // it; falling back to nothing would render it as 'Home', which reads as
      // a different speaker.
      const attribution = agentName || lastAgentRef.current;
      const { ready, checking } = copyFor(session);

      const fields = { agentName: attribution, agentSessionId: session.id };

      /*
       * Hand the turn back to the user.
       *
       * LIVE COMPLETIONS ONLY, and for a different reason than the notice
       * above: the backend STORES this one as a real assistant message
       * (OrchestrationService._record_session_follow_up), so on a replay it
       * arrives with the rest of the transcript. Appending it here as well
       * would show it twice on every resumed conversation.
       *
       * Attributed to the interview agent, matching the completion bubble
       * above it AND the stored row -- whose agent_id cannot be null anyway
       * (ck_conversation_messages_assistant_attribution). Anything else and
       * the bubble would change speaker when the user reloads.
       */
      const askFollowUp = () => {
        if (anchorSessionId) return;
        messages.append('system', {
          content: COPY.sessionFollowUp,
          agentName: attribution,
        });
      };

      if (session.report_url) {
        const item = anchorSessionId
          ? messages.insertAfterSession(anchorSessionId, 'session-complete', {
              ...fields,
              content: ready,
              reportUrl: session.report_url,
            })
          : messages.append('session-complete', {
              ...fields,
              content: ready,
              reportUrl: session.report_url,
            });
        askFollowUp();
        return item;
      }

      const placeholder = anchorSessionId
        ? messages.insertAfterSession(anchorSessionId, 'session-complete', {
            ...fields,
            content: checking,
          })
        : messages.append('session-complete', { ...fields, content: checking });
      // Before pollReport, so the prompt sits below the completion bubble. The
      // report lands via REPLACE on that bubble's id, which patches in place
      // and cannot reorder the two.
      askFollowUp();
      pollReport(session.id, placeholder.id, ready);
      return placeholder;
    },
    [copyFor, lastAgentRef, messages, pollReport],
  );

  /** Entry point: react to whatever state a session payload reports. */
  const handleSession = useCallback(
    (session, agentName = null) => {
      if (!session) return;

      if (session.state === 'finalizing') {
        messages.append('session-finalizing', {
          content: COPY.finalizing,
          agentName: agentName || lastAgentRef.current,
          agentSessionId: session.id,
        });

        const stop = polls.trackedInterval(async () => {
          try {
            const { ok, data } = await getSession(session.id);
            if (!ok || !data) return;
            if (data.state === 'completed') {
              stop();
              renderCompleted(data, agentName);
            } else if (data.state === 'failed' || data.state === 'abandoned') {
              stop();
              messages.removeKind('session-finalizing');
              messages.append('system', { content: copyFor(session).failed });
            }
          } catch {
            /* keep polling */
          }
        }, SESSION_POLL_MS);
      } else if (session.state === 'completed') {
        renderCompleted(session, agentName);
      }
    },
    [copyFor, lastAgentRef, messages, polls, renderCompleted],
  );

  return { handleSession, renderCompleted };
}
