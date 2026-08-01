import DOMPurify from 'dompurify';
import { marked } from 'marked';

/**
 * Markdown -> sanitized HTML, for AGENT messages only.
 *
 * `marked` and `dompurify` are pinned to the exact versions jsDelivr was
 * serving to the Flask app (15.0.12 / 3.4.12) so rendering is identical --
 * marked's defaults for heading ids, mangling and smart quotes have changed
 * across majors, and v13+ can even return a Promise from parse().
 *
 * Only agent content goes through this. User, system and context-switch
 * messages render as plain text, which React escapes by default -- the same
 * deliberate XSS boundary the original drew with textContent vs innerHTML.
 */
export function renderAgentHtml(content) {
  return DOMPurify.sanitize(marked.parse(content ?? ''));
}
