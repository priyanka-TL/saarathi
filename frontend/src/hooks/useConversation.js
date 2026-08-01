import { useCallback } from 'react';

import { resetConversation as resetConversationApi } from '../api/chat';
import { getMessages } from '../api/conversations';
import { COPY } from '../constants';
import { useConversationContext } from '../context/ConversationContext.jsx';
import { makeItem } from './useChatMessages';
import { renderAgentHtml } from '../utils/markdown';

/**
 * Conversation switching: reset, history replay, and forgetting a dead id.
 *
 * Switching conversations is the SAME state boundary that reset guards, and
 * everything reset clears has to be cleared here too. Left behind,
 * `lastSessionId` points at the PREVIOUS conversation's interview -- so a
 * timeout in the new one offers "Check for reply" and happily writes a
 * recovered turn into the conversation the user just left.
 */
export function useConversation({ messages, polls, onFlow, onClearActive, onSession }) {
  const {
    conversationIdRef,
    setConversationId,
    lastAgentRef,
    lastSessionIdRef,
    lastSessionAgentKeyRef,
    resetConversationScopedState,
  } = useConversationContext();

  /** A stored id that no longer resolves (different login, wiped DB, not ours). */
  const forgetConversation = useCallback(() => {
    setConversationId(null);
    messages.resetToGreeting();
    onFlow(null, null);
    onClearActive();
  }, [messages, onClearActive, onFlow, setConversationId]);

  /** POST /api/reset, then adopt the id the server hands back. */
  const startNewConversation = useCallback(async () => {
    polls.clearAll();
    try {
      const { data } = await resetConversationApi();
      // Adopt the returned id explicitly rather than clearing -- the server
      // reuses an already-empty conversation when the user has one, so
      // repeated resets do not accumulate dead rows.
      setConversationId(data?.conversation_id ?? null);
    } catch {
      setConversationId(null);
    }
    messages.resetToGreeting();
    resetConversationScopedState();
    onFlow(null, null);
    onClearActive();
  }, [messages, onClearActive, onFlow, polls, resetConversationScopedState, setConversationId]);

  /** Replay a conversation's full transcript plus its session state. */
  const loadConversationHistory = useCallback(
    async (id) => {
      polls.clearAll();
      resetConversationScopedState();

      let result;
      try {
        result = await getMessages(id);
      } catch {
        return;
      }

      if (result.status === 404) {
        // Leaving a dead id in storage meant the next message re-sent it and
        // the server materialised a NEW conversation under that
        // caller-supplied id.
        forgetConversation();
        return;
      }
      if (!result.ok || !result.data) return;

      const data = result.data;
      let lastAgentName = null;
      // Which session each message belongs to, so a completion notice can be
      // anchored to ITS point in the timeline rather than appended after
      // everything else.
      const agentNameBySession = new Map();

      const items = data.messages.map((m) => {
        const kind = m.role === 'assistant' ? 'agent' : 'user';
        if (m.role === 'assistant') lastAgentName = m.agent_name;
        if (m.agent_session_id && m.agent_name) {
          agentNameBySession.set(m.agent_session_id, m.agent_name);
        }
        return makeItem(kind, {
          content: m.content,
          html: kind === 'agent' ? renderAgentHtml(m.content) : null,
          agentName: m.agent_name,
          timestamp: new Date(m.created_at),
          agentSessionId: m.agent_session_id,
          // Replayed groups are decided, never live: disabled, with the chosen
          // one marked. No handler is attached at all.
          options: m.role === 'assistant' && m.options && m.options.length ? m.options : null,
          selectedOptionId: m.selected_option_id,
          readOnly: true,
        });
      });

      messages.setItems(items);

      setConversationId(id);
      // Restore the speaker the transcript ended on, or every resumed
      // conversation opens with a spurious "Switched context to X" pill.
      lastAgentRef.current = lastAgentName;

      // Restore the FULL agent journey, not just the last speaker -- the
      // server derives `flow` from the same message sequence a live turn uses.
      onFlow(lastAgentName, data.flow, COPY.resumedConversation);

      // Replay the session state the transcript cannot carry: the completion
      // notice is generated, not stored.
      const sessions = data.sessions || [];

      // EVERY finished session, not just the newest -- a conversation that
      // moved on to another agent used to hide the completed story's download
      // button. And NOT only the ones that already have a report_url:
      // finalising deliberately completes with report_url = null when Mitra's
      // PDF generation lags, and the completed handler polls for it.
      sessions.forEach((s) => {
        if (s.state === 'completed') {
          onSession.renderCompleted(s, agentNameBySession.get(s.id) || lastAgentName, s.id);
        }
      });

      // Only the newest session can still be in flight.
      const latest = sessions[sessions.length - 1];
      if (latest && latest.state === 'finalizing') {
        lastSessionIdRef.current = latest.id;
        lastSessionAgentKeyRef.current = latest.agent_key || null;
        onSession.handleSession(latest, agentNameBySession.get(latest.id) || lastAgentName);
      } else if (latest && latest.state !== 'completed') {
        // An interview mid-flight: remember it so a timeout recovers via
        // /resume instead of re-sending.
        lastSessionIdRef.current = latest.id;
        lastSessionAgentKeyRef.current = latest.agent_key || null;
      }
    },
    [
      forgetConversation,
      lastAgentRef,
      lastSessionAgentKeyRef,
      lastSessionIdRef,
      messages,
      onFlow,
      onSession,
      polls,
      resetConversationScopedState,
      setConversationId,
    ],
  );

  return { startNewConversation, loadConversationHistory, forgetConversation, conversationIdRef };
}
