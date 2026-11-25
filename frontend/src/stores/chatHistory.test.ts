import { beforeEach, expect, it, vi } from 'vitest';
import { sessionsApi } from '@/lib/api';
import { invalidateAuthSession } from '@/lib/authSession';
import type { Message, Session, SessionDetail } from '@/lib/types';
import { useChatStore } from './chatStore';

vi.mock('@/lib/api', () => ({ sessionsApi: { list: vi.fn(), get: vi.fn(), pin: vi.fn(), archive: vi.fn() }, workspacesApi: {} }));

function session(id: string): Session {
  return { id, title: id, agent_id: null, workspace_id: null, source: 'chat', is_pinned: false,
    is_archived: false, created_at: '', updated_at: '' };
}
function history(ids: string[], more: boolean): SessionDetail {
  const messages: Message[] = ids.map(id => ({ id, role: 'user', content: id, created_at: '' }));
  return { ...session('active'), messages, has_more_messages: more };
}
beforeEach(() => {
  vi.resetAllMocks();
  invalidateAuthSession();
});

it('keeps a filtered list independent of the open session and discards superseded requests', async () => {
  const store = useChatStore.getState();
  useChatStore.setState({ activeSessionId: 'active', sessions: [session('active')] });
  let finish!: (sessions: Session[]) => void;
  vi.mocked(sessionsApi.list).mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
  const stale = store.refreshSessions(null, { search: 'stale' });
  const page = Array.from({ length: 50 }, (_, i) => session('search-' + i));
  vi.mocked(sessionsApi.list).mockResolvedValueOnce(page);
  await store.refreshSessions(null, { search: 'search', source: 'chat', pinned_first: true });
  finish([session('stale')]);
  await stale;
  vi.mocked(sessionsApi.list).mockResolvedValueOnce([session('search-50')]);
  await store.loadMoreSessions();
  expect(sessionsApi.list).toHaveBeenLastCalledWith(null, { search: 'search', source: 'chat', pinned_first: true, offset: 50, limit: 50 });
  const state = useChatStore.getState();
  expect(state.sessionListIds).toHaveLength(51);
  expect(state.sessionListIds).not.toContain('active');
  expect(state.sessions.some(item => item.id === 'active')).toBe(true);
  expect(state.sessionListIds).not.toContain('stale');
  expect(state.hasMoreSessions).toBe(false);
});

it('prepends older pages once and retains them when refreshing the latest page', async () => {
  const store = useChatStore.getState();
  vi.mocked(sessionsApi.get)
    .mockResolvedValueOnce(history(['3', '4'], true))
    .mockResolvedValueOnce(history(['1', '2'], false))
    .mockResolvedValueOnce(history(['4', '5'], true));
  await store.loadSessionMessages('active');
  await Promise.all([store.loadOlderMessages('active'), store.loadOlderMessages('active')]);
  expect(sessionsApi.get).toHaveBeenNthCalledWith(2, 'active', '3');
  await store.loadSessionMessages('active');
  expect(useChatStore.getState().messages.active.map(m => m.id)).toEqual(['1', '2', '3', '4', '5']);
  expect(useChatStore.getState().hasMoreMessages.active).toBe(false);
});

it('updates pin and archive from server responses and removes a completed delete locally', async () => {
  const store = useChatStore.getState();
  const item = session('active');
  useChatStore.setState({ sessions: [item], sessionListIds: ['active'], activeSessionId: 'active' });
  vi.mocked(sessionsApi.pin).mockResolvedValue({ ...item, title: 'server saved' });
  vi.mocked(sessionsApi.archive).mockResolvedValue(item);
  await store.pinSession('active', true);
  expect(useChatStore.getState().sessions[0]).toMatchObject({ title: 'server saved', is_pinned: false });
  await store.archiveSession('active', true);
  expect(useChatStore.getState().sessions[0].is_archived).toBe(false);
  store.removeSessionLocal('active');
  expect(useChatStore.getState().activeSessionId).toBeNull();
  expect(useChatStore.getState().sessionListIds).toEqual([]);
});
