import { AgentIcon, BrainIcon, GlobeIcon, SparkleIcon } from './index.jsx';

/**
 * Name -> icon component, for config-driven rendering.
 *
 * Config names an icon by KEY, never by path. src/assets/ is empty on purpose:
 * every icon is inline SVG so `stroke="currentColor"` picks up
 * --text-primary / --primary-color from the cascade, which an
 * `<img src="*.svg">` cannot do (see components/icons/index.jsx). A config
 * value of 'brain' therefore selects a component; it does not resolve a URL.
 *
 * Adding an icon for a new capability is two lines: export it from index.jsx,
 * register it here.
 */
const ICONS = {
  brain: BrainIcon,
  globe: GlobeIcon,
  agent: AgentIcon,
  sparkle: SparkleIcon,
};

/**
 * Never returns undefined. An unknown or missing icon name falls back to
 * AgentIcon so a typo in a deployed config.js costs one wrong glyph, not a
 * crashed sidebar -- `<undefined />` would throw during render and take the
 * whole panel down.
 */
export function resolveIcon(name) {
  return ICONS[name] || AgentIcon;
}
