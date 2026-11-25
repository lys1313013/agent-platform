import { useEffect, useMemo } from 'react';
import { useChatStore } from '@/stores/chatStore';

/** Keep the active chat metadata separate from the server's filtered, ordered list. */
export function useSessionHistory(open: boolean, search: string, agentId?: string | null, source?: string) {
  const sessions = useChatStore(state => state.sessions);
  const ids = useChatStore(state => state.sessionListIds);
  const refresh = useChatStore(state => state.refreshSessions);
  const query = search.trim();

  useEffect(() => {
    if (!open) return;
    const timer = window.setTimeout(() => {
      void refresh(agentId, { search: query || undefined, source, pinned_first: true });
    }, query ? 200 : 0);
    return () => window.clearTimeout(timer);
  }, [open, query, agentId, source, refresh]);

  return useMemo(() => {
    const byId = new Map(sessions.map(item => [item.id, item]));
    return ids.flatMap(id => {
      const item = byId.get(id);
      if (!item || (agentId && item.agent_id !== agentId) || (source && item.source !== source)) return [];
      if (query && !(item.title ?? '').toLowerCase().includes(query.toLowerCase())) return [];
      return [item];
    });
  }, [ids, sessions, agentId, source, query]);
}
