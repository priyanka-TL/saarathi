import DOMPurify from 'dompurify';

/**
 * Wherever a server-supplied URL becomes a clickable href, the scheme is
 * re-checked here rather than trusting the response shape. The server already
 * allowlists the host (app/providers/transport/http.py::url_is_permitted); this
 * is defence in depth, ported verbatim.
 *
 * Used by the session report link and by a turn's downloadable documents --
 * the check was never report-specific, only its name was.
 *
 * Returns the safe URL, or null if it must not be rendered.
 */
export function safeHttpsUrl(url) {
  const sanitized = DOMPurify.sanitize(url || '');
  return /^https:\/\//i.test(sanitized) ? sanitized : null;
}
