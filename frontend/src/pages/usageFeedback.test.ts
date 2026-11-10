import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { analyticsApi } from '@/lib/api';
import UsagePage from './UsagePage';

vi.mock('@ant-design/plots', () => ({ Bar: () => null, Column: () => null, Line: () => null, Pie: () => null }));
let root: Root;
let host: HTMLDivElement;
const summary = { sessions: 1, messages: 2, prompt_tokens: 100, completion_tokens: 11, total_tokens: 111, request_count: 1, active_users: null, prev_sessions: 0, prev_messages: 0, prev_total_tokens: 0, prev_request_count: 0 };
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  host = document.createElement('div'); document.body.append(host); root = createRoot(host);
  vi.spyOn(analyticsApi, 'summary').mockResolvedValue(summary);
  vi.spyOn(analyticsApi, 'trend').mockResolvedValue([]);
  vi.spyOn(analyticsApi, 'distribution').mockResolvedValue([]);
  vi.spyOn(analyticsApi, 'detail').mockResolvedValue({ items: [], total: 0 });
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.restoreAllMocks(); });
const render = async () => { await act(async () => root.render(createElement(UsagePage))); };
const changeRange = async () => { await act(async () => (host.querySelector('input[value="7d"]') as HTMLInputElement).click()); };

it('shows a retryable error instead of statistics from an earlier time range', async () => {
  await render();
  expect(host.textContent).toContain('111');
  vi.mocked(analyticsApi.summary).mockRejectedValueOnce(new Error('offline'));
  await changeRange();
  expect(host.textContent).toContain('用量数据加载失败');
  expect(host.textContent).not.toContain('111');
  const retry = [...host.querySelectorAll('button')].find(button => button.textContent?.replace(/\s/g, '') === '重试')!;
  await act(async () => retry.click());
  expect(host.textContent).not.toContain('用量数据加载失败');
  expect(host.textContent).toContain('111');
});

it('ignores detail responses from a previous filter after a newer response arrived', async () => {
  type Detail = Awaited<ReturnType<typeof analyticsApi.detail>>;
  let oldResolve!: (value: Detail) => void;
  let currentResolve!: (value: Detail) => void;
  vi.mocked(analyticsApi.detail).mockImplementationOnce(() => new Promise(resolve => { oldResolve = resolve; }))
    .mockImplementationOnce(() => new Promise(resolve => { currentResolve = resolve; }));
  await render();
  await changeRange();
  const detail = (model: string): Detail => ({ items: [{ date: '2026-10-31', model, prompt_tokens: 10, completion_tokens: 1, total_tokens: 11, request_count: 1 }], total: 1 });
  await act(async () => currentResolve(detail('current-model')));
  await act(async () => oldResolve(detail('stale-model')));
  expect(host.textContent).toContain('current-model');
  expect(host.textContent).not.toContain('stale-model');
});
