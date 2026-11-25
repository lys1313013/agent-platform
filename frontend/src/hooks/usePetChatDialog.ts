import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { petsApi } from '@/lib/api';
import { getAuthGeneration, onAuthSessionChange } from '@/lib/authSession';

/** Scope both the visible conversation and pending requests to one workspace. */
export function usePetChatDialog(petId: string | null, workspaceId: string | null) {
  const generation = useSyncExternalStore(onAuthSessionChange, getAuthGeneration);
  // A -> B -> A must also discard requests from the first visit to A.
  const scope = useMemo(() => ({ petId, workspaceId, generation }), [petId, workspaceId, generation]);
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const requestRef = useRef(0);
  const mountedRef = useRef(false);
  const [dialog, setDialog] = useState<{
    scope: typeof scope;
    sessionId: string;
    agentId: string | null;
  } | null>(null);

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const openChat = useCallback(async (forceNew = false) => {
    if (!scope.petId || scopeRef.current !== scope || !mountedRef.current) return;
    const request = ++requestRef.current;
    const isCurrent = () => mountedRef.current && scopeRef.current === scope
      && getAuthGeneration() === scope.generation && requestRef.current === request;
    try {
      const result = await petsApi.petChat(scope.petId, { forceNew, workspaceId: scope.workspaceId });
      if (isCurrent()) setDialog({ scope, sessionId: result.conversation_id, agentId: result.agent_id });
    } catch (error) {
      if (isCurrent()) throw error;
    }
  }, [scope]);

  const closeChat = useCallback(() => {
    requestRef.current += 1;
    setDialog(null);
  }, []);

  const current = dialog?.scope === scope ? dialog : null;
  return {
    open: current !== null,
    sessionId: current?.sessionId ?? null,
    agentId: current?.agentId ?? null,
    openChat,
    closeChat,
  };
}
