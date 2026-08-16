/* IndexedDB: the write queue and the offline reference cache.
   Queue entries are removed ONLY when the server acknowledges the event id.
   Everything else in here is a cache and can be rebuilt from the server. */

"use strict";

const LSF_DB = (() => {
  const NAME = "lsf-track";
  const VERSION = 1;
  let handle = null;

  function open() {
    if (handle) return Promise.resolve(handle);
    return new Promise((resolve, reject) => {
      const request = indexedDB.open(NAME, VERSION);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains("queue")) {
          db.createObjectStore("queue", { keyPath: "id" });
        }
        if (!db.objectStoreNames.contains("kv")) {
          db.createObjectStore("kv"); // reference payload, items snapshot, meta
        }
      };
      request.onsuccess = () => { handle = request.result; resolve(handle); };
      request.onerror = () => reject(request.error);
    });
  }

  function tx(store, mode, work) {
    return open().then((db) => new Promise((resolve, reject) => {
      const transaction = db.transaction(store, mode);
      const result = work(transaction.objectStore(store));
      transaction.oncomplete = () => resolve(result && result.result !== undefined ? result.result : result);
      transaction.onerror = () => reject(transaction.error);
    }));
  }

  function getAll(store) {
    return open().then((db) => new Promise((resolve, reject) => {
      const request = db.transaction(store).objectStore(store).getAll();
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    }));
  }

  return {
    // ---- write queue ----
    enqueue: (event) => tx("queue", "readwrite", (s) => s.put({ ...event, _queued_at: Date.now(), _status: "pending" })),
    queueAll: () => getAll("queue"),
    ack: (id) => tx("queue", "readwrite", (s) => s.delete(id)),
    markRejected: (id, reason) =>
      tx("queue", "readwrite", (s) => {
        const req = s.get(id);
        req.onsuccess = () => {
          if (req.result) s.put({ ...req.result, _status: "rejected", _reason: reason });
        };
      }),
    dropRejected: (id) => tx("queue", "readwrite", (s) => s.delete(id)),

    // ---- kv cache ----
    put: (key, value) => tx("kv", "readwrite", (s) => s.put(value, key)),
    get: (key) => open().then((db) => new Promise((resolve, reject) => {
      const request = db.transaction("kv").objectStore("kv").get(key);
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    })),
  };
})();
