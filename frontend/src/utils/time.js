/**
 * Timestamp formatting, ported verbatim from main.js.
 *
 * The exact wording and pluralisation matter -- these strings appear in the
 * sidebar and under every message bubble.
 */

/** 24-hour "14:05". hour12:false is explicit, so it does not follow locale. */
export function formatTime(date) {
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
}

/** "Just now" / "5 minutes ago" / "2 hours ago" / "3 days ago" / a locale date. */
export function formatRelativeTime(date) {
  const seconds = Math.floor((Date.now() - date.getTime()) / 1000);
  if (seconds < 60) return 'Just now';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? '' : 's'} ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? '' : 's'} ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days} day${days === 1 ? '' : 's'} ago`;
  return date.toLocaleDateString();
}
