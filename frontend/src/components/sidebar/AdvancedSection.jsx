import { useRef, useState } from 'react';

import { CAPABILITY_ACTIONS, SIDEBAR_HIDDEN_KEYS } from '../../constants';
import { cx } from '../../utils/cx';
import { AgentIcon, BrainIcon, ChevronDownIcon, GlobeIcon } from '../icons';

/**
 * The collapsible "ADVANCED" panel.
 *
 * COLLAPSE MECHANISM: max-height + opacity on .advanced-content -- NOT the
 * CSS-grid technique the workflow banner uses. Two different techniques on
 * purpose, with different easing. Also note the chevron rotation is INVERTED
 * relative to the banner's: this one is rotated 180deg when COLLAPSED
 * (`.advanced-toggle[aria-expanded="false"] .chevron`), so aria-expanded must
 * be a real attribute, not just state.
 */

/**
 * One capability action button.
 *
 * Holds its own re-entrancy latch, ported from the original's
 * `dataset.pending`. The global busy flag is NOT enough here: the handler
 * awaits resetConversation() before sending, and during that await the global
 * flag is still false, so a second click would get through.
 */
function CapabilityActionButton({ action, onActivate, isBusy }) {
  const pendingRef = useRef(false);

  async function handleClick(e) {
    // The parent card has its own display-only handler; without this it would
    // also fire and reset the conversation a second time.
    e.stopPropagation();
    if (isBusy() || pendingRef.current) return;
    pendingRef.current = true;
    try {
      await onActivate(action);
    } finally {
      pendingRef.current = false;
    }
  }

  return (
    <button
      type="button"
      className="capability-action-btn"
      data-agent-key={action.agentKey}
      data-agent-label={action.label}
      data-autostart={action.autostart}
      onClick={handleClick}
    >
      {action.label}
    </button>
  );
}

export default function AdvancedSection({
  agents,
  activeCard,
  activeAgentKey,
  onActivateCapability,
  onSelectDisplayCard,
  onSelectAgent,
  isBusy,
}) {
  const [collapsed, setCollapsed] = useState(true);

  // The manual list hides the three keys reachable another way. With the
  // current YAML agents that leaves it EMPTY -- correct, current behaviour.
  const listedAgents = agents.filter((a) => a.key && !SIDEBAR_HIDDEN_KEYS.has(a.key));

  return (
    <div className="advanced-section">
      <button
        type="button"
        className="advanced-toggle"
        id="advanced-toggle"
        aria-expanded={collapsed ? 'false' : 'true'}
        onClick={() => setCollapsed((c) => !c)}
      >
        <div className="advanced-toggle-left">
          <span className="advanced-title">ADVANCED</span>
          <div className="advanced-subtitle">Manual capability controls</div>
        </div>
        <ChevronDownIcon className="chevron" />
      </button>

      <div className={cx('advanced-content', collapsed && 'collapsed')} id="advanced-content">
        <div className="capabilities-label">CAPABILITIES</div>

        {/*
          The CARD carries no agent key -- deliberately. It used to, while
          containing BOTH action buttons, so any click a few pixels off a
          button (in the padding, title, description or badge) reset the
          conversation and pinned the wrong agent, with the banner then
          showing the raw routing key. The card is display-only; only the
          buttons route.
        */}
        <div
          className={cx('capability-card', activeCard === 'capability' && 'active')}
          onClick={() => onSelectDisplayCard('capability', 'Listening at Scale')}
        >
          <div className="capability-header">
            <BrainIcon />
            <span className="capability-title">Listening at Scale</span>
          </div>
          <div className="capability-desc">Synthesize field insights into actionable knowledge</div>
          <div className="capability-badge">SHIKSHALOKAM</div>
          <div className="capability-actions">
            {CAPABILITY_ACTIONS.map((action) => (
              <CapabilityActionButton
                key={action.agentKey}
                action={action}
                onActivate={onActivateCapability}
                isBusy={isBusy}
              />
            ))}
          </div>
        </div>

        {/* Display-only too: clicking sets the banner but never routes. */}
        <div
          className={cx('highlight-card', activeCard === 'highlight' && 'active')}
          onClick={() => onSelectDisplayCard('highlight', 'SG Commons Portal')}
        >
          <div className="highlight-header">
            <GlobeIcon />
            <span className="highlight-title">SG Commons Portal</span>
          </div>
          <div className="highlight-desc">AI search for ecosystem assets</div>
        </div>

        <ul id="agent-list" className="agent-list">
          {listedAgents.map((agent) => (
            <li
              key={agent.key}
              className={cx('agent-item', activeAgentKey === agent.key && 'active')}
              data-key={agent.key}
              onClick={() => onSelectAgent(agent)}
            >
              <div className="agent-item-title">
                <AgentIcon />
                <span className="agent-name">{agent.name}</span>
              </div>
              <div className="agent-desc">{agent.description}</div>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
