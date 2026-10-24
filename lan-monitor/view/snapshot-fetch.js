// Share recent read responses between panels and same-origin embedded frames.
// Mutations always go directly to the server.
export function createSnapshotFetch(store, request = globalThis.fetch.bind(globalThis), now = Date.now) {
  return async function snapshotFetch(path, options = {}) {
    const url = new URL(path, globalThis.location?.origin || 'http://localhost');
    const ttl = url.pathname === '/api/devices' ? 3000 : 2000;
    if ((options.method || 'GET') !== 'GET') return request(path, options);
    const key = url.href;
    let entry = store.get(key);
    if (!entry || (entry.finished !== null && now() - entry.finished >= ttl)) {
      entry = {finished: null};
      entry.promise = request(path, options).then(async response => {
        // Buffer once so every consumer gets an independent response body.
        const body = await response.arrayBuffer();
        if (!response.ok) store.delete(key);
        entry.finished = now();
        return {body, status: response.status, headers: response.headers};
      }).catch(error => { store.delete(key); throw error; });
      store.set(key, entry);
    }
    const result = await entry.promise;
    return new Response(result.body.slice(0), {status: result.status, headers: result.headers});
  };
}
let owner = globalThis;
try { if (globalThis.top && globalThis.location && globalThis.top.location.origin === globalThis.location.origin) owner = globalThis.top; } catch {}
const store = owner.__lanSnapshots ||= new Map();
export const snapshotFetch = createSnapshotFetch(store);
