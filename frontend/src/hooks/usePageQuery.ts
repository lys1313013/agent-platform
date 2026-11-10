import { useCallback, useLayoutEffect, useRef } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';

/** Merge consecutive filter updates before React renders the next location. */
export function usePageQuery() {
  const { search, pathname } = useLocation();
  const navigate = useNavigate();
  const current = useRef(search);
  useLayoutEffect(() => { current.current = search; }, [search]);
  const update = useCallback((patch: Record<string, string | number | null | undefined>) => {
    const next = new URLSearchParams(current.current);
    for (const [key, value] of Object.entries(patch)) {
      if (value == null || value === '') next.delete(key);
      else next.set(key, String(value));
    }
    const encoded = next.toString();
    const nextSearch = encoded ? `?${encoded}` : '';
    if (nextSearch === current.current) return;
    current.current = nextSearch;
    navigate({ pathname, search: nextSearch }, { replace: true });
  }, [navigate, pathname]);
  return [new URLSearchParams(search), update] as const;
}

export function queryPage(value: string | null) {
  const page = Number(value);
  return Number.isSafeInteger(page) && page > 0 ? page : 1;
}

export function queryChoice<T extends string>(value: string | null, choices: readonly T[], fallback: T): T {
  return choices.includes(value as T) ? value as T : fallback;
}
