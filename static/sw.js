/* Three caches, three strategies:

   - Shell (HTML/CSS/JS): cache-first, versioned by the server. The cache name
     carries a hash of static/ stamped into __STATIC_HASH__ when /sw.js is
     served, so every shipped frontend change busts the shell cache without
     anyone remembering to bump a constant.
   - Images (/api/images/*): cache-first, because an image id names immutable
     bytes; trimmed so photos can't grow the cache without bound.
   - Data (other /api/ GETs): network-first with cache fallback, so lists,
     photos indexes and reports stay readable in a dead spot — showing
     last-known data beats an error screen on the factory floor. A cache hit
     is marked with X-LSF-From-Cache so the app can say the data is stale.

   Writes (POST) never touch this file; the IndexedDB queue owns those. The
   one exception is the Background Sync drain below, which exists so a queue
   built up in a dead spot still syncs after the engineer pockets the phone. */

importScripts("/static/js/db.js"); // LSF_DB: IndexedDB only, safe in a worker

const VERSION = "__STATIC_HASH__";
const SHELL = `lsf-shell-${VERSION}`;
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
  "/static/icon-192.png",
  "/static/icon-512.png",
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
    if (hit) {
      // Mark the hit so the app can tell the user they're looking at
      // last-known data, and when it was fetched.
      const headers = new Headers(hit.headers);
      headers.set("X-LSF-From-Cache", hit.headers.get("date") || "1");
      return new Response(hit.body, { status: hit.status, statusText: hit.statusText, headers });
    }
    return new Response(JSON.stringify({ detail: "offline and not cached" }), {
      status: 503,
      headers: { "Content-Type": "application/json" },
    });
  }
}

async function shellCacheFirst(request, ignoreSearch) {
  const shell = await caches.open(SHELL);
  const cached = await shell.match(request, { ignoreSearch });
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response.ok && new URL(request.url).origin === location.origin) {
      await shell.put(request, response.clone());
    }
    return response;
  } catch (_networkDown) {
    return shell.match("/");
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

/* Background Sync: drain the queue even when no page is open. Deliberately
   minimal — batch-post pending entries and ack what the server confirms. The
   page's sync() owns the full logic (poison-entry isolation, reference
   refresh); this only has to move the queue. A network failure rejects the
   waitUntil promise so the browser retries when connectivity returns. */
self.addEventListener("sync", (event) => {
  if (event.tag === "lsf-drain") event.waitUntil(drainFromWorker());
});

async function drainFromWorker() {
  const queue = (await LSF_DB.queueAll()).filter((q) => q._status === "pending");
  for (let start = 0; start < queue.length; start += 100) {
    const events = queue
      .slice(start, start + 100)
      .map(({ _queued_at, _status, _reason, ...event }) => event);
    const response = await fetch("/api/events/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ events }),
    });
    if (!response.ok) return; // 4xx/401 needs the page's UI; leave the queue alone
    const { results } = await response.json();
    for (const result of results) {
      if (result.status === "stored" || result.status === "duplicate") {
        await LSF_DB.ack(result.id);
      } else if (result.status === "rejected") {
        await LSF_DB.markRejected(result.id, result.reason || "rejected");
      }
      // "error" stays pending; the page's sync retries it.
    }
  }
  const clients = await self.clients.matchAll();
  clients.forEach((client) => client.postMessage("queue-drained"));
}

/* The app posts "purge-data" on logout so the next user of a shared floor
   phone doesn't see the previous user's cached lists and reports. */
self.addEventListener("message", (event) => {
  if (event.data === "purge-data") event.waitUntil(caches.delete(DATA));
});
