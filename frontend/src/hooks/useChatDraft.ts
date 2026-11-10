import { useSyncExternalStore, type SetStateAction } from 'react';
import { useAuthStore } from '@/stores/authStore';
import { getAuthGeneration, onAuthSessionChange } from '@/lib/authSession';
import type { ChatAttachment, FileAttachmentRef } from '@/lib/types';

export interface PendingAttachment {
  localId: string;
  status: 'uploading' | 'done' | 'error';
  thumb?: string;
  attachment?: ChatAttachment;
  error?: string;
}
export interface PendingFile {
  localId: string;
  status: 'uploading' | 'done' | 'error';
  fileName: string;
  fileSize: number;
  fileRef?: FileAttachmentRef;
  error?: string;
}
interface Draft { text: string; images: PendingAttachment[]; files: PendingFile[] }
interface Entry { value: Draft }
const drafts = new Map<string, Entry>();
const listeners = new Set<() => void>();
const empty = (): Draft => ({ text: '', images: [], files: [] });
const notify = () => listeners.forEach((listener) => listener());
const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
const identity = () => JSON.stringify([getAuthGeneration(), useAuthStore.getState().tenantId, useAuthStore.getState().username]);
const entryFor = (key: string) => {
  let entry = drafts.get(key);
  if (!entry) { entry = { value: empty() }; drafts.set(key, entry); }
  return entry;
};
onAuthSessionChange(() => { drafts.clear(); notify(); });

/** Memory-only drafts. Async setters retain their originating conversation. */
export function useChatDraft(sessionId?: string | null, scope = 'chat') {
  useSyncExternalStore(onAuthSessionChange, getAuthGeneration);
  useAuthStore((state) => `${state.tenantId}:${state.username}`);
  const owner = identity();
  const keyFor = (id?: string | null) => JSON.stringify([owner, scope, id ?? 'new']);
  const key = keyFor(sessionId);
  const draft = useSyncExternalStore(subscribe, () => entryFor(key).value);
  const entry = entryFor(key);
  function set<K extends keyof Draft>(field: K, update: SetStateAction<Draft[K]>) {
    if (identity() !== owner) return;
    entry.value = { ...entry.value, [field]: typeof update === 'function'
      ? (update as (old: Draft[K]) => Draft[K])(entry.value[field]) : update };
    notify();
  }
  return {
    ...draft,
    getDraft: () => entry.value,
    setText: (value: SetStateAction<string>) => set('text', value),
    setImages: (value: SetStateAction<PendingAttachment[]>) => set('images', value),
    setFiles: (value: SetStateAction<PendingFile[]>) => set('files', value),
    promote: (id: string) => {
      if (sessionId || identity() !== owner) return;
      drafts.set(keyFor(id), entry);
      if (drafts.get(key) === entry) drafts.delete(key);
      notify();
    },
  };
}
