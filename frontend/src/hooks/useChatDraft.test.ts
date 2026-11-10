import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it } from 'vitest';
import { useChatDraft } from './useChatDraft';
import { invalidateAuthSession } from '@/lib/authSession';
import { useAuthStore } from '@/stores/authStore';

let root: Root;
let draft: ReturnType<typeof useChatDraft>;
function Harness({ id, scope = 'agent-a' }: { id?: string; scope?: string }) {
  draft = useChatDraft(id, scope);
  return createElement('p', null, draft.text);
}
const render = async (id?: string, scope?: string) => { await act(async () => root.render(createElement(Harness, { id, scope }))); };
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  invalidateAuthSession();
  useAuthStore.setState({ tenantId: 'tenant-a', username: 'user-a' });
  root = createRoot(document.createElement('div'));
});
afterEach(async () => { await act(async () => root.unmount()); });

it('restores each conversation draft without carrying text or attachments into another', async () => {
  await render('a');
  await act(async () => { draft.setText('draft A'); draft.setFiles([{ localId: 'file', status: 'uploading', fileName: 'a.txt', fileSize: 1 }]); });
  const old = draft;
  await render('b');
  expect(draft.text).toBe('');
  expect(draft.files).toEqual([]);
  await act(async () => draft.setText('draft B'));
  // A delayed upload must settle only in the original conversation.
  await act(async () => old.setFiles(files => files.map(file => ({ ...file, status: 'done' }))));
  expect(draft.text).toBe('draft B');
  expect(draft.files).toEqual([]);
  await render('a');
  expect(draft.text).toBe('draft A');
  expect(draft.files[0].status).toBe('done');
});

it('an accepted send in A never clears a newer draft in B', async () => {
  await render('a');
  const sending = draft;
  await render('b');
  await act(async () => draft.setText('keep B'));
  await act(async () => { sending.setText(''); sending.setImages([]); sending.setFiles([]); });
  expect(draft.text).toBe('keep B');
});

it('promotes an unsent draft when uploading creates its session', async () => {
  await render();
  await act(async () => draft.setText('new draft'));
  const uploading = draft;
  await render('created'); // navigation can render before ensureSession resolves
  await act(async () => uploading.promote('created'));
  expect(draft.text).toBe('new draft');
  await act(async () => uploading.setFiles([{ localId: 'f', fileName: 'a.txt', fileSize: 1, status: 'done' }]));
  expect(draft.files).toHaveLength(1);
  await render();
  expect(draft.text).toBe('');
  expect(draft.files).toEqual([]);
});

it('isolates new conversations for different agents and tenants', async () => {
  await render();
  await act(async () => draft.setText('private draft'));
  const old = draft;
  await render(undefined, 'agent-b');
  expect(draft.text).toBe('');
  await act(async () => useAuthStore.setState({ tenantId: 'tenant-b' }));
  await render(undefined, 'agent-a');
  expect(draft.text).toBe('');
  await act(async () => old.setText('stale'));
  expect(draft.text).toBe('');
});

it('clears drafts on logout and rejects callbacks from an earlier login', async () => {
  await render('a');
  await act(async () => draft.setText('secret draft'));
  const old = draft;
  await act(async () => invalidateAuthSession());
  expect(draft.text).toBe('');
  await act(async () => old.setText('stale callback'));
  expect(draft.text).toBe('');
});
