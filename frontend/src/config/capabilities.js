/**
 * The capability document's CONTRACT -- the vocabulary, not the content.
 *
 * There is deliberately NO bundled catalogue here. The capability cards in the
 * ADVANCED panel come from `GET /api/ui/capabilities` and from nowhere else:
 * the backend's app/config/ui/capabilities.yaml is the single source of truth,
 * so the sidebar cannot disagree with it and cannot be stale. If that request
 * fails, the panel renders no cards -- see src/hooks/useCapabilities.js.
 *
 * What this module holds instead is the two closed sets that are CODE, not
 * configuration: the statuses the renderer knows how to draw, and the actions
 * ChatPage knows how to run. Config selects from them; it can never add to
 * them.
 *
 * THE DOCUMENT SHAPE
 * ------------------
 *   { version, capabilities: [ Capability ] }
 *
 *   Capability
 *     id           stable key. Also the value `activeCard` holds, so it is
 *                  what decides which card wears `.active`.
 *     title        heading
 *     description  the line under it
 *     icon         key into components/icons/registry.js -- NOT a path. The
 *                  icons are inline SVG because they inherit currentColor.
 *     badge        the small outlined tag (e.g. 'SHIKSHALOKAM'), or null
 *     status       see STATUS below
 *     order        ascending; ties broken by declaration order
 *     visible      false removes it entirely
 *     action       what clicking the CARD BODY does -- see ACTION_TYPES
 *     agents       zero or more; zero renders no `.capability-actions` block,
 *                  one renders a single button, many render a column
 *
 *   Agent (inside a capability)
 *     id, label, status, order, visible, action
 *
 * Only `id` + `title` are required (`id` + `label` for an agent); everything
 * else is defaulted by src/config/capabilitySchema.js, which also drops
 * anything malformed rather than letting it reach a component.
 */

/**
 * Every action type ChatPage knows how to run.
 *
 *   start_agent   full reset, pin the agent, autostart. Needs `agentKey`;
 *                 `autostart` is optional.
 *   display_card  set the header banner only; never routes.
 *   coming_soon   show the sidebar toast. No navigation, no request.
 *   none          inert.
 */
export const ACTION_TYPES = Object.freeze({
  startAgent: 'start_agent',
  displayCard: 'display_card',
  comingSoon: 'coming_soon',
  none: 'none',
});

/** Every status a capability or an agent may carry. */
export const STATUS = Object.freeze({
  enabled: 'enabled',
  disabled: 'disabled',
  comingSoon: 'coming_soon',
});
