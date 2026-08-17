/**
 * Every icon in the app, as inline SVG.
 *
 * Inlining is load-bearing, not a preference: all but two use
 * `stroke="currentColor"`, which picks up --text-primary / --primary-color
 * from the cascade. An <img src="*.svg"> cannot inherit a CSS colour, so the
 * icons would stop following the theme. That is why src/assets/ is empty.
 *
 * Geometry is copied verbatim from templates/index.html and main.js.
 */

// The shared attribute set on all but the two noted exceptions.
const stroked = {
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: '2',
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
};

export const CompassIcon = () => (
  <svg {...stroked} width="22" height="22">
    <circle cx="12" cy="12" r="10" />
    <polygon points="16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76" />
  </svg>
);

export const InfoIcon = () => (
  <svg {...stroked} width="12" height="12">
    <circle cx="12" cy="12" r="10" />
    <line x1="12" y1="16" x2="12" y2="12" />
    <line x1="12" y1="8" x2="12.01" y2="8" />
  </svg>
);

export const CloseIcon = () => (
  <svg {...stroked} width="16" height="16">
    <line x1="18" y1="6" x2="6" y2="18" />
    <line x1="6" y1="6" x2="18" y2="18" />
  </svg>
);

export const PlusIcon = () => (
  <svg {...stroked} width="16" height="16">
    <line x1="12" y1="5" x2="12" y2="19" />
    <line x1="5" y1="12" x2="19" y2="12" />
  </svg>
);

/** Used by both collapsibles; the rotation is applied by CSS, per container. */
export const ChevronDownIcon = ({ className }) => (
  <svg {...stroked} width="16" height="16" className={className}>
    <polyline points="6 9 12 15 18 9" />
  </svg>
);

export const BrainIcon = () => (
  <svg {...stroked} width="18" height="18" className="capability-icon">
    <path d="M11 2.2A6.5 6.5 0 0 0 4.5 8.7c0 2.2 1 4.2 2.7 5.4.6.4 1 1.1 1 1.8v2.6c0 1.2.9 2.2 2 2.4 2 .3 3.8-1.2 3.8-3.1v-2c0-.7.3-1.4.8-1.9.9-.9 1.5-2 1.5-3.3a6.5 6.5 0 0 0-6.5-6.5h0c.4 0 1.2 0 1.2 0z" />
  </svg>
);

export const GlobeIcon = () => (
  <svg {...stroked} width="18" height="18" className="highlight-icon">
    <circle cx="12" cy="12" r="10" />
    <line x1="2" y1="12" x2="22" y2="12" />
    <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
  </svg>
);

/**
 * EXCEPTION 1: stroke is the literal #4e2277, not currentColor, and not the
 * --primary-color variable either. That means it does NOT follow dark mode --
 * shipped behaviour, reproduced as-is.
 */
export const WorkflowIcon = () => (
  <svg
    className="workflow-icon"
    xmlns="http://www.w3.org/2000/svg"
    width="20"
    height="20"
    viewBox="0 0 24 24"
    fill="none"
    stroke="#4e2277"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <path d="M6 3v12" />
    <circle cx="6" cy="18" r="3" />
    <path d="M6 9a9 9 0 0 1 9 9" />
    <circle cx="15" cy="21" r="3" />
  </svg>
);

export const MenuIcon = () => (
  <svg {...stroked} width="24" height="24">
    <line x1="3" y1="12" x2="21" y2="12" />
    <line x1="3" y1="6" x2="21" y2="6" />
    <line x1="3" y1="18" x2="21" y2="18" />
  </svg>
);

export const SendIcon = () => (
  <svg {...stroked} width="16" height="16">
    <line x1="22" y1="2" x2="11" y2="13" />
    <polygon points="22 2 15 22 11 13 2 9 22 2" />
  </svg>
);

/** The avatar on every agent/system bubble. */
export const BotIcon = () => (
  <svg
    xmlns="http://www.w3.org/2000/svg"
    width="28"
    height="28"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    className="lucide lucide-bot"
  >
    <path d="M12 8V4H8" />
    <rect width="16" height="12" x="4" y="8" rx="2" />
    <path d="M2 14h2" />
    <path d="M20 14h2" />
    <path d="M15 13v2" />
    <path d="M9 13v2" />
  </svg>
);

/**
 * The user's counterpart to BotIcon, on the RIGHT of their own messages.
 *
 * The one icon here whose geometry is NOT copied from the Flask templates --
 * the original never rendered an avatar on user messages, so there was nothing
 * to copy. Drawn to match BotIcon: same 24x24 grid, same 28px box, same
 * `stroked` set, so the two columns align and both follow --primary-color.
 */
export const UserIcon = () => (
  <svg {...stroked} width="28" height="28">
    <circle cx="12" cy="12" r="10" />
    <circle cx="12" cy="10" r="3" />
    <path d="M6.17 18.849A4 4 0 0 1 10 16h4a4 4 0 0 1 3.83 2.849" />
  </svg>
);

/** Injected next to each entry in the manual agent list (AGENT_ICON_SVG). */
export const AgentIcon = () => (
  <svg {...stroked} width="16" height="16">
    <path d="M3 12a9 9 0 1 0 2.6-6.36" />
    <polyline points="3 4 3 12 11 12" />
  </svg>
);

/**
 * The Saathi capability's mark: a large four-pointed sparkle with a small one
 * trailing it. Stroked like the rest, so it follows the theme colour.
 */
export const SparkleIcon = () => (
  <svg {...stroked} width="16" height="16">
    <path d="M11 3 L13 9 L19 11 L13 13 L11 19 L9 13 L3 11 L9 9 Z" />
    <path d="M18.5 3.5 L19.4 5.6 L21.5 6.5 L19.4 7.4 L18.5 9.5 L17.6 7.4 L15.5 6.5 L17.6 5.6 Z" />
  </svg>
);

/** The separator between workflow breadcrumb chips. */
export const BreadcrumbSeparatorIcon = () => (
  <svg {...stroked} width="14" height="14">
    <polyline points="9 18 15 12 9 6" />
  </svg>
);

/** A simplified mortarboard -- the School row in the profile dialog. */
export const SchoolIcon = () => (
  <svg {...stroked} width="16" height="16">
    <path d="M12 3 3 8l9 5 9-5-9-5z" />
    <path d="M3 8v8l9 5 9-5V8" />
    <path d="M12 13v8" />
  </svg>
);

/** District and State rows in the profile dialog. */
export const MapPinIcon = () => (
  <svg {...stroked} width="16" height="16">
    <path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 1 1 16 0z" />
    <circle cx="12" cy="10" r="3" />
  </svg>
);

/** The role badge in the profile dialog's header. */
export const BriefcaseIcon = () => (
  <svg {...stroked} width="14" height="14">
    <rect x="2" y="7" width="20" height="14" rx="2" />
    <path d="M16 7V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v2" />
  </svg>
);

/* -------------------------------------------------------------------------
 * Voice
 * ---------------------------------------------------------------------- */

/** The composer's record button, at rest. */
export const MicIcon = () => (
  <svg {...stroked} width="18" height="18">
    <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
    <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
    <line x1="12" y1="19" x2="12" y2="23" />
    <line x1="8" y1="23" x2="16" y2="23" />
  </svg>
);

/**
 * Shown while recording. A filled square, not a crossed-out mic: the button
 * means "stop and use this", and a struck-through mic reads as "cancel".
 */
export const StopIcon = () => (
  <svg {...stroked} width="18" height="18">
    <rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" />
  </svg>
);

/** Read-this-out, on an agent bubble. */
export const SpeakerIcon = () => (
  <svg {...stroked} width="15" height="15">
    <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" />
    <path d="M15.54 8.46a5 5 0 0 1 0 7.07" />
    <path d="M19.07 4.93a10 10 0 0 1 0 14.14" />
  </svg>
);

/** The same speaker, muted, while a clip is playing -- click to stop. */
export const SpeakerOffIcon = () => (
  <svg {...stroked} width="15" height="15">
    <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" />
    <line x1="23" y1="9" x2="17" y2="15" />
    <line x1="17" y1="9" x2="23" y2="15" />
  </svg>
);

/** Indeterminate progress, for the transcribe and synthesise waits. */
export const SpinnerIcon = () => (
  <svg {...stroked} width="15" height="15" className="voice-spinner">
    <path d="M21 12a9 9 0 1 1-6.219-8.56" />
  </svg>
);

/** Logout, next to the logged-in user's name in the sidebar. */
export const LogoutIcon = () => (
  <svg {...stroked} width="14" height="14">
    <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
    <polyline points="16 17 21 12 16 7" />
    <line x1="21" y1="12" x2="9" y2="12" />
  </svg>
);
