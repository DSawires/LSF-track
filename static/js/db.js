/* IndexedDB: the write queue and the offline reference cache.
   A queue entry stops being sent ONLY when the server acknowledges its id, and
   is removed only once the item list this device holds has been fetched again
   since -- until then it is kept as "acked", so the cards go on showing the
   move it made (see `ack` below, and `sync()` in app.js).
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
      // A full disk fails the transaction at commit, which fires `abort` and
      // no `error`. Without this the promise never settles, and the one tap
      // that matters gets neither "Logged" nor "NOT saved".
      transaction.onabort = () => reject(transaction.error || new Error("transaction aborted"));
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
    // The server has it. Not deleted yet: the item list on this device was
    // fetched before the entry landed, and the cards are that list plus the
    // queue -- drop the entry now and the units jump back to where they were
    // until the next successful refresh, which in a dead spot is a long time,
    // and an engineer who sees that logs the move again. The first ack's time
    // is kept: the page and the service worker can both drain the same entry,
    // and a later "duplicate" must not make it look newer than the list.
    ack: (id) =>
      tx("queue", "readwrite", (s) => {
        const req = s.get(id);
        req.onsuccess = () => {
          const entry = req.result;
          if (!entry || entry._status === "acked") return;
          s.put({ ...entry, _status: "acked", _acked_at: Date.now(), _reason: undefined });
        };
      }),
    // Acknowledged entries the item list now reflects: every one acked before
    // the fetch that produced the list was started.
    dropAcked: (before) =>
      tx("queue", "readwrite", (s) => {
        const req = s.openCursor();
        req.onsuccess = () => {
          const cursor = req.result;
          if (!cursor) return;
          const entry = cursor.value;
          if (entry._status === "acked" && (entry._acked_at || 0) <= before) cursor.delete();
          cursor.continue();
        };
      }),
    markRejected: (id, reason) =>
      tx("queue", "readwrite", (s) => {
        const req = s.get(id);
        req.onsuccess = () => {
          if (req.result) s.put({ ...req.result, _status: "rejected", _reason: reason });
        };
      }),
    // Back to pending: the engineer's recovery path for a false rejection
    // (e.g. an over-advance whose upstream event synced from another device
    // after this one was refused).
    markPending: (id) =>
      tx("queue", "readwrite", (s) => {
        const req = s.get(id);
        req.onsuccess = () => {
          if (req.result) s.put({ ...req.result, _status: "pending", _reason: undefined });
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
