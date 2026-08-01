/**
 * Validation and normalisation for the capability document.
 *
 * The API response goes through `normalizeCapabilities` before it reaches a
 * component, so the renderer only ever sees entries that are complete, sorted
 * and visible.
 *
 * NOTHING HERE THROWS. That is the whole point: this config arrives from a
 * YAML file an operator edits at deploy time, i.e. from a place that can be
 * wrong, and one bad entry must not take down the panel. Two failure levels:
 *
 *   * the document as a whole is unusable  -> return null, and the caller
 *     renders nothing;
 *   * a single entry is unusable           -> drop that entry, keep the rest.
 *
 * Terse config is intended. Only `id` and `title` (`label` for an agent) are
 * required; everything else has a default, including `order`, which falls back
 * to declaration position so an unordered list still renders in written order.
 */

import { ACTION_TYPES, STATUS } from './capabilities';

const KNOWN_STATUSES = new Set(Object.values(STATUS));
const KNOWN_ACTION_TYPES = new Set(Object.values(ACTION_TYPES));

/** Position multiplier for entries that declare no `order`. */
const IMPLICIT_ORDER_STEP = 10;

function isPlainObject(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function nonEmptyString(value) {
  return typeof value === 'string' && value.trim() !== '' ? value.trim() : null;
}

/** Unknown statuses degrade to 'enabled' rather than hiding the entry. */
function normalizeStatus(raw) {
  return KNOWN_STATUSES.has(raw) ? raw : STATUS.enabled;
}

/**
 * An unrecognised action type becomes 'none' (inert) rather than being run.
 * A missing action object is also inert -- that is how a purely decorative
 * capability is expressed.
 */
function normalizeAction(raw) {
  if (!isPlainObject(raw)) return { type: ACTION_TYPES.none };
  const type = KNOWN_ACTION_TYPES.has(raw.type) ? raw.type : ACTION_TYPES.none;
  // start_agent without an agentKey cannot route; it would reset the
  // conversation and pin `undefined`. Neutralise it here, not at click time.
  if (type === ACTION_TYPES.startAgent && !nonEmptyString(raw.agentKey)) {
    return { type: ACTION_TYPES.none };
  }
  return { ...raw, type };
}

/**
 * Sort by `order`, then by declaration index so equal orders keep the order
 * they were written in. Array.prototype.sort is stable in every engine this
 * app targets, but the explicit tie-break makes the contract independent of
 * that -- and of whether the caller pre-filtered.
 */
function byOrderThenIndex(a, b) {
  return a.order - b.order || a._index - b._index;
}

function normalizeAgent(raw, index) {
  if (!isPlainObject(raw)) return null;

  const id = nonEmptyString(raw.id);
  const label = nonEmptyString(raw.label);
  if (!id || !label) return null;
  if (raw.visible === false) return null;

  return {
    _index: index,
    id,
    label,
    status: normalizeStatus(raw.status),
    order: Number.isFinite(raw.order) ? raw.order : index * IMPLICIT_ORDER_STEP,
    action: normalizeAction(raw.action),
    // Anything the config carries that this version does not know about
    // survives here, so extra metadata can be added ahead of the UI that
    // consumes it.
    meta: isPlainObject(raw.meta) ? raw.meta : {},
  };
}

function normalizeCapability(raw, index) {
  if (!isPlainObject(raw)) return null;

  const id = nonEmptyString(raw.id);
  const title = nonEmptyString(raw.title);
  if (!id || !title) return null;
  if (raw.visible === false) return null;

  const agents = (Array.isArray(raw.agents) ? raw.agents : [])
    .map(normalizeAgent)
    .filter(Boolean)
    .sort(byOrderThenIndex);

  return {
    _index: index,
    id,
    title,
    description: nonEmptyString(raw.description) || '',
    icon: nonEmptyString(raw.icon) || '',
    badge: nonEmptyString(raw.badge),
    status: normalizeStatus(raw.status),
    order: Number.isFinite(raw.order) ? raw.order : index * IMPLICIT_ORDER_STEP,
    action: normalizeAction(raw.action),
    agents,
    meta: isPlainObject(raw.meta) ? raw.meta : {},
  };
}

/**
 * @param  {unknown} raw  the capability document, as the API returned it.
 * @return {Array|null}   the ordered, visible capabilities, or null when the
 *                        document itself is unusable.
 *
 * Note the deliberate asymmetry: a document whose every entry is invalid
 * yields `[]`, NOT null. Both render an empty panel, but only `[]` is the
 * server saying so on purpose -- worth keeping distinguishable.
 */
export function normalizeCapabilities(raw) {
  // A bare array is accepted too -- it is the natural thing to write in
  // config.js, and matches how GET /api/agents answers.
  const list = Array.isArray(raw)
    ? raw
    : isPlainObject(raw) && Array.isArray(raw.capabilities)
      ? raw.capabilities
      : null;

  if (list === null) return null;

  return list.map(normalizeCapability).filter(Boolean).sort(byOrderThenIndex);
}

/** True when clicking the entry should do nothing at all. */
export function isInert(entry) {
  return entry.status === STATUS.disabled || entry.action.type === ACTION_TYPES.none;
}
