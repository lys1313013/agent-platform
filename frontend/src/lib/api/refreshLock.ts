// Web Locks require HTTPS. An IndexedDB read/write transaction also serializes
// tabs on HTTP deployments; the lease expires if its owner closes mid-refresh.
async function openLockDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open('aio-auth-locks', 1);
    request.onupgradeneeded = () => request.result.createObjectStore('leases');
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function changeLease(db: IDBDatabase, owner: string, release = false): Promise<boolean> {
  return new Promise((resolve, reject) => {
    const transaction = db.transaction('leases', 'readwrite');
    const store = transaction.objectStore('leases');
    const request = store.get('refresh');
    let acquired = false;
    request.onsuccess = () => {
      const lease = request.result as { owner: string; expires: number } | undefined;
      if (release) {
        if (lease?.owner === owner) store.delete('refresh');
      } else if (!lease || lease.expires <= Date.now()) {
        // The refresh request times out well before another tab can take over.
        store.put({ owner, expires: Date.now() + 30_000 }, 'refresh');
        acquired = true;
      }
    };
    transaction.oncomplete = () => resolve(acquired);
    transaction.onabort = () => reject(transaction.error ?? new Error('无法协调登录凭据刷新'));
  });
}

export async function withRefreshLock(signal: AbortSignal, rotate: () => Promise<boolean>): Promise<boolean> {
  if (navigator.locks) return navigator.locks.request('aio-refresh-token', { signal }, rotate);
  if (typeof indexedDB === 'undefined') return rotate();
  const db = await openLockDatabase();
  const owner = Array.from(crypto.getRandomValues(new Uint32Array(4))).join('-');
  let acquired = false;
  try {
    while (!acquired) {
      signal.throwIfAborted();
      acquired = await changeLease(db, owner);
      if (!acquired) await new Promise(resolve => setTimeout(resolve, 200));
    }
    signal.throwIfAborted();
    return await rotate();
  } finally {
    try {
      if (acquired) await changeLease(db, owner, true);
    } finally {
      db.close();
    }
  }
}
