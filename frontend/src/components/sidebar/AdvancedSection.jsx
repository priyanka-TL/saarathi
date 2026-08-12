import { useState } from 'react';

import { hiddenAgentKeys } from '../../constants';
import { cx } from '../../utils/cx';
import { AgentIcon, ChevronDownIcon } from '../icons';
import CapabilityCard from './CapabilityCard';

/**
 * The collapsible "ADVANCED" panel.
 *
 * COLLAPSE MECHANISM: max-height + opacity on .advanced-content -- NOT the
 * CSS-grid technique the workflow banner uses. Two different techniques on
 * purpose, with different easing. Also note the chevron rotation is INVERTED
 * relative to the banner's: this one is rotated 180deg when COLLAPSED
 * (`.advanced-toggle[aria-expanded="false"] .chevron`), so aria-expanded must
 * be a real attribute, not just state.
 *
 * TWO LISTS, TWO SOURCES. The capability cards come from the capability
 * document (src/config/capabilities.js and the layers above it); the manual
 * list underneath comes from GET /api/agents and always did. They are
 * separate catalogues and are not merged -- an agent reachable from a card is
 * deliberately hidden from the manual list, which is what hiddenAgentKeys does.
 */
export default function AdvancedSection({
  capabilities,
  agents,
  activeCard,
  activeAgentKey,
  onRunAction,
  onSelectAgent,
  isBusy,
}) {
  const [collapsed, setCollapsed] = useState(true);

  // Hide anything already reachable from a card, DERIVED from the capability
  // document rather than from a hardcoded key list -- see hiddenAgentKeys.
  // With the current catalogue that leaves this EMPTY, which is correct.
  const hidden = hiddenAgentKeys(capabilities);
  const listedAgents = agents.filter((a) => a.key && !hidden.has(a.key));

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

        {capabilities.map((capability) => (
          <CapabilityCard
            key={capability.id}
            capability={capability}
            // activeCard holds a capability id, so a card only ever compares
            // the value against its own -- adding capabilities cannot make two
            // of them light up.
            isActive={activeCard === capability.id}
            onRun={onRunAction}
            isBusy={isBusy}
          />
        ))}

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
