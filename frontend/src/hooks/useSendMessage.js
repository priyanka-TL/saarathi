import { useCallback } from 'react';

import { sendTurn } from '../api/chat';
import { COPY } from '../constants';
import { useConversationContext } from '../context/ConversationContext.jsx';
import { renderAgentHtml } from '../utils/markdown';

/**
 * One conversational turn: the port of main.js's `sendMessage()`.
 *
 * THE MUTEX
 * ---------
 * `acquireBusy()` is a synchronous ref flip, not a state update. React state
 * is asynchronous and batched, so a state-based guard would let two rapid
 * clicks both pass the check -- and two user messages in flight is precisely
 * what Mitra merges into one, destroying an answer with no error surfaced
 * anywhere. Every entry point (form submit, option click, capability button,
 * agent select, conversation select, New Chat) checks it.
 *
 * ROUTING HANDBACK
 * ----------------
 * `currentAgentKey` is cleared to null after every SUCCESSFUL turn, so the
 * next turn is routed by the server's five-gate router rather than staying
 * pinned to whatever the user last clicked. The client never re-pins.
 */
export function useSendMessage({ messages, onFlow, onUpsertConversation, onSession }) {
  const {
    conversationIdRef,
    setConversationId,
    acquireBusy,
    releaseBusy,
    currentAgentKeyRef,
    lastAgentRef,
    lastSentTextRef,
    lastSessionIdRef,
    lastSessionAgentKeyRef,
  } = useConversationContext();

  return useCallback(
    async (text, optionId = null, { autostart = false } = {}) => {
      if (!text || !text.trim()) return;
      if (!acquireBusy()) return;

      lastSentTextRef.current = text;
      // Disable every option group BEFORE the request, so a slow network
      // cannot let a second click through.
      messages.disableAllOptions();

      try {
        const { data } = await sendTurn({
          message: text,
          agentKey: currentAgentKeyRef.current,
          conversationId: conversationIdRef.current,
          optionId,
          autostart,
        });

        if (data?.conversation_id) setConversationId(data.conversation_id);

        if (data?.status === 'success') {
          // 1. Hand routing back to the server for the next turn.
          currentAgentKeyRef.current = null;

          // 2. A centred pill whenever the responding agent changed.
          if (lastAgentRef.current !== data.agent_name) {
            messages.append('context-switch', {
              content: `${COPY.contextSwitchPrefix}${data.agent_name}`,
            });
            lastAgentRef.current = data.agent_name;
          }

          // 3. The reply itself (markdown -> sanitized HTML).
          const agentItem = messages.append('agent', {
            content: data.response,
            html: renderAgentHtml(data.response),
            agentName: data.agent_name,
            options: data.options && data.options.length > 0 ? data.options : null,
            agentSessionId: data.session?.id ?? null,
          });

          // 4. Session lifecycle, if this agent runs one.
          if (data.session) {
            lastSessionIdRef.current = data.session.id;
            lastSessionAgentKeyRef.current = data.session.agent_key;
            onSession(data.session);
          }

          // 5. Patch the sidebar locally -- deliberately no refetch.
          onUpsertConversation({
            id: data.conversation_id,
            title: data.flow && data.flow.title,
            last_message_at: new Date().toISOString(),
          });

          // 6. The workflow banner.
          onFlow(data.agent_name, data.flow);

          return agentItem;
        }

        // A timeout leaves the session alive, so offer recovery rather than a
        // dead end. Everything else is a plain system message.
        if (data?.error_code === 'UPSTREAM_TIMEOUT') {
          messages.append('error-retry', {
            content: COPY.timeoutError,
            retryText: text,
          });
        } else {
          messages.append('system', { content: data?.error || COPY.genericError });
        }
      } catch {
        // Transport-level failure only: axios is configured not to reject on
        // HTTP status, so reaching here means the request never completed.
        messages.append('system', { content: COPY.networkError });
      } finally {
        releaseBusy();
      }
      return null;
    },
    [
      acquireBusy,
      releaseBusy,
      conversationIdRef,
      setConversationId,
      currentAgentKeyRef,
      lastAgentRef,
      lastSentTextRef,
      lastSessionIdRef,
      lastSessionAgentKeyRef,
      messages,
      onFlow,
      onSession,
      onUpsertConversation,
    ],
  );
}
