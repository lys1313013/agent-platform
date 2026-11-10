import { useId, useLayoutEffect } from 'react';
import { onAuthSessionChange } from '@/lib/authSession';

const editors = new Map<string, { dirty: boolean; stayWithin?: string }>();
export const clearUnsavedChanges = () => editors.clear();
onAuthSessionChange(clearUnsavedChanges);

export function hasUnsavedChanges(nextPath?: string) {
  return [...editors.values()].some(({ dirty, stayWithin }) => dirty && (!nextPath || !stayWithin
    || (nextPath !== stayWithin && !nextPath.startsWith(`${stayWithin}/`))));
}

export function confirmDiscardChanges(dirty = hasUnsavedChanges()) {
  return !dirty || window.confirm('有尚未保存的修改。确定放弃修改并离开吗？');
}

/** Navigation protection; stayWithin allows moving between sessions of one agent. */
export function useUnsavedChanges(dirty: boolean, stayWithin?: string) {
  const id = useId();
  useLayoutEffect(() => {
    editors.set(id, { dirty, stayWithin });
    return () => { editors.delete(id); };
  }, [id, dirty, stayWithin]);
}
