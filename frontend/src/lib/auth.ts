import { invalidateAuthSession } from './authSession';

const ACCESS_KEY = 'aio_access_token';
const REFRESH_KEY = 'aio_refresh_token';
const CONTEXT_KEY = 'aio_browser_context';
let pageContext = localStorage.getItem(CONTEXT_KEY);
let pageUser = getUserId();
let pageTenant: string | null = null;

function publishContext() {
  pageContext = crypto.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
  localStorage.setItem(CONTEXT_KEY, pageContext);
}

function pageContextIsCurrent(): boolean {
  return localStorage.getItem(CONTEXT_KEY) === pageContext && getUserId() === pageUser;
}

function reloadIfContextChanged(): boolean {
  if (pageContextIsCurrent()) return false;
  invalidateAuthSession();
  window.location.reload();
  return true;
}

export function assertBrowserContext() {
  if (reloadIfContextChanged()) throw new DOMException('账号或租户已变更', 'AbortError');
}

window.addEventListener('storage', event => {
  if (event.key === CONTEXT_KEY || event.key === ACCESS_KEY || event.key === null) reloadIfContextChanged();
});
window.addEventListener('pageshow', () => { reloadIfContextChanged(); });

export const expectedUser = () => pageUser;
export const expectedTenant = () => pageTenant;
export function rememberTenant(tenant: string) { pageTenant = tenant; }
export function tenantSwitched() {
  publishContext();
  invalidateAuthSession();
  pageTenant = null;
}

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
    const nextUser = decodeJwtPayload(access)?.sub ?? null;
    if (pageUser !== nextUser || !localStorage.getItem(ACCESS_KEY)) publishContext();
    pageUser = nextUser;
    localStorage.setItem(ACCESS_KEY, access);
    localStorage.setItem(REFRESH_KEY, refresh);
  },

  clear(): void {
    publishContext();
    pageUser = null;
    pageTenant = null;
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
