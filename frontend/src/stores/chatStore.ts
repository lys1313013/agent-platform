import { create } from 'zustand';
import { assertAuthGeneration, getAuthGeneration, isCurrentAuthGeneration, onAuthSessionChange } from '@/lib/authSession';
import { sessionsApi, workspacesApi } from '@/lib/api';
import type { Session, Message } from '@/lib/types';
import type { Workspace } from '@/lib/api';

const SESSION_PAGE_SIZE = 50;
type SessionFilter = { search?: string; source?: string; pinned_first?: boolean };
let listRequest = 0;
let messageRequest = 0;
const latestMessageRequests = new Map<string, number>();

function matchesList(session: Session, agentId: string | null, filter: SessionFilter) {
  return (!agentId || agentId === session.agent_id)
    && (!filter.source || filter.source === session.source)
    && (!filter.search || (session.title ?? '').toLowerCase().includes(filter.search.toLowerCase()));
}

function mergeById<T extends { id: string }>(first: T[], next: T[]): T[] {
  return Array.from(new Map([...first, ...next].map(item => [item.id, item])).values());
}

interface ChatState {
  sessions: Session[];
  activeSessionId: string | null;
  messages: Record<string, Message[]>;
  isSessionsLoading: boolean;
  hasMoreSessions: boolean;
  sessionListError: boolean;
  sessionListOffset: number;
  sessionListAgent: string | null;
  sessionFilter: SessionFilter;
  sessionListIds: string[];
  hasMoreMessages: Record<string, boolean>;
  loadingOlderMessages: Record<string, boolean>;
  /** 当前会话消息异步加载中（点击历史项切换会话时，消息未就绪前显示 loading） */
  messagesLoading: boolean;

  // Workspace selection
  workspaces: Workspace[];
  selectedWorkspaceId: string | null;
  isWorkspacesLoading: boolean;

  // Actions
  loadSessions: (agentId?: string | null, filter?: SessionFilter) => Promise<void>;
  refreshSessions: (agentId?: string | null, filter?: SessionFilter) => Promise<void>;
  loadMoreSessions: () => Promise<void>;
  loadOlderMessages: (id: string) => Promise<void>;
  removeSessionLocal: (id: string) => void;
  createSession: (title?: string, agentId?: string | null) => Promise<string>;
  deleteSession: (id: string) => Promise<void>;
  setActiveSession: (id: string | null) => void;
  loadSessionMessages: (id: string) => Promise<void>;
  addMessage: (sessionId: string, msg: Message) => void;
  renameSession: (id: string, title: string) => Promise<void>;
  setSessionTitleLocal: (id: string, title: string) => void;
  pinSession: (id: string, isPinned: boolean) => Promise<void>;
  archiveSession: (id: string, isArchived: boolean) => Promise<void>;

  // Workspace actions
  loadWorkspaces: () => Promise<void>;
  setSelectedWorkspace: (id: string | null) => void;
}

export const useChatStore = create<ChatState>((set, get) => ({
  sessions: [],
  activeSessionId: null,
  messages: {},
  isSessionsLoading: false,
  hasMoreSessions: false,
  sessionListError: false,
  sessionListOffset: 0,
  sessionListAgent: null,
  sessionFilter: {},
  sessionListIds: [],
  hasMoreMessages: {},
  loadingOlderMessages: {},
  messagesLoading: false,
  workspaces: [],
  selectedWorkspaceId: null,
  isWorkspacesLoading: false,

  loadSessions: async (agentId, filter = {}) => {
    latestMessageRequests.clear();
    set({ sessions: [], sessionListIds: [], activeSessionId: null, messages: {},
      hasMoreMessages: {}, loadingOlderMessages: {}, messagesLoading: false });
    await get().refreshSessions(agentId, filter);
  },

  refreshSessions: async (agentId, filter) => {
    const generation = getAuthGeneration();
    const revision = ++listRequest;
    const agent = agentId ?? null;
    const previous = get();
    const query = filter ?? (agent === previous.sessionListAgent ? previous.sessionFilter : {});
    const changed = agent !== previous.sessionListAgent || query.search !== previous.sessionFilter.search
      || query.source !== previous.sessionFilter.source || query.pinned_first !== previous.sessionFilter.pinned_first;
    set({ isSessionsLoading: true, sessionListError: false, sessionFilter: query, sessionListAgent: agent,
      ...(changed && { sessionListIds: [], hasMoreSessions: false, sessionListOffset: 0 }) });
    try {
      const page = await sessionsApi.list(agent, { ...query, limit: SESSION_PAGE_SIZE });
      if (!isCurrentAuthGeneration(generation) || revision !== listRequest) return;
      set(state => ({
        sessions: mergeById(state.sessions.filter(item => item.id === state.activeSessionId), page),
        sessionListIds: page.map(item => item.id), sessionListOffset: page.length,
        hasMoreSessions: page.length === SESSION_PAGE_SIZE,
      }));
    } catch {
      if (isCurrentAuthGeneration(generation) && revision === listRequest) set({ sessionListError: true });
    } finally {
      if (isCurrentAuthGeneration(generation) && revision === listRequest) set({ isSessionsLoading: false });
    }
  },

  loadMoreSessions: async () => {
    const { hasMoreSessions, isSessionsLoading, sessionListOffset, sessionListAgent, sessionFilter } = get();
    if (!hasMoreSessions || isSessionsLoading) return;
    const generation = getAuthGeneration();
    const revision = ++listRequest;
    set({ isSessionsLoading: true, sessionListError: false });
    try {
      const page = await sessionsApi.list(sessionListAgent, {
        ...sessionFilter, limit: SESSION_PAGE_SIZE, offset: sessionListOffset,
      });
      if (!isCurrentAuthGeneration(generation) || revision !== listRequest) return;
      set(state => ({
        sessions: mergeById(state.sessions, page),
        sessionListIds: [...new Set([...state.sessionListIds, ...page.map(item => item.id)])],
        sessionListOffset: sessionListOffset + page.length,
        hasMoreSessions: page.length === SESSION_PAGE_SIZE,
      }));
    } catch {
      if (isCurrentAuthGeneration(generation) && revision === listRequest) set({ sessionListError: true });
    } finally {
      if (isCurrentAuthGeneration(generation) && revision === listRequest) set({ isSessionsLoading: false });
    }
  },

  createSession: async (title, agentId) => {
    const generation = getAuthGeneration();
    const workspaceId = get().selectedWorkspaceId;
    const session = await sessionsApi.create(title, agentId, workspaceId);
    assertAuthGeneration(generation);
    set((state) => ({
      sessions: [session, ...state.sessions],
      ...(matchesList(session, state.sessionListAgent, state.sessionFilter)
        ? { sessionListIds: [session.id, ...state.sessionListIds], sessionListOffset: state.sessionListOffset + 1 }
        : {}),
      activeSessionId: session.id,
      // 新会话无历史消息可加载，复位可能残留的加载指示
      messagesLoading: false,
    }));
    if (get().isSessionsLoading) void get().refreshSessions(get().sessionListAgent);
    return session.id;
  },

  deleteSession: async (id) => {
    const generation = getAuthGeneration();
    await sessionsApi.delete(id);
    assertAuthGeneration(generation);
    get().removeSessionLocal(id);
  },

  removeSessionLocal: (id) => {
    latestMessageRequests.delete(id);
    set((state) => ({
      sessionListIds: state.sessionListIds.filter(item => item !== id),
      sessionListOffset: Math.max(0, state.sessionListOffset - Number(state.sessionListIds.includes(id))),
      hasMoreMessages: Object.fromEntries(Object.entries(state.hasMoreMessages).filter(([key]) => key !== id)),
      loadingOlderMessages: Object.fromEntries(Object.entries(state.loadingOlderMessages).filter(([key]) => key !== id)),
      sessions: state.sessions.filter((s) => s.id !== id),
      activeSessionId: state.activeSessionId === id ? null : state.activeSessionId,
      messages: Object.fromEntries(
        Object.entries(state.messages).filter(([key]) => key !== id),
      ),
    }));
    if (get().isSessionsLoading) void get().refreshSessions(get().sessionListAgent);
  },

  setActiveSession: (id) => {
    // 未缓存的消息触发异步加载并置 loading；已缓存直接使用，清掉可能残留的 loading
    set({ activeSessionId: id, messagesLoading: !!id && !get().messages[id] });
    if (id && !get().messages[id]) {
      get().loadSessionMessages(id);
    }
  },

  loadSessionMessages: async (id) => {
    const generation = getAuthGeneration();
    const revision = ++messageRequest;
    latestMessageRequests.set(id, revision);
    try {
      const detail = await sessionsApi.get(id);
      if (!isCurrentAuthGeneration(generation) || latestMessageRequests.get(id) !== revision) return;
      const { messages: page, has_more_messages: more, ...session } = detail;
      set(state => {
        const cached = state.messages[id] ?? [];
        const start = more && page.length ? cached.findIndex(item => item.id === page[0].id) : -1;
        const older = start > 0 ? cached.slice(0, start) : [];
        return {
          sessions: mergeById(state.sessions, [session]),
          messages: { ...state.messages, [id]: [...older, ...page] },
          hasMoreMessages: { ...state.hasMoreMessages, [id]: older.length ? !!state.hasMoreMessages[id] : !!more },
        };
      });
    } catch {
      // Leave cached history available while disconnected.
    } finally {
      if (isCurrentAuthGeneration(generation) && latestMessageRequests.get(id) === revision && get().activeSessionId === id)
        set({ messagesLoading: false });
    }
  },

  loadOlderMessages: async (id) => {
    const state = get();
    const before = state.messages[id]?.[0]?.id;
    if (!before || !state.hasMoreMessages[id] || state.loadingOlderMessages[id]) return;
    const generation = getAuthGeneration();
    set({ loadingOlderMessages: { ...state.loadingOlderMessages, [id]: true } });
    try {
      const page = await sessionsApi.get(id, before);
      if (!isCurrentAuthGeneration(generation) || get().messages[id]?.[0]?.id !== before) return;
      set(current => ({
        messages: { ...current.messages, [id]: mergeById(page.messages, current.messages[id]) },
        hasMoreMessages: { ...current.hasMoreMessages, [id]: page.has_more_messages },
      }));
    } catch {
      // Keep the oldest cursor available for retry.
    } finally {
      if (isCurrentAuthGeneration(generation) && get().loadingOlderMessages[id])
        set(current => ({ loadingOlderMessages: { ...current.loadingOlderMessages, [id]: false } }));
    }
  },

  addMessage: (sessionId, msg) => {
    set((state) => {
      const existing = state.messages[sessionId] || [];
      return {
        messages: { ...state.messages, [sessionId]: existing.some((item) => item.id === msg.id)
          ? existing.map((item) => item.id === msg.id ? msg : item)
          : [...existing, msg] },
      };
    });
  },

  renameSession: async (id, title) => {
    const generation = getAuthGeneration();
    await sessionsApi.rename(id, title);
    assertAuthGeneration(generation);
    set((state) => ({
      sessions: state.sessions.map((s) => (s.id === id ? { ...s, title } : s)),
    }));
  },

  // Local-only update — used when the backend pushes an auto-generated title
  setSessionTitleLocal: (id, title) => {
    set((state) => ({
      sessions: state.sessions.map((s) => (s.id === id ? { ...s, title } : s)),
    }));
  },

  pinSession: async (id, isPinned) => {
    const generation = getAuthGeneration();
    const saved = await sessionsApi.pin(id, isPinned);
    assertAuthGeneration(generation);
    set((state) => ({
      sessions: state.sessions.map((s) => (s.id === id ? saved : s)),
    }));
  },

  archiveSession: async (id, isArchived) => {
    const generation = getAuthGeneration();
    const saved = await sessionsApi.archive(id, isArchived);
    assertAuthGeneration(generation);
    set((state) => ({
      sessions: state.sessions.map((s) =>
        s.id === id ? saved : s,
      ),
    }));
  },

  loadWorkspaces: async () => {
    const generation = getAuthGeneration();
    set({ isWorkspacesLoading: true });
    try {
      const workspaces = await workspacesApi.list();
      assertAuthGeneration(generation);
      // Default to the default workspace, or the first one
      const defaultWs = workspaces.find((w) => w.is_default) || workspaces[0];
      set({
        workspaces,
        selectedWorkspaceId: defaultWs?.id ?? null,
        isWorkspacesLoading: false,
      });
    } catch {
      if (!isCurrentAuthGeneration(generation)) return;
      set({ isWorkspacesLoading: false });
    }
  },

  setSelectedWorkspace: (id) => {
    set({ selectedWorkspaceId: id });
  },
}));

onAuthSessionChange(() => {
  listRequest++;
  latestMessageRequests.clear();
  useChatStore.setState({ hasMoreSessions: false, sessionListError: false, sessionListIds: [], sessionListOffset: 0, sessionFilter: {},
    sessionListAgent: null, hasMoreMessages: {}, loadingOlderMessages: {}, sessions: [], activeSessionId: null, messages: {}, isSessionsLoading: false, messagesLoading: false, workspaces: [], selectedWorkspaceId: null, isWorkspacesLoading: false });
});
