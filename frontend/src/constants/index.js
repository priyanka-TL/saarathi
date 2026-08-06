/**
 * Every literal the UI depends on, in one place.
 *
 * The user-visible strings are reproduced EXACTLY, including the unusual bits:
 * the en-dash sub-context placeholder, the two spaces after the download
 * arrow, and the HTML entity that rendered as a single ellipsis character.
 */

// --- storage -------------------------------------------------------------
// sessionStorage, NOT localStorage: conversation identity is per-tab on
// purpose. A reload restores the conversation; a new tab starts fresh.
export const STORAGE_KEYS = {
  conversationId: 'saarthi_cid',
  theme: 'theme',
};

// --- layout --------------------------------------------------------------
// Mirrors the single `@media (max-width: 768px)` breakpoint in style.css.
// Kept as a literal comparison against window.innerWidth (as the original
// did) rather than a matchMedia hook -- the two behave differently on resize.
export const MOBILE_MAX_WIDTH = 768;

// --- polling -------------------------------------------------------------
export const SESSION_POLL_MS = 2000;      // uncapped, by design
export const REPORT_POLL_MS = 3000;
export const REPORT_MAX_ATTEMPTS = 30;    // 30 x 3s = 90s
export const RESUME_MAX_ATTEMPTS = 10;
export const RESUME_DEFAULT_RETRY_S = 3;

// --- agents --------------------------------------------------------------
/*
 * Agents hidden from the manual agent list in the Advanced panel.
 *
 * With the three current YAML agents this means #agent-list renders EMPTY --
 * record_stories and capture_discussion are reached through the capability
 * buttons instead, and general_support is the router's default. That is
 * correct, current behaviour. Do not "fix" the empty list.
 */
export const SIDEBAR_HIDDEN_KEYS = new Set([
  'record_stories',
  'capture_discussion',
  'general_support',
]);

// The synthetic first entry from GET /api/agents has no `key`; it means
// "let the server route me".
export const AUTO_ROUTE_AGENT_NAME = 'Saarthi';

// --- copy ----------------------------------------------------------------
export const COPY = {
  greeting: 'Namaste. How can I help you today?',
  homeContext: 'Home',
  // Attribution shown on a USER bubble that was replayed from a stored
  // transcript rather than typed this session -- those items are the ones
  // useConversation marks `readOnly: true`. A live message shows its context
  // name instead. See Message.jsx.
  chatHistoryContext: 'Chat History',
  // U+2013 EN DASH, as in the original markup.
  emptySubContext: '–',
  defaultWorkflowTitle: 'Workflow Progress',
  agentInteraction: 'Agent interaction',
  storyCapture: 'Story capture',
  resumedConversation: 'Resumed conversation',
  newConversation: 'New conversation',
  noMessages: 'No messages yet',
  lastActivePrefix: 'Last active ',
  contextSwitchPrefix: 'Switched context to ',

  // Session lifecycle. The '…' are single U+2026 characters, matching the
  // `&hellip;` entities in the original.
  finalizing: 'Writing your story… This may take a moment.',
  storyReady: '✅ Your story is ready.',
  storyChecking: 'Your story is ready. Checking for the PDF report…',
  discussionReady: '✅ Your discussion report is ready.',
  discussionChecking: 'Your discussion report is ready. Checking for the PDF report…',
  storyFailed: 'Story capture could not be completed. Please try again.',
  discussionFailed: 'Capturing this discussion could not be completed. Please try again.',
  reportPending: 'PDF report is still being generated. Please check back later.',
  // Asked once a remote_flow session (story OR discussion) has completed, so
  // the transcript hands the turn back to the user instead of ending on a
  // download link. Rendered here live; STORED by the backend as a real
  // assistant message, so it must stay byte-identical to SESSION_FOLLOW_UP in
  // backend/app/services/orchestration.py or the wording changes on reload.
  // Pinned by backend/tests/guards/test_sync_contract.py.
  sessionFollowUp: 'Is there anything else I can help you with today?',
  // U+2B07 DOWNWARDS BLACK ARROW followed by TWO spaces.
  downloadReport: '⬇  Download PDF report',

  // Errors and recovery.
  networkError: 'Network error. Please try again.',
  genericError: 'An error occurred.',
  timeoutError: 'The request timed out. Your session is still active.',
  retry: 'Retry',
  checkForReply: 'Check for reply',
  checking: 'Checking…',
  checkAgain: 'Check again',

  // Appended to a capability's title when its config supplies no explicit
  // `action.message`. Capability TITLES are not here on purpose -- they are
  // configuration (src/config/capabilities.js), not fixed copy.
  comingSoonSuffix: 'is coming soon.',
};
