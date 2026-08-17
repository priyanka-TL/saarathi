import { useRef } from 'react';

import { STATUS } from '../../config/capabilities';
import { isInert } from '../../config/capabilitySchema';
import { cx } from '../../utils/cx';
import { resolveIcon } from '../icons/registry';

/**
 * One capability, rendered from configuration.
 *
 * Replaces the two literal cards that used to live in AdvancedSection. The
 * markup for an enabled capability with agents is UNCHANGED from that version
 * -- same class names, same nesting, same data-* attributes -- because
 * style.css is a byte-for-byte copy of the Flask stylesheet and cannot be
 * adjusted to suit a new structure.
 *
 * ONE COMPONENT COVERS ALL THREE SHAPES the brief asks for:
 *   many agents  -> a column of buttons (Listening at Scale)
 *   one agent    -> the same column with one button; nothing special-cases it
 *   no agents    -> `.capability-actions` is OMITTED, not left empty, so the
 *                   card keeps its natural height (SG Commons Portal)
 */

/** Statuses that mean "visible, but not enterable". */
function isBlocked(entry) {
  return entry.status === STATUS.disabled || entry.status === STATUS.comingSoon;
}

/**
 * One agent button.
 *
 * Holds its own re-entrancy latch, ported from the original's
 * `dataset.pending`. The global busy flag is NOT enough here: the handler
 * awaits resetConversation() before sending, and during that await the global
 * flag is still false, so a second click would get through.
 */
function AgentActionButton({ capabilityId, agent, onRun, isBusy }) {
  const pendingRef = useRef(false);
  const blocked = isBlocked(agent) || isInert(agent);

  async function handleClick(e) {
    // A card that renders these buttons no longer carries a handler of its own,
    // so this is belt-and-braces rather than load-bearing. Kept deliberately:
    // without it the button's correctness would depend on the parent's gating
    // staying right, and a stray bubble would reset the conversation twice.
    e.stopPropagation();
    if (blocked) {
      // A blocked agent still reports why -- it just never routes.
      onRun(agent.action, { capabilityId, label: agent.label });
      return;
    }
    if (isBusy() || pendingRef.current) return;
    pendingRef.current = true;
    try {
      await onRun(agent.action, { capabilityId, label: agent.label });
    } finally {
      pendingRef.current = false;
    }
  }

  return (
    <button
      type="button"
      className={cx('capability-action-btn', blocked && 'is-blocked')}
      // Preserved from the hand-written version: these carried the routing
      // data in the Flask DOM and are the handle any external script or test
      // uses to find a button.
      data-agent-key={agent.action.agentKey}
      data-agent-label={agent.label}
      data-autostart={agent.action.autostart}
      data-status={agent.status}
      aria-disabled={blocked || undefined}
      onClick={handleClick}
    >
      {agent.label}
      {agent.status === STATUS.comingSoon && (
        <span className="capability-status-pill">Coming soon</span>
      )}
    </button>
  );
}

export default function CapabilityCard({ capability, isActive, onRun, isBusy }) {
  const Icon = resolveIcon(capability.icon);
  const blocked = isBlocked(capability);
  const inert = isInert(capability);

  // A card that renders agent buttons is a GROUPING HEADER, not a control: the
  // buttons inside it are what you click, and a handler on the body as well
  // gives two meanings to one target (Listening at Scale).
  //
  // A card with no buttons is itself the only target and must stay live -- a
  // self-launching `start_agent` card, whose nested agents the server
  // suppresses precisely because the card IS the control (Saathi), a
  // coming_soon card whose click raises the toast, or a display_card a tenant
  // has added before wiring any agents to it.
  const hasAgentButtons = !blocked && capability.agents.length > 0;
  const cardIsInteractive = !inert && !hasAgentButtons;

  return (
    <div
      className={cx(
        'capability-card',
        isActive && 'active',
        capability.status === STATUS.disabled && 'is-disabled',
        !cardIsInteractive && 'is-static',
      )}
      data-capability-id={capability.id}
      data-status={capability.status}
      aria-disabled={capability.status === STATUS.disabled || undefined}
      // Omitted rather than early-returning, so a non-interactive card carries
      // no listener at all.
      onClick={
        cardIsInteractive
          ? () => onRun(capability.action, { capabilityId: capability.id, label: capability.title })
          : undefined
      }
    >
      <div className="capability-header">
        <Icon />
        <span className="capability-title">{capability.title}</span>
        {capability.status === STATUS.comingSoon && (
          <span className="capability-status-pill">Coming soon</span>
        )}
      </div>

      {capability.description && <div className="capability-desc">{capability.description}</div>}

      {capability.badge && <div className="capability-badge">{capability.badge}</div>}

      {/*
        Omitted entirely when the capability has no agents -- an empty
        `.capability-actions` would still contribute its 4px top margin and
        the parent's 10px gap.

        A blocked CAPABILITY hides its agents too: entering them individually
        would contradict the card saying the whole thing is unavailable.
      */}
      {hasAgentButtons && (
        <div className="capability-actions">
          {capability.agents.map((agent) => (
            <AgentActionButton
              key={agent.id}
              capabilityId={capability.id}
              agent={agent}
              onRun={onRun}
              isBusy={isBusy}
            />
          ))}
        </div>
      )}
    </div>
  );
}
