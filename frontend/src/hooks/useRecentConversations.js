import { useCallback, useState } from 'react';

import { listRecent } from '../api/conversations';

const MAX_ROWS = 20;

/**
 * The sidebar's recent-conversations list.
 *
 * Fetched ONCE at boot, then patched locally after every turn -- it is
 * deliberately never refetched. `upsert` moves the touched conversation to the
 * top and trims to 20 rows, which is what the original's
 * `_upsertConversationRow` did with DOM nodes.
 *
 * Relative timestamps ("5 minutes ago") are derived at render from the stored
 * `last_message_at` rather than being stamped into the row, so they refresh for
 * free on any re-render -- the original needed a separate sweep for that.
 */
export function useRecentConversations() {
  const [conversations, setConversations] = useState([]);

  const loadRecent = useCallback(async () => {
    try {
      const { data } = await listRecent(MAX_ROWS);
      setConversations(data?.conversations ?? []);
    } catch {
      setConversations([]);
    }
  }, []);

  const upsert = useCallback(({ id, title, last_message_at: lastMessageAt }) => {
    if (!id) return;
    setConversations((prev) => {
      const existing = prev.find((c) => c.id === id);
      const merged = {
        id,
        // `||` keeps the existing title when this turn carries none -- an
        // autostart turn deliberately does not title the conversation.
        title: title || existing?.title || null,
        last_message_at: lastMessageAt || existing?.last_message_at || null,
        message_count: (existing?.message_count ?? 0) + 1,
      };
      return [merged, ...prev.filter((c) => c.id !== id)].slice(0, MAX_ROWS);
    });
  }, []);

  return { conversations, loadRecent, upsert };
}
