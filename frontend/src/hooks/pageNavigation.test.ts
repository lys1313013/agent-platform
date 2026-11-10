import { act, createElement, useState } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { createMemoryRouter, RouterProvider, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { usePageQuery, queryPage } from './usePageQuery';
import { useUnsavedChanges, hasUnsavedChanges } from './useUnsavedChanges';
import UnsavedChangesGuard from '@/components/UnsavedChangesGuard';

let root: Root;
let router: ReturnType<typeof createMemoryRouter>;
let query: ReturnType<typeof usePageQuery>;
let setDirty: (dirty: boolean) => void;
function Harness() {
  const location = useLocation();
  const [dirty, updateDirty] = useState(false);
  setDirty = updateDirty;
  query = usePageQuery();
  useUnsavedChanges(dirty, '/agents/a');
  return createElement('div', null, createElement(UnsavedChangesGuard), location.pathname);
}
beforeEach(async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  root = createRoot(document.createElement('div'));
  router = createMemoryRouter([{ path: '*', element: createElement(Harness) }], { initialEntries: ['/agents/a/chat/one?status=failed&page=3'] });
  await act(async () => root.render(createElement(RouterProvider, { router })));
});
afterEach(async () => { await act(async () => root.unmount()); router.dispose(); vi.restoreAllMocks(); });

it('preserves URL filters and merges updates issued in one interaction', async () => {
  expect(queryPage(query[0].get('page'))).toBe(3);
  await act(async () => { query[1]({ status: 'success' }); query[1]({ page: null, job: 'daily' }); });
  expect(router.state.location.search).toBe('?status=success&job=daily');
  expect(queryPage(query[0].get('page'))).toBe(1);
  await act(async () => router.navigate('/agents/a/chat/one?status=failed&page=4'));
  expect(query[0].get('status')).toBe('failed');
  expect(queryPage(query[0].get('page'))).toBe(4);
});

it('keeps unsaved edits when navigation is cancelled and allows confirmed navigation', async () => {
  await act(async () => setDirty(true));
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  await act(async () => router.navigate('/agents/b/chat'));
  expect(router.state.location.pathname).toBe('/agents/a/chat/one');
  expect(confirm).toHaveBeenCalledTimes(1);
  confirm.mockReturnValue(true);
  await act(async () => router.navigate('/agents/b/chat'));
  expect(router.state.location.pathname).toBe('/agents/b/chat');
});

it('allows session changes inside the same agent and protects refresh only while dirty', async () => {
  await act(async () => setDirty(true));
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  await act(async () => router.navigate('/agents/a/chat/two'));
  expect(router.state.location.pathname).toBe('/agents/a/chat/two');
  expect(confirm).not.toHaveBeenCalled();
  const event = new Event('beforeunload', { cancelable: true });
  window.dispatchEvent(event);
  expect(event.defaultPrevented).toBe(true);
  await act(async () => setDirty(false));
  expect(hasUnsavedChanges()).toBe(false);
  const clean = new Event('beforeunload', { cancelable: true });
  window.dispatchEvent(clean);
  expect(clean.defaultPrevented).toBe(false);
});

it('rejects invalid page query values', () => {
  for (const value of ['-1', '0', '1.5', 'NaN', 'Infinity', '9007199254740992']) expect(queryPage(value)).toBe(1);
});
