// Offline cache: big, versioned files (CDN libraries, OCR model) are cache-first;
// the app's own files are network-first so updates show up straight away.
const CACHE = "card-scanner-v2";
// Bump CACHE when models/ or data/ change so phones fetch the new files once.
const IMMUTABLE = [/cdn\.jsdelivr\.net\/npm\/.+@\d/, /\/models\//, /\/data\//];

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(
  caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())
));

self.addEventListener("fetch", e => {
  const url = e.request.url;
  if (e.request.method !== "GET") return;
  const sameOrigin = url.startsWith(self.location.origin);
  if (IMMUTABLE.some(r => r.test(url))) {
    e.respondWith(caches.open(CACHE).then(async c => {
      const hit = await c.match(e.request);
      if (hit) return hit;
      const res = await fetch(e.request);
      if (res.ok) c.put(e.request, res.clone());
      return res;
    }));
  } else if (sameOrigin) {
    e.respondWith(fetch(e.request).then(res => {
      if (res.ok) caches.open(CACHE).then(c => c.put(e.request, res.clone()));
      return res;
    }).catch(() => caches.match(e.request)));
  }
});
