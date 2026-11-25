import type { Session, SessionDetail, ChatAttachment, ChatResponse, ChatRequest } from '../types';
import { API_BASE, queryString, request, uploadForm } from './transport';
import { openJsonEventStream, type EventStreamController } from './sse';

// ---- Sessions ----

export const sessionsApi = {
  list(agentId?: string | null, page: { search?: string; source?: string; pinned_first?: boolean; limit?: number; offset?: number } = {}) {
    return request<Session[]>(`/sessions${queryString({ agent_id: agentId ?? undefined, ...page })}`);
  },

  get(id: string, before?: string) {
    return request<SessionDetail>(`/sessions/${id}${queryString({ message_limit: 50, before })}`);
  },

  getStatus(id: string) {
    return request<import('../types').SessionStatus>(`/sessions/${id}/status`);
  },

  /**
   * Replay a running channel task's agent events via SSE (GET /sessions/{id}/events).
   * Fires `onEvent` per event; emits `{ type: 'closed' }` on normal EOF so the
   * caller can finalize streaming state. Returns an AbortController for cancel.
   */
  watchEvents(id: string, onEvent: (event: Record<string, unknown>) => void): AbortController {
    return openJsonEventStream(`/sessions/${id}/events`, onEvent, {}, 'error');
  },

  create(title?: string, agentId?: string | null, workspaceId?: string | null) {
    return request<Session>('/sessions', {
      method: 'POST',
      body: JSON.stringify({ title, agent_id: agentId, workspace_id: workspaceId }),
    });
  },

  rename(id: string, title: string) {
    return request<Session>(`/sessions/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ title }),
    });
  },

  pin(id: string, isPinned: boolean) {
    return request<Session>(`/sessions/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ is_pinned: isPinned }),
    });
  },

  archive(id: string, isArchived: boolean) {
    return request<Session>(`/sessions/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ is_archived: isArchived }),
    });
  },

  delete(id: string) {
    return request<void>(`/sessions/${id}`, { method: 'DELETE' });
  },
};

// ---- Chat ----

export interface ChatRunInfo {
  id: string;
  session_id: string;
  assistant_message_id: string;
  status: 'running' | 'completed' | 'interrupted' | 'failed' | 'stopped';
  last_sequence: number;
  stop_requested: boolean;
  can_resume: boolean;
  uncertain_tools: string[];
}

function openChatStream(
  path: string,
  onEvent: (event: Record<string, unknown>) => void,
  req?: ChatRequest | Record<string, never>,
): EventStreamController {
  return openJsonEventStream(path, onEvent, {
    method: req ? 'POST' : 'GET',
    body: req ? JSON.stringify(req) : undefined,
  });
}

export const chatApi = {
  send(req: ChatRequest) {
    return request<ChatResponse>('/chat', {
      method: 'POST',
      body: JSON.stringify(req),
    });
  },

  /**
   * Streaming chat via SSE (POST /api/chat/stream).
   * Calls `onEvent` for each SSE event, returns an AbortController
   * so the caller can detach the subscription; stopRun cancels execution.
   */
  stream(req: ChatRequest, onEvent: (event: Record<string, unknown>) => void): EventStreamController {
    return openChatStream('/chat/stream', onEvent, req);
  },

  latestRun(sessionId: string) {
    return request<ChatRunInfo | null>(`/chat/sessions/${sessionId}/run`);
  },

  watchRun(id: string, onEvent: (event: Record<string, unknown>) => void, after = 0): AbortController {
    return openChatStream(`/chat/runs/${id}/events?after=${after}`, onEvent);
  },

  stopRun(id: string) {
    return request<ChatRunInfo>(`/chat/runs/${id}/stop`, { method: 'POST' });
  },

  resumeRun(id: string, onEvent: (event: Record<string, unknown>) => void): AbortController {
    return openChatStream(`/chat/runs/${id}/resume`, onEvent, {});
  },

  /**
   * Upload an image attachment for a future chat message.
   * Returns metadata including a presigned download URL.
   */
  uploadAttachment(file: File, sessionId?: string | null): Promise<ChatAttachment> {
    const form = new FormData();
    form.append('file', file);
    if (sessionId) form.append('session_id', sessionId);
    return uploadForm('/chat/attachments', form);
  },

  /** Upload a file to the workspace for agent processing. */
  uploadFile(file: File, sessionId: string): Promise<import('../types').FileAttachmentRef> {
    const form = new FormData();
    form.append('file', file);
    form.append('session_id', sessionId);
    return uploadForm('/chat/files', form);
  },

  /** Build the WebSocket URL for a session (legacy) */
  wsUrl(sessionId: string): string {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = window.location.host;
    return `${protocol}//${host}${API_BASE}/chat/ws/${sessionId}`;
  },
};
