import { useEffect } from 'react';
import { useBlocker } from 'react-router-dom';
import { confirmDiscardChanges, hasUnsavedChanges } from '@/hooks/useUnsavedChanges';

export default function UnsavedChangesGuard() {
  const blocker = useBlocker(({ currentLocation, nextLocation }) =>
    currentLocation.pathname !== nextLocation.pathname
    && !confirmDiscardChanges(hasUnsavedChanges(nextLocation.pathname)));
  useEffect(() => {
    if (blocker.state === 'blocked') blocker.reset();
  }, [blocker]);
  useEffect(() => {
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (hasUnsavedChanges()) { event.preventDefault(); event.returnValue = ''; }
    };
    window.addEventListener('beforeunload', beforeUnload);
    return () => window.removeEventListener('beforeunload', beforeUnload);
  }, []);
  return null;
}
