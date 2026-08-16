/* Three caches, three strategies:

   - Shell (HTML/CSS/JS): cache-first, versioned by hand. Bump SHELL on every
     frontend change you ship.
   - Images (/api/images/*): cache-first, because an image id names immutable
     bytes; trimmed so photos can't grow the cache without bound.
   - Data (other /api/ GETs): network-first with cache fallback, so lists,
     photos indexes and reports stay readable in a dead spot — showing
     last-known data beats an error screen on the factory floor.

   Writes (POST) never touch this file; the IndexedDB queue owns those. */

const SHELL = "lsf-shell-v8";
const IMAGES = "lsf-img-v1";
const DATA = "lsf-data-v1";
const KEEP = [SHELL, IMAGES, DATA];
const IMAGE_CACHE_MAX_ENTRIES = 300;

const SHELL_FILES = [
  "/",
  "/static/css/app.css",
  "/static/js/db.js",
  "/static/js/app.js",
  "/static/icon.svg",
  "/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL).then((cache) => cache.addAll(SHELL_FILES)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => !KEEP.includes(k)).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

async function imageCacheFirst(request) {
  const cache = await caches.open(IMAGES);
  const hit = await cache.match(request);
  if (hit) return hit;
  const response = await fetch(request);
  if (response.ok) {
    await cache.put(request, response.clone());
    trimImages(cache); // fire and forget
  }
  return response;
}

async function trimImages(cache) {
  const keys = await cache.keys();
  for (const key of keys.slice(0, Math.max(0, keys.length - IMAGE_CACHE_MAX_ENTRIES))) {
    await cache.delete(key);
  }
}

async function dataNetworkFirst(request) {
  const cache = await caches.open(DATA);
  try {
    const response = await fetch(request);
    if (response.ok) cache.put(request, response.clone());
    return response;
  } catch (_networkDown) {
    const hit = await cache.match(request);
    if (hit) return hit;
    return new Response(JSON.stringify({ detail: "offline and not cached" }), {
      status: 503,
      headers: { "Content-Type": "application/json" },
    });
  }
}

async function shellCacheFirst(request, ignoreSearch) {
  const cached = await caches.match(request, { ignoreSearch });
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response.ok && new URL(request.url).origin === location.origin) {
      const cache = await caches.open(SHELL);
      await cache.put(request, response.clone());
    }
    return response;
  } catch (_networkDown) {
    return caches.match("/");
  }
}

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin) return;

  if (url.pathname.startsWith("/api/images/")) {
    event.respondWith(imageCacheFirst(event.request));
  } else if (url.pathname.startsWith("/api/")) {
    event.respondWith(dataNetworkFirst(event.request));
  } else {
    event.respondWith(shellCacheFirst(event.request, url.pathname === "/"));
  }
});
