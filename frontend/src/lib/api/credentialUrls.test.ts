import { afterEach, expect, it, vi } from 'vitest';
import { tokenStorage } from '../auth';
import { chatApi } from './chat';
import { uiActionsApi } from './interaction';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it('keeps login credentials out of unload and WebSocket request URLs', () => {
  vi.spyOn(tokenStorage, 'getAccess').mockReturnValue('test-access-token');
  const send = vi.fn().mockResolvedValue(new Response());
  vi.stubGlobal('fetch', send);
  uiActionsApi.cancelAllBeacon('test-session');
  const [url, init] = send.mock.calls[0];
  expect(url).toMatch(/\/ui-actions\/sessions\/test-session\/cancel_all$/);
  expect(init.keepalive).toBe(true);
  expect(new Headers(init.headers).get('Authorization')).toBe('Bearer test-access-token');
  expect(new URL(chatApi.wsUrl('test-session')).search).toBe('');
});
