import { act, render, renderHook, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import * as uiApi from '../api/ui';

import CapabilityCard from '../components/sidebar/CapabilityCard.jsx';
import { normalizeCapabilities } from '../config/capabilitySchema';
import { useCapabilities } from '../hooks/useCapabilities';

/**
 * The capability catalogue arrives from ONE place -- GET /api/ui/capabilities,
 * backed by a YAML file an operator edits at deploy time. There is no bundled
 * fallback, so two things matter:
 *
 *   1. a malformed document drops the bad part instead of crashing the panel;
 *   2. one component renders many-agent, one-agent and no-agent capabilities.
 */

const never = () => false;

afterEach(() => {
  vi.restoreAllMocks();
});

describe('normalizeCapabilities', () => {
  it('orders by `order`, not by declaration', () => {
    const out = normalizeCapabilities({
      capabilities: [
        { id: 'c', title: 'C', order: 30 },
        { id: 'a', title: 'A', order: 10 },
        { id: 'b', title: 'B', order: 20 },
      ],
    });
    expect(out.map((c) => c.id)).toEqual(['a', 'b', 'c']);
  });

  it('keeps declaration order when `order` ties or is absent', () => {
    const out = normalizeCapabilities({
      capabilities: [
        { id: 'first', title: 'First' },
        { id: 'second', title: 'Second' },
        { id: 'third', title: 'Third' },
      ],
    });
    expect(out.map((c) => c.id)).toEqual(['first', 'second', 'third']);
  });

  it('drops entries marked visible:false, at both levels', () => {
    const out = normalizeCapabilities({
      capabilities: [
        { id: 'shown', title: 'Shown' },
        { id: 'hidden', title: 'Hidden', visible: false },
        {
          id: 'partly',
          title: 'Partly',
          agents: [
            { id: 'in', label: 'In' },
            { id: 'out', label: 'Out', visible: false },
          ],
        },
      ],
    });
    expect(out.map((c) => c.id)).toEqual(['shown', 'partly']);
    expect(out[1].agents.map((a) => a.id)).toEqual(['in']);
  });

  it('drops entries missing an id or a title, keeping their siblings', () => {
    const out = normalizeCapabilities({
      capabilities: [
        { title: 'No id' },
        { id: 'no_title' },
        { id: 'ok', title: 'OK' },
        null,
        'not an object',
      ],
    });
    expect(out.map((c) => c.id)).toEqual(['ok']);
  });

  it('coerces an unknown status to enabled rather than hiding the entry', () => {
    const [cap] = normalizeCapabilities({
      capabilities: [{ id: 'x', title: 'X', status: 'retired' }],
    });
    expect(cap.status).toBe('enabled');
  });

  it('neutralises an unknown action type, and start_agent with no agentKey', () => {
    const out = normalizeCapabilities({
      capabilities: [
        { id: 'a', title: 'A', action: { type: 'launch_missiles' } },
        { id: 'b', title: 'B', action: { type: 'start_agent' } },
      ],
    });
    expect(out.map((c) => c.action.type)).toEqual(['none', 'none']);
  });

  it('returns null for a document that is not a capability document', () => {
    // null is the signal to fall through to the next config layer.
    expect(normalizeCapabilities(undefined)).toBeNull();
    expect(normalizeCapabilities(null)).toBeNull();
    expect(normalizeCapabilities('nope')).toBeNull();
    expect(normalizeCapabilities({ capabilities: 'nope' })).toBeNull();
  });

  it('distinguishes "show none" from "unusable"', () => {
    // Both render an empty panel, but only the first is the server's
    // intention -- keep the two answers distinct so a future caller can tell.
    expect(normalizeCapabilities({ capabilities: [] })).toEqual([]);
    expect(normalizeCapabilities({ capabilities: 'nope' })).toBeNull();
  });

  it('accepts a bare array as well as a wrapped document', () => {
    const out = normalizeCapabilities([{ id: 'a', title: 'A' }]);
    expect(out.map((c) => c.id)).toEqual(['a']);
  });
});

describe('useCapabilities is API-only', () => {
  // The panel has no bundled catalogue: with no backend there are no cards.
  // This is the behaviour to protect -- reintroducing a local default would
  // draw buttons whose POST /api/reset is going to fail anyway.
  it('starts empty, before any request resolves', () => {
    const { result } = renderHook(() => useCapabilities());
    expect(result.current.capabilities).toEqual([]);
  });

  it('renders the document the API returns', async () => {
    vi.spyOn(uiApi, 'getCapabilities').mockResolvedValue({
      ok: true,
      status: 200,
      data: { capabilities: [{ id: 'from_api', title: 'From API' }] },
    });

    const { result } = renderHook(() => useCapabilities());
    await act(() => result.current.loadCapabilities());

    expect(result.current.capabilities.map((c) => c.id)).toEqual(['from_api']);
  });

  it.each([
    ['a 404', () => vi.spyOn(uiApi, 'getCapabilities').mockResolvedValue({ ok: false, status: 404 })],
    ['a dead backend', () => vi.spyOn(uiApi, 'getCapabilities').mockRejectedValue(new Error('ECONNREFUSED'))],
    [
      'a nonsense body',
      () =>
        vi
          .spyOn(uiApi, 'getCapabilities')
          .mockResolvedValue({ ok: true, status: 200, data: 'not a document' }),
    ],
  ])('stays empty on %s', async (_label, arrange) => {
    arrange();

    const { result } = renderHook(() => useCapabilities());
    await act(() => result.current.loadCapabilities());

    expect(result.current.capabilities).toEqual([]);
  });
});

describe('CapabilityCard renders any number of agents', () => {
  const capability = (agents) => ({
    id: 'cap',
    title: 'Cap',
    description: 'A capability',
    icon: 'brain',
    badge: null,
    status: 'enabled',
    order: 10,
    action: { type: 'display_card' },
    agents: agents.map((label, i) => ({
      id: `a${i}`,
      label,
      status: 'enabled',
      order: i,
      action: { type: 'start_agent', agentKey: `k${i}` },
      meta: {},
    })),
    meta: {},
  });

  it('renders one button per agent when there are several', () => {
    render(
      <CapabilityCard
        capability={capability(['One', 'Two', 'Three'])}
        isActive={false}
        onRun={vi.fn()}
        isBusy={never}
      />,
    );
    expect(screen.getAllByRole('button')).toHaveLength(3);
  });

  it('renders a single-agent capability with no special casing', () => {
    render(
      <CapabilityCard
        capability={capability(['Only'])}
        isActive={false}
        onRun={vi.fn()}
        isBusy={never}
      />,
    );
    expect(screen.getAllByRole('button')).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'Only' })).toBeInTheDocument();
  });

  it('omits the actions container entirely when a capability has no agents', () => {
    const { container } = render(
      <CapabilityCard
        capability={capability([])}
        isActive={false}
        onRun={vi.fn()}
        isBusy={never}
      />,
    );
    expect(screen.queryAllByRole('button')).toHaveLength(0);
    // An empty .capability-actions would still contribute its own margin plus
    // the card's 10px gap, so its absence is the assertion, not its emptiness.
    expect(container.querySelector('.capability-actions')).toBeNull();
  });
});

describe('a coming-soon capability', () => {
  const comingSoon = {
    id: 'sg_commons',
    title: 'SG Commons Portal',
    description: 'AI search for ecosystem assets',
    icon: 'globe',
    badge: null,
    status: 'coming_soon',
    order: 20,
    action: { type: 'coming_soon', message: 'SG Commons Portal is coming soon.' },
    agents: [],
    meta: {},
  };

  it('shows a pill and runs its coming_soon action on click', async () => {
    const onRun = vi.fn();
    render(
      <CapabilityCard capability={comingSoon} isActive={false} onRun={onRun} isBusy={never} />,
    );

    expect(screen.getByText('Coming soon')).toBeInTheDocument();

    await userEvent.click(screen.getByText('SG Commons Portal'));
    expect(onRun).toHaveBeenCalledTimes(1);
    // The action type is what decides the behaviour, and it must be the toast
    // one -- NOT display_card, which would set the header banner and so count
    // as navigating somewhere.
    expect(onRun.mock.calls[0][0].type).toBe('coming_soon');
  });
});

describe('a disabled capability', () => {
  const disabled = {
    id: 'off',
    title: 'Switched Off',
    description: '',
    icon: 'brain',
    badge: null,
    status: 'disabled',
    order: 10,
    action: { type: 'display_card' },
    agents: [
      {
        id: 'a',
        label: 'A',
        status: 'enabled',
        order: 0,
        action: { type: 'start_agent', agentKey: 'k' },
        meta: {},
      },
    ],
    meta: {},
  };

  it('runs nothing on click and hides its agents', async () => {
    const onRun = vi.fn();
    render(<CapabilityCard capability={disabled} isActive={false} onRun={onRun} isBusy={never} />);

    await userEvent.click(screen.getByText('Switched Off'));
    expect(onRun).not.toHaveBeenCalled();
    // Entering an agent individually would contradict the card.
    expect(screen.queryByRole('button', { name: 'A' })).toBeNull();
  });
});
