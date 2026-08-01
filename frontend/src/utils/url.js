import DOMPurify from 'dompurify';

/**
 * The report link is the one place a server-supplied URL becomes a clickable
 * href, so the scheme is re-checked here rather than trusting the response
 * shape. The server already allowlists the host
 * (MitraRestClient._validate_url); this is defence in depth, ported verbatim.
 *
 * Returns the safe URL, or null if it must not be rendered.
 */
export function safeReportUrl(url) {
  const sanitized = DOMPurify.sanitize(url || '');
  return /^https:\/\//i.test(sanitized) ? sanitized : null;
}
