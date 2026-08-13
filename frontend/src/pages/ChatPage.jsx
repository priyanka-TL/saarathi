import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { resetConversation as resetConversationApi } from '../api/chat';
import ChatInput from '../components/chat/ChatInput.jsx';
import MessageList from '../components/chat/MessageList.jsx';
import TypingIndicator from '../components/chat/TypingIndicator.jsx';
import WorkflowBanner from '../components/chat/WorkflowBanner.jsx';
import { useToast } from '../components/common/Toast.jsx';
import ProfileGate from '../components/profile/ProfileGate.jsx';
import Sidebar from '../components/sidebar/Sidebar.jsx';
import { ACTION_TYPES } from '../config/capabilities';
import { COPY, MOBILE_MAX_WIDTH } from '../constants';
import { useConversationContext } from '../context/ConversationContext.jsx';
import { useAgents } from '../hooks/useAgents';
import { useCapabilities } from '../hooks/useCapabilities';
import { useChatMessages } from '../hooks/useChatMessages';
import { useConversation } from '../hooks/useConversation';
import { usePollRegistry } from '../hooks/usePollRegistry';
import { useRecentConversations } from '../hooks/useRecentConversations';
import { useSendMessage } from '../hooks/useSendMessage';
import { useSessionLifecycle } from '../hooks/useSessionLifecycle';
import { useSpeech } from '../hooks/useSpeech';
import { useVoiceRecorder } from '../hooks/useVoiceRecorder';
import AppLayout from '../layouts/AppLayout.jsx';
import { runResume } from '../services/resumeFlow';
import { renderAgentHtml } from '../utils/markdown';
import { readTheme, readVoiceLanguage, writeVoiceLanguage } from '../utils/storage';

const HIDDEN_BANNER = {
  hidden: true,
  title: null,
  stops: null,
  currentIndex: 0,
  label: COPY.homeContext,
  subLabel: COPY.emptySubContext,
};

export default function ChatPage() {
  const {
    conversationId,
    conversationIdRef,
    setConversationId,
    isBusy,
    isBusyNow,
    currentAgentKeyRef,
    lastSessionIdRef,
    lastSentTextRef,
  } = useConversationContext();

  const messages = useChatMessages();
  const polls = usePollRegistry();
  const { agents, loadAgents } = useAgents();
  const { capabilities, loadCapabilities } = useCapabilities();
  const { conversations, loadRecent, upsert } = useRecentConversations();
  const toast = useToast();

  const [banner, setBanner] = useState(HIDDEN_BANNER);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [draft, setDraft] = useState('');
  const [activeCard, setActiveCard] = useState(null);
  const [activeAgentKey, setActiveAgentKey] = useState(null);
  const [voiceLanguage, setVoiceLanguageState] = useState(readVoiceLanguage);

  // --- voice --------------------------------------------------------------
  const setVoiceLanguage = useCallback((language) => {
    setVoiceLanguageState(language);
    writeVoiceLanguage(language);
  }, []);

  /**
   * The current conversation id, creating one if there is none yet.
   *
   * For voice: a recording has to be filed under a conversation, and a fresh
   * tab has none until New Chat is clicked or a message is sent. Rather than
   * hide the mic until then -- which made the first message, the one you would
   * most want to speak, the one message you could not -- the conversation is
   * created on demand.
   *
   * NOT `startNewConversation`, deliberately: that also resets the transcript
   * to the greeting and clears flow/session state, which would be a bizarre
   * side effect of pressing the mic. This only ensures an id exists. The
   * server reuses an already-empty conversation, so it does not accumulate
   * rows when someone starts and abandons several recordings.
   */
  const ensureConversation = useCallback(async () => {
    if (conversationIdRef.current) return conversationIdRef.current;
    try {
      const { data } = await resetConversationApi();
      const id = data?.conversation_id ?? null;
      setConversationId(id);
      return id;
    } catch {
      return null;
    }
  }, [conversationIdRef, setConversationId]);

  // The transcript REPLACES the draft rather than appending to it: it is the
  // whole of what the user just said, and appending would silently concatenate
  // two takes when someone re-records.
  const voice = useVoiceRecorder({
    conversationId,
    ensureConversation,
    language: voiceLanguage,
    onTranscript: setDraft,
  });

  const speech = useSpeech({ language: voiceLanguage });

  // --- banner ------------------------------------------------------------
  const setContextBanner = useCallback(
    (label, subLabel, stops = null, title = null, currentIndex = 0) => {
      setBanner({ hidden: false, label, subLabel, stops, title, currentIndex });
    },
    [],
  );

  /**
   * The ONE place a server `flow` payload becomes the header, so the live-turn
   * path and the resume path cannot drift apart -- they did once, which is
   * exactly why reopening a conversation from history used to lose its
   * breadcrumb.
   */
  const applyFlowBanner = useCallback(
    (agentName, flow, fallbackSubLabel) => {
      const stops = flow && Array.isArray(flow.stops) ? flow.stops : null;
      if (stops && stops.length > 0) {
        setContextBanner(agentName, agentName, stops, flow.title, flow.current_index);
      } else if (agentName) {
        setContextBanner(agentName, fallbackSubLabel || agentName);
      } else {
        setContextBanner(COPY.homeContext, COPY.emptySubContext);
      }
    },
    [setContextBanner],
  );

  const clearActiveItems = useCallback(() => {
    setActiveCard(null);
    setActiveAgentKey(null);
  }, []);

  // --- session lifecycle --------------------------------------------------
  const sessionLifecycle = useSessionLifecycle({ messages, polls });

  // --- turn loop ----------------------------------------------------------
  const sendMessage = useSendMessage({
    messages,
    onFlow: applyFlowBanner,
    onUpsertConversation: upsert,
    onSession: sessionLifecycle.handleSession,
  });

  const { startNewConversation, loadConversationHistory } = useConversation({
    messages,
    polls,
    onFlow: applyFlowBanner,
    onClearActive: clearActiveItems,
    onSession: sessionLifecycle,
  });

  // --- boot ---------------------------------------------------------------
  // One effect, firing in the same ORDER as the original's DOMContentLoaded
  // block: agents, then recent conversations, then history restore. Completion
  // order is race-dependent in both; initiation order is what must match.
  const bootedRef = useRef(false);
  useEffect(() => {
    if (bootedRef.current) return;
    bootedRef.current = true;

    // Boot-read only. The toggle button is commented out in the original
    // markup, so dark mode is unreachable from the UI -- but a previously
    // stored preference still applies.
    if (readTheme() === 'dark') {
      document.documentElement.setAttribute('data-theme', 'dark');
    }

    loadAgents();
    loadRecent();
    // Last of the three, and the only one whose failure is invisible: the
    // sidebar already rendered from config.js / the bundled default.
    loadCapabilities();
    if (conversationId) loadConversationHistory(conversationId);
    // Intentionally runs once; `conversationId` is read from storage at mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // --- interactions -------------------------------------------------------
  const closeSidebarOnMobile = useCallback(() => {
    // The literal comparison the original used, deliberately not a matchMedia
    // hook -- the two behave differently on resize.
    if (window.innerWidth <= MOBILE_MAX_WIDTH) setSidebarOpen(false);
  }, []);

  const handleSubmit = useCallback(() => {
    const text = draft.trim();
    if (!text || isBusyNow()) return;
    setDraft('');
    messages.append('user', { content: text });
    sendMessage(text);
  }, [draft, isBusyNow, messages, sendMessage]);

  const handleSelectOption = useCallback(
    (opt) => {
      if (isBusyNow()) return;
      // Disable every group immediately -- before the POST -- so a slow
      // network cannot let a double-click through. sendMessage does this too;
      // doing it here as well closes the window between click and send.
      messages.disableAllOptions();
      messages.append('user', { content: opt.label });
      sendMessage(opt.value, opt.id);
    },
    [isBusyNow, messages, sendMessage],
  );

  /** A capability action button: full reset, pin the agent, autostart. */
  const handleActivateCapability = useCallback(
    async (action, { capabilityId, label }) => {
      // MUST await: startNewConversation assigns the new id, and sending
      // before it resolves would post against the conversation being left.
      await startNewConversation();
      clearActiveItems();
      // The card that owns the button lights up. Before capabilities were
      // configuration this was the literal 'capability'; the value is only
      // ever compared against a card's own id, so behaviour is unchanged.
      setActiveCard(capabilityId);
      currentAgentKeyRef.current = action.agentKey;
      setContextBanner(label, COPY.storyCapture);
      closeSidebarOnMobile();
      if (action.autostart) {
        // autostart:true so the server does not title the conversation from it.
        await sendMessage(action.autostart, null, { autostart: true });
      }
    },
    [
      clearActiveItems,
      closeSidebarOnMobile,
      currentAgentKeyRef,
      sendMessage,
      setContextBanner,
      startNewConversation,
    ],
  );

  /** The capability CARDS are display-only: they never route. */
  const handleSelectDisplayCard = useCallback(
    (action, { capabilityId, label }) => {
      clearActiveItems();
      setActiveCard(capabilityId);
      setContextBanner(label, label);
      closeSidebarOnMobile();
    },
    [clearActiveItems, closeSidebarOnMobile, setContextBanner],
  );

  /**
   * A capability that cannot be entered yet.
   *
   * Does exactly one thing: shows the sidebar toast. In particular it does NOT
   * close the sidebar on mobile (that would hide the toast it just raised),
   * does not touch the banner, does not clear the active card, and issues no
   * request. "Non-intrusive, and navigates nowhere."
   */
  const handleComingSoon = useCallback(
    (action, { label }) => {
      toast.show(action.message || `${label} ${COPY.comingSoonSuffix}`);
    },
    [toast],
  );

  /**
   * type -> handler. The ONE bridge between configuration and behaviour.
   *
   * Config selects from this table; it can never introduce a behaviour that is
   * not in it. So a new capability that reuses an existing type is a pure data
   * change, while a genuinely new KIND of action is one entry here -- which is
   * the right place for that decision to be visible.
   */
  const capabilityActions = useMemo(
    () => ({
      [ACTION_TYPES.startAgent]: handleActivateCapability,
      [ACTION_TYPES.displayCard]: handleSelectDisplayCard,
      [ACTION_TYPES.comingSoon]: handleComingSoon,
      [ACTION_TYPES.none]: () => {},
    }),
    [handleActivateCapability, handleComingSoon, handleSelectDisplayCard],
  );

  const handleRunAction = useCallback(
    (action, context) => {
      const run = capabilityActions[action?.type];
      // Unknown types are already normalised to 'none', so this only guards
      // against a future action type reaching an older bundle.
      if (!run) return undefined;
      return run(action, context);
    },
    [capabilityActions],
  );

  const handleSelectAgent = useCallback(
    async (agent) => {
      if (isBusyNow()) return;
      await startNewConversation();
      clearActiveItems();
      setActiveAgentKey(agent.key);
      currentAgentKeyRef.current = agent.key;
      setContextBanner(agent.name, COPY.agentInteraction);
      closeSidebarOnMobile();
    },
    [
      clearActiveItems,
      closeSidebarOnMobile,
      currentAgentKeyRef,
      isBusyNow,
      setContextBanner,
      startNewConversation,
    ],
  );

  const handleSelectConversation = useCallback(
    (id) => {
      if (isBusyNow()) return;
      clearActiveItems();
      loadConversationHistory(id);
      closeSidebarOnMobile();
    },
    [clearActiveItems, closeSidebarOnMobile, isBusyNow, loadConversationHistory],
  );

  const handleNewChat = useCallback(() => {
    if (isBusyNow()) return;
    startNewConversation();
    closeSidebarOnMobile();
  }, [closeSidebarOnMobile, isBusyNow, startNewConversation]);

  // --- recovery -----------------------------------------------------------
  const handleRetry = useCallback(
    async (item) => {
      messages.remove(item.id);
      await sendMessage(item.retryText ?? lastSentTextRef.current);
    },
    [lastSentTextRef, messages, sendMessage],
  );

  const handleResume = useCallback(
    async (item) => {
      const sessionId = lastSessionIdRef.current;
      if (!sessionId) return false;

      const result = await runResume(sessionId);
      if (!result.settled) return false;

      messages.remove(item.id);
      if (result.outcome === 'answered') {
        messages.append('agent', {
          content: result.text,
          html: renderAgentHtml(result.text),
          // Options are NOT recoverable over REST -- the resume endpoint
          // returns an empty array by design.
          agentName: null,
        });
        if (result.session) sessionLifecycle.handleSession(result.session);
      } else if (result.canResend) {
        // The ONLY case where re-submitting the user's text is safe.
        await sendMessage(item.retryText ?? lastSentTextRef.current);
      }
      return true;
    },
    [lastSentTextRef, lastSessionIdRef, messages, sendMessage, sessionLifecycle],
  );

  return (
    <>
    <AppLayout
      sidebarOpen={sidebarOpen}
      onToggleSidebar={() => setSidebarOpen((o) => !o)}
      sidebar={
        <Sidebar
          open={sidebarOpen}
          onClose={() => setSidebarOpen(false)}
          conversations={conversations}
          activeConversationId={conversationId}
          onSelectConversation={handleSelectConversation}
          onNewChat={handleNewChat}
          agents={agents}
          capabilities={capabilities}
          activeCard={activeCard}
          activeAgentKey={activeAgentKey}
          onRunAction={handleRunAction}
          onSelectAgent={handleSelectAgent}
          isBusy={isBusyNow}
          toast={toast}
          voiceLanguage={voiceLanguage}
          onVoiceLanguageChange={setVoiceLanguage}
          // `supported` alone -- can this browser record. It no longer implies
          // an existing conversation (see useVoiceRecorder), so the picker and
          // the mic ask the same question again and this is back to one flag.
          // `voiceDisabled` stays out: it only turns true mid-session after a
          // 503, and the stored language preference is valid regardless.
          voiceAvailable={voice.supported}
        />
      }
      banner={<WorkflowBanner banner={banner} />}
      chatCard={
        <>
          <MessageList
            items={messages.items}
            hasRemoteSession={Boolean(lastSessionIdRef.current)}
            onSelectOption={handleSelectOption}
            onRetry={handleRetry}
            onResume={handleResume}
            speech={speech}
          />
          <TypingIndicator visible={isBusy} />
          <ChatInput
            value={draft}
            onChange={setDraft}
            onSubmit={handleSubmit}
            disabled={isBusy}
            voice={voice}
          />
        </>
      }
    />

    {/*
      HERE, not in AppLayout or RequireAuth. AppLayout takes named slots
      (sidebar / banner / chatCard) rather than free children, and documents
      that .sidebar-overlay must stay its last child. RequireAuth is rendered
      directly, with no providers, by requireAuth.test.jsx -- mounting a
      context consumer there would break it.

      This is the first authenticated screen, which is what "after login" means
      in practice. Renders nothing unless the profile is genuinely incomplete
      and the operator left the popup on.
    */}
    <ProfileGate />
    </>
  );
}
