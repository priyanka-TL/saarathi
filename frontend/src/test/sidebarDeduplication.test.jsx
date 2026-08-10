import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import AdvancedSection from '../components/sidebar/AdvancedSection.jsx';
import CapabilityCard from '../components/sidebar/CapabilityCard.jsx';
import { hiddenAgentKeys, SIDEBAR_HIDDEN_KEYS } from '../constants';

/**
 * ONE control per agent in the rail.
 *
 * Seeding the Saathi agent put "Saathi" in the sidebar THREE times: the card
 * title, its nested button, and an entry in the manual agent list. Nothing
 * failed -- the duplicates simply appeared, and the manual list (which shares
 * the `agent-list` class with the recents list) read as a second chat history.
 *
 * Two rules prevent it, and these are the tests for them:
 *   - a card that launches an agent ITSELF renders no nested buttons;
 *   - the manual list hides every key reachable from a card, DERIVED from the
 *     capability document rather than from a hardcoded list.
 */

const never = () => false;

const selfLaunching = {
  id: 'saathi_assistant',
  title: 'Saathi',
  description: 'Ask an education question.',
  icon: 'school',
  badge: null,
  status: 'enabled',
  order: 30,
  visible: true,
  // The card is the control. The backend suppresses the nested agents for
  // exactly this shape.
  action: { type: 'start_agent', agentKey: 'saathi' },
  agents: [],
};

const grouping = {
  id: 'listening_at_scale',
  title: 'Listening at Scale',
  description: 'Synthesize field insights.',
  icon: 'brain',
  badge: 'SHIKSHALOKAM',
  status: 'enabled',
  order: 10,
  visible: true,
  action: { type: 'display_card' },
  agents: [
    {
      id: 'record_stories', label: 'Record Stories', description: '', icon: '',
      status: 'enabled', order: 10, visible: true,
      action: { type: 'start_agent', agentKey: 'record_stories' },
    },
    {
      id: 'capture_discussion', label: 'Capture Discussions', description: '', icon: '',
      status: 'enabled', order: 20, visible: true,
      action: { type: 'start_agent', agentKey: 'capture_discussion' },
    },
  ],
};

function advanced(props = {}) {
  return (
    <AdvancedSection
      capabilities={[grouping, selfLaunching]}
      agents={[]}
      activeCard={null}
      activeAgentKey={null}
      onRunAction={() => {}}
      onSelectAgent={() => {}}
      isBusy={never}
      {...props}
    />
  );
}

describe('a card that launches its own agent', () => {
  it('renders no nested button -- the card IS the control', () => {
    const { container } = render(
      <CapabilityCard capability={selfLaunching} isActive={false} onRun={() => {}} isBusy={never} />,
    );

    expect(container.querySelector('.capability-actions')).toBeNull();
    expect(screen.queryAllByRole('button')).toHaveLength(0);
    // The title still shows exactly once.
    expect(screen.getAllByText('Saathi')).toHaveLength(1);
  });

  it('starts the agent when the card body is clicked', async () => {
    const onRun = vi.fn();
    render(
      <CapabilityCard capability={selfLaunching} isActive={false} onRun={onRun} isBusy={never} />,
    );

    await userEvent.click(screen.getByText('Saathi'));

    expect(onRun).toHaveBeenCalledTimes(1);
    expect(onRun.mock.calls[0][0]).toEqual({ type: 'start_agent', agentKey: 'saathi' });
  });

  it('leaves a grouping card\'s buttons alone', () => {
    const { container } = render(
      <CapabilityCard capability={grouping} isActive={false} onRun={() => {}} isBusy={never} />,
    );

    expect(container.querySelectorAll('.capability-action-btn')).toHaveLength(2);
  });
});

describe('hiddenAgentKeys', () => {
  it('derives a self-launching card\'s key from the CARD action', () => {
    // The case that broke: a card with no nested agents, so nothing to read
    // an agentKey off except the card itself.
    expect(hiddenAgentKeys([selfLaunching]).has('saathi')).toBe(true);
  });

  it('derives a grouping card\'s keys from its NESTED agents', () => {
    const keys = hiddenAgentKeys([grouping]);

    expect(keys.has('record_stories')).toBe(true);
    expect(keys.has('capture_discussion')).toBe(true);
  });

  it('always keeps the router default, which no card offers', () => {
    expect(hiddenAgentKeys([]).has('general_support')).toBe(true);
    expect(SIDEBAR_HIDDEN_KEYS.has('general_support')).toBe(true);
  });

  it('does not mutate the shared constant', () => {
    const before = SIDEBAR_HIDDEN_KEYS.size;
    hiddenAgentKeys([selfLaunching, grouping]);

    expect(SIDEBAR_HIDDEN_KEYS.size).toBe(before);
    expect(SIDEBAR_HIDDEN_KEYS.has('saathi')).toBe(false);
  });

  it('tolerates a missing or malformed document', () => {
    expect(hiddenAgentKeys(undefined).has('general_support')).toBe(true);
    expect(hiddenAgentKeys([{}, { agents: null }, null]).has('general_support')).toBe(true);
  });
});

describe('the manual agent list', () => {
  it('omits an agent already reachable from a card', () => {
    const { container } = render(advanced({
      agents: [
        { key: 'saathi', name: 'Saathi', description: 'x' },
        { key: 'record_stories', name: 'Record Stories', description: 'x' },
      ],
    }));

    expect(container.querySelectorAll('#agent-list .agent-item')).toHaveLength(0);
  });

  it('still lists an agent no card offers', () => {
    const { container } = render(advanced({
      agents: [{ key: 'standalone', name: 'Standalone', description: 'x' }],
    }));

    const items = container.querySelectorAll('#agent-list .agent-item');
    expect(items).toHaveLength(1);
    expect(items[0].dataset.key).toBe('standalone');
  });

  it('never lists the router default', () => {
    const { container } = render(advanced({
      agents: [{ key: 'general_support', name: 'General Support', description: 'x' }],
    }));

    expect(container.querySelectorAll('#agent-list .agent-item')).toHaveLength(0);
  });
});
