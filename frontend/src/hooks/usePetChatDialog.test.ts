import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { petsApi } from '@/lib/api';
import { usePetChatDialog } from './usePetChatDialog';

vi.mock('@/lib/api', () => ({ petsApi: { petChat: vi.fn() } }));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((yes) => { resolve = yes; });
  return { promise, resolve };
}

let root: Root;
let dialog: ReturnType<typeof usePetChatDialog>;
function Harness({ workspaceId, petId }: { workspaceId: string; petId: string }) {
  dialog = usePetChatDialog(petId, workspaceId);
  return null;
}
async function render(workspaceId = 'ws-a', petId = 'pet-1') {
  await act(async () => root.render(createElement(Harness, { workspaceId, petId })));
}

beforeEach(() => {
  vi.clearAllMocks();
  (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
  vi.mocked(petsApi.petChat).mockImplementation(async (_id, opts) => ({
    conversation_id: `session-${opts?.workspaceId}`, agent_id: 'agent-1',
  }));
  root = createRoot(document.createElement('div'));
});
afterEach(async () => { await act(async () => root.unmount()); });

describe('pet conversation workspace scope', () => {
  it('closes the old conversation on a workspace change and opens the requested workspace', async () => {
    await render();
    await act(async () => dialog.openChat());
    expect(dialog.sessionId).toBe('session-ws-a');
    await render('ws-b');
    expect(dialog.open).toBe(false);
    expect(dialog.sessionId).toBeNull();
    await act(async () => dialog.openChat());
    expect(dialog.sessionId).toBe('session-ws-b');
    expect(petsApi.petChat).toHaveBeenLastCalledWith('pet-1', { forceNew: false, workspaceId: 'ws-b' });
  });

  it('does not revive an old dialog when switching A -> B -> A', async () => {
    await render();
    await act(async () => dialog.openChat());
    await render('ws-b');
    await render('ws-a');
    expect(dialog.open).toBe(false);
    expect(dialog.sessionId).toBeNull();
  });

  it('ignores a late open response even after returning to its original workspace', async () => {
    const old = deferred<Awaited<ReturnType<typeof petsApi.petChat>>>();
    vi.mocked(petsApi.petChat).mockReturnValueOnce(old.promise);
    await render();
    let pending!: Promise<void>;
    await act(async () => { pending = dialog.openChat(); });
    await render('ws-b');
    await render('ws-a');
    await act(async () => { old.resolve({ conversation_id: 'stale', agent_id: null }); await pending; });
    expect(dialog.open).toBe(false);
  });

  it('ignores a late force-new response after another workspace is opened', async () => {
    await render();
    await act(async () => dialog.openChat());
    const old = deferred<Awaited<ReturnType<typeof petsApi.petChat>>>();
    vi.mocked(petsApi.petChat).mockReturnValueOnce(old.promise);
    let pending!: Promise<void>;
    await act(async () => { pending = dialog.openChat(true); });
    await render('ws-b');
    await act(async () => dialog.openChat());
    await act(async () => { old.resolve({ conversation_id: 'stale-new', agent_id: null }); await pending; });
    expect(dialog.sessionId).toBe('session-ws-b');
  });

  it('does not reopen after closing while a request is pending', async () => {
    const old = deferred<Awaited<ReturnType<typeof petsApi.petChat>>>();
    vi.mocked(petsApi.petChat).mockReturnValueOnce(old.promise);
    await render();
    let pending!: Promise<void>;
    await act(async () => { pending = dialog.openChat(); });
    await act(async () => dialog.closeChat());
    await act(async () => { old.resolve({ conversation_id: 'stale', agent_id: null }); await pending; });
    expect(dialog.open).toBe(false);
  });

  it('invalidates callbacks and conversations when the active pet changes', async () => {
    await render();
    await act(async () => dialog.openChat());
    const oldOpen = dialog.openChat;
    await render('ws-a', 'pet-2');
    expect(dialog.open).toBe(false);
    await act(async () => oldOpen(true));
    expect(petsApi.petChat).toHaveBeenCalledTimes(1);
  });
});
