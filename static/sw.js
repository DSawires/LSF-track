/* App-shell cache. API responses are never cached here: reference data lives in
   IndexedDB where the app can read it offline, and the write queue is IndexedDB
   too. The service worker's only job is that the shell loads with no network. */

// Bump this on every frontend change you ship: browsers re-check sw.js on each
// visit, see the new byte, and swap the whole shell cache for the new one.
const CACHE = "lsf-shell-v4";
const SHELL = [
  "/",
  "/static/css/app.css",
  "/static/js/db.js",
  "/static/js/app.js",
  "/static/icon.svg",
  "/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.pathname.startsWith("/api/")) return;

  event.respondWith(
    caches.match(event.request, { ignoreSearch: url.pathname === "/" }).then(
      (cached) =>
        cached ||
        fetch(event.request).then((response) => {
          if (response.ok && url.origin === location.origin) {
            const copy = response.clone();
            caches.open(CACHE).then((cache) => cache.put(event.request, copy));
          }
          return response;
        }).catch(() => caches.match("/"))
    )
  );
});
