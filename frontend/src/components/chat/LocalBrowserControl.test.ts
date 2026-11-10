import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { App } from 'antd';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import LocalBrowserControl from './LocalBrowserControl';
import { localBrowserApi, type BrowserState } from '@/lib/api/localBrowser';

let node: HTMLDivElement;
let root: Root;
const state: BrowserState = {
  enabled: true, devices: [], sessions: [],
  pending: [{ id: 'command-a', operation: 'navigate', payload: { url: 'https://www.baidu.com' }, payload_digest: 'd'.repeat(64), expires_at: '2030-01-01T00:00:00Z' }],
};
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  node = document.createElement('div'); document.body.appendChild(node); root = createRoot(node);
});
afterEach(async () => { await act(async () => root.unmount()); node.remove(); vi.restoreAllMocks(); });
const render = (sessionId: string) => act(async () => root.render(createElement(MemoryRouter, null,
  createElement(App, null, createElement(LocalBrowserControl, { sessionId, available: true, onChange: () => {} })))));

it('only confirms an explicit click and sends the displayed command digest', async () => {
  vi.spyOn(localBrowserApi, 'state').mockResolvedValue(state);
  const approve = vi.spyOn(localBrowserApi, 'approve').mockResolvedValue({ status: 'queued' });
  await render('session-a');
  expect(approve).not.toHaveBeenCalled();
  const button = [...node.querySelectorAll('button')].find(b => b.textContent === '允许这次操作');
  expect(button).toBeDefined();
  await act(async () => button!.click());
  expect(approve).toHaveBeenCalledExactlyOnceWith('command-a', true, 'd'.repeat(64));
});

it('ignores a late response from the previous conversation', async () => {
  let resolveOld!: (value: BrowserState) => void;
  vi.spyOn(localBrowserApi, 'state').mockImplementation(id => id === 'old'
    ? new Promise(resolve => { resolveOld = resolve; }) : Promise.resolve({ ...state, pending: [] }));
  await render('old');
  await render('new');
  await act(async () => resolveOld(state));
  expect(node.textContent).not.toContain('允许这次操作');
  expect(node.textContent).not.toContain('www.baidu.com');
});

it('automatically selects a single persistently authorized device without an origin list', async () => {
  vi.spyOn(localBrowserApi, 'state').mockResolvedValue({ ...state, pending: [], devices: [
    { id: 'trusted', tenant_id: 'tenant-a', label: 'Chrome', online: true, revoked: false, expires_at: '2030-01-01', persistent_authorized: true },
  ] });
  const onChange = vi.fn();
  await act(async () => root.render(createElement(MemoryRouter, null, createElement(App, null,
    createElement(LocalBrowserControl, { sessionId: 'session-a', available: true, onChange })))));
  expect(onChange).toHaveBeenLastCalledWith({ deviceId: 'trusted', urls: [], persistent: true });
  const pause = [...node.querySelectorAll('button')].find(b => b.textContent === '本轮不使用');
  await act(async () => pause!.click());
  expect(onChange).toHaveBeenLastCalledWith(null);
});

it('does not select an untrusted, offline or ambiguous device automatically', async () => {
  const device = { id: 'a', tenant_id: 'tenant-a', label: 'Chrome', online: true, revoked: false, expires_at: '2030-01-01', persistent_authorized: true };
  const spy = vi.spyOn(localBrowserApi, 'state');
  const onChange = vi.fn();
  for (const devices of [[{ ...device, persistent_authorized: false }], [{ ...device, online: false }], [device, { ...device, id: 'b' }]]) {
    spy.mockResolvedValue({ ...state, pending: [], devices });
    await act(async () => root.render(createElement(MemoryRouter, null, createElement(App, null,
      createElement(LocalBrowserControl, { key: JSON.stringify(devices), sessionId: 'session-a', available: true, onChange })))));
    expect(onChange).toHaveBeenLastCalledWith(null);
  }
});
