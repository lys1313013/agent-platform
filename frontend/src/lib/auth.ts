import { invalidateAuthSession } from './authSession';

const ACCESS_KEY = 'aio_access_token';
const REFRESH_KEY = 'aio_refresh_token';

export function decodeJwtPayload(token: string): { role?: string; sub?: string } | null {
  try {
    const payload = token.split('.')[1];
    const decoded = atob(payload.replace(/-/g, '+').replace(/_/g, '/'));
    return JSON.parse(decoded);
  } catch {
    return null;
  }
}

export function getUserRole(): string | null {
  const token = localStorage.getItem(ACCESS_KEY);
  if (!token) return null;
  const payload = decodeJwtPayload(token);
  return payload?.role ?? null;
}

export function getUserId(): string | null {
  const token = localStorage.getItem(ACCESS_KEY);
  if (!token) return null;
  const payload = decodeJwtPayload(token);
  return payload?.sub ?? null;
}

export const tokenStorage = {
  getAccess(): string | null {
    return localStorage.getItem(ACCESS_KEY);
  },

  getRefresh(): string | null {
    return localStorage.getItem(REFRESH_KEY);
  },

  set(access: string, refresh: string): void {
    localStorage.setItem(ACCESS_KEY, access);
    localStorage.setItem(REFRESH_KEY, refresh);
  },

  clear(): void {
    localStorage.removeItem(ACCESS_KEY);
    localStorage.removeItem(REFRESH_KEY);
    invalidateAuthSession();
  },

  hasTokens(): boolean {
    return !!localStorage.getItem(ACCESS_KEY) && !!localStorage.getItem(REFRESH_KEY);
  },
};

/**
 * Attachment URLs now carry a short-lived HMAC token scoped to the object key
 * (minted by the server), so no access token is appended in the browser.
 */
export function withImageAuth(url: string): string {
  return url;
}
