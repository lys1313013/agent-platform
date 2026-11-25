import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter, Outlet, Route, Routes } from 'react-router-dom';
import { App } from 'antd';
import { expect, it, vi } from 'vitest';
import { sessionsApi, portalApi } from '@/lib/api';
import PortalChatPage from './PortalChatPage';

vi.mock('@/hooks/useMobileChatViewport', () => ({ useMobileChatViewport: () => true }));
vi.mock('@/components/chat/ChatWindow', () => ({ default: () => null }));

it('refreshes mobile history once on each drawer opening with the current search', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  const list = vi.spyOn(sessionsApi, 'list').mockResolvedValue([]);
  vi.spyOn(portalApi, 'getAgent').mockResolvedValue({
    id: 'a', name: '智能体', description: '', icon: 'robot', welcome_message: null, starter_prompts: null,
  });

  async function click(selector: string) {
    const button = document.querySelector<HTMLButtonElement>(selector);
    expect(button).toBeTruthy();
    await act(async () => button!.click());
  }
  async function expectRequest(search?: string) {
    await act(async () => {
      await vi.waitFor(() => expect(list).toHaveBeenCalledTimes(1));
    });
    expect(list).toHaveBeenCalledWith('a', { source: 'chat', limit: 50, pinned_first: true, search });
    list.mockClear();
  }

  try {
    const page = createElement(Route, { path: '/portal/agents/:agentId/chat', element: createElement(PortalChatPage) });
    const shell = createElement(Route, { element: createElement(Outlet, { context: { navigation: null } }) }, page);
    const router = createElement(MemoryRouter, { initialEntries: ['/portal/agents/a/chat'] }, createElement(Routes, {}, shell));
    await act(async () => root.render(createElement(App, {}, router)));
    expect(list).toHaveBeenCalledTimes(1);
    list.mockClear();

    const open = '[aria-label="打开导航与对话历史"]';
    await click(open);
    await expectRequest();

    const input = document.querySelector<HTMLInputElement>('.portal-mobile-drawer input')!;
    expect(input).toBeTruthy();
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, '旧对话');
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await expectRequest('旧对话');
    await click('.ant-drawer-close');
    expect(list).not.toHaveBeenCalled();
    await click(open);
    await expectRequest('旧对话');
  } finally {
    await act(async () => root.unmount());
    container.remove();
    vi.restoreAllMocks();
  }
});
