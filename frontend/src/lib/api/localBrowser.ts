import { request } from './transport';

export interface BrowserDevice { id: string; tenant_id: string; label: string; online: boolean; revoked: boolean; expires_at: string; persistent_authorized?: boolean }
export interface BrowserSelection { deviceId: string; urls: string[]; persistent?: boolean }
export interface BrowserAuthorization { grantId: string; sessionId: string }
export interface BrowserState {
  enabled: boolean;
  devices: BrowserDevice[];
  sessions: { id: string; state: string; cleanup_state: string; device_id: string }[];
  pending: { id: string; operation: string; payload: Record<string, unknown>; payload_digest: string; expires_at: string }[];
}
const base = '/local-browser';
const post = <T>(path: string, data: unknown = {}) => request<T>(base + path, { method: 'POST', body: JSON.stringify(data) }, { authRetry: 'never' });
export const localBrowserApi = {
  devices: () => request<BrowserDevice[]>(base + '/devices', { cache: 'no-store' }),
  pair: (requestId: string) => post<{ url: string; expires_at: string }>('/pairings', { request_id: requestId }),
  revoke: (id: string) => request(base + `/devices/${id}`, { method: 'DELETE' }, { authRetry: 'never' }),
  persistentAuthorization: (id: string, authorized: boolean) => request<BrowserDevice>(base + `/devices/${id}/persistent-authorization`, { method: 'PUT', body: JSON.stringify({ authorized }) }, { authRetry: 'never' }),
  state: (sessionId: string) => request<BrowserState>(base + `/chats/${sessionId}`, { cache: 'no-store' }),
  grant: (sessionId: string, selection: BrowserSelection) => post<{ id: string }>('/grants', { session_id: sessionId, device_id: selection.deviceId, urls: selection.urls, persistent: selection.persistent ?? false }),
  approve: (id: string, approved: boolean, digest: string) => post(`/commands/${id}/approval`, { approved, payload_digest: digest }),
  stop: (id: string) => post(`/sessions/${id}/stop`),
  cleanup: (id: string) => post(`/sessions/${id}/confirm-cleanup`),
};
