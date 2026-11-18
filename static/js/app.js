/* Life Style Track — the whole client.

   Ground rules, mirroring CLAUDE.md:
   - Logging never touches the network. Writes go to the IndexedDB queue, the UI
     confirms instantly, and a background drain syncs when it can.
   - Nothing in this file names a stage, station, state or event type by its code
     string. Everything renders from the reference payload and branches on flags.
   - The header pill always shows pending count and last sync, so an engineer can
     tell at a glance whether their entries have landed. */

"use strict";

/* ---------------------------------------------------------------- state -- */

const S = {
  user: null,
  ref: null,          // /api/reference payload
  items: [],          // /api/items payload rows
  itemsMeta: { token: null, at: 0 }, // which server state S.items is, and when fetched
  pending: [],        // queue snapshot for badges
  lastSync: null,
  online: navigator.onLine,
  needsLogin: false,
};

const $view = document.getElementById("view");
const $pill = document.getElementById("sync-pill");
const $pillText = document.getElementById("sync-text");
$pill.onclick = () => { if (!S.needsLogin || S.user) navigate("#/sync"); };

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ------------------------------------------------------------------ api -- */

async function apiResponse(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...options,
  });
  if (response.status === 401) {
    S.needsLogin = true;
    throw Object.assign(new Error("unauthorized"), { auth: true });
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw Object.assign(new Error("request failed"), { status: response.status, body });
  }
  return response;
}

async function api(path, options = {}) {
  return (await apiResponse(path, options)).json();
}

// A fetch() that never reached a server throws a bare TypeError; errors from
// apiResponse carry .status (HTTP) or .auth (401). The distinction decides
// retry-later versus quarantine.
const isNetworkError = (error) => !error.status && !error.auth;

/* ----------------------------------------------------------------- sync -- */

let syncRunning = false;
const BATCH_CHUNK = 100; // the server caps a batch at 500; stay well under it

// occurred_at is always toISOString() -- UTC, fixed width -- so it sorts as text.
const byOccurredAt = (a, b) =>
  (a.occurred_at < b.occurred_at ? -1 : a.occurred_at > b.occurred_at ? 1 : 0);

/* A GET that only counts if the server itself answered. The service worker
   serves /api/ GETs from its cache when the network is down, marked with
   X-LSF-From-Cache; that is what keeps the lists readable in a dead spot, and
   exactly what must NOT pass for a sync. Thrown without .status or .auth, so
   it reads as the network failure it is. */
async function apiLive(path) {
  const response = await apiResponse(path);
  if (response.headers.get("X-LSF-From-Cache")) throw new Error("served from cache");
  return response.json();
}

/* The item list is a replay of the whole log on the server, so it is fetched
   only when the reference payload's token says something it is drawn from has
   changed. The token cannot see time passing -- ages on the cards grow whether
   or not anyone logs -- so a copy older than this is refetched regardless. */
const ITEMS_MAX_AGE_MS = 10 * 60 * 1000;

async function drainQueue() {
  // Loop, because entries can be enqueued *while* a drain is in flight (the
  // auto-queue second event, or a fast pair of taps) and the syncRunning
  // guard swallows the sync() call they trigger. Bounded so a server that
  // rejects nothing but stores nothing can't spin us.
  for (let round = 0; round < 10; round++) {
    // Oldest first. IndexedDB hands the queue back in key order, and the keys
    // are random UUIDs: with more than one chunk waiting, an entry could be
    // sent a round before the earlier entry it depends on (the batch move
    // before the sample's finish) and be refused for it. The server orders
    // each batch itself, so only the chunk boundaries need this.
    const queue = (await LSF_DB.queueAll())
      .filter((q) => q._status === "pending")
      .sort(byOccurredAt);
    if (!queue.length) return;
    const events = queue
      .slice(0, BATCH_CHUNK)
      .map(({ _queued_at, _status, _reason, ...event }) => event);
    let results;
    try {
      ({ results } = await api("/api/events/batch", {
        method: "POST",
        body: JSON.stringify({ events }),
      }));
    } catch (error) {
      // Only a batch-level 4xx falls back to one-at-a-time posting: one
      // malformed entry fails request validation for all 100, and the poison
      // entry must quarantine alone instead of wedging the queue. Network
      // failures and 5xx are retried whole next sync.
      if (!(error.status >= 400 && error.status < 500)) throw error;
      results = await drainOneByOne(events);
    }
    let divergent = 0;
    let progressed = false;
    for (const result of results) {
      if (result.status === "stored" || result.status === "duplicate") {
        await LSF_DB.ack(result.id);
        progressed = true;
        if (result.divergent) divergent += 1;
      } else if (result.status === "rejected") {
        await LSF_DB.markRejected(result.id, result.reason || "rejected");
        progressed = true;
      }
      // status "error": a server-side failure on that entry. Leave it pending;
      // it retries next sync rather than being quarantined for a server bug.
    }
    if (divergent) {
      toast(`${divergent} ${divergent === 1 ? "entry" : "entries"} already existed with different details`);
    }
    if (!progressed) return; // all errors: stop looping, wait for the next sync
  }
}

async function drainOneByOne(events) {
  const results = [];
  for (const event of events) {
    try {
      const { divergent } = await api("/api/events", {
        method: "POST",
        body: JSON.stringify(event),
      });
      results.push({ id: event.id, status: "stored", divergent });
    } catch (error) {
      if (!(error.status >= 400 && error.status < 500)) throw error;
      results.push({ id: event.id, status: "rejected", reason: rejectReason(error) });
    }
  }
  return results;
}

function rejectReason(error) {
  const detail = error.body?.detail;
  if (typeof detail === "string") return detail;
  if (detail?.reason) return detail.reason; // EventRejected shape
  if (Array.isArray(detail) && detail.length) {
    // Pydantic validation error shape
    const first = detail[0];
    return `${(first.loc || []).slice(1).join(".")}: ${first.msg}`.replace(/^: /, "");
  }
  return "rejected by server";
}

async function sync() {
  if (syncRunning) return;
  syncRunning = true;
  try {
    try {
      await drainQueue();
    } catch (error) {
      if (error.auth) {
        // Never touch the queue on a 401: the entries outlive the session, and
        // render() keeps the app usable from cache until the engineer can sign
        // in again. Nothing here navigates away from the floor.
      } else if (isNetworkError(error)) {
        S.online = false; // network failed; queue stays, we try again later
      }
      // 5xx: the server is reachable but unwell; the queue stays and the pill
      // does not lie about being offline.
    }
    // Refresh the offline caches in their own try: a wedged drain must never
    // freeze reference data, and a failed refresh must never look like a
    // failed drain.
    try {
      S.ref = await apiLive("/api/reference");
      await LSF_DB.put("reference", S.ref);
      const current = !!S.itemsMeta.token
        && S.itemsMeta.token === S.ref.items_token
        && Date.now() - S.itemsMeta.at < ITEMS_MAX_AGE_MS;
      if (!current) {
        const itemsPayload = await apiLive("/api/items");
        S.items = itemsPayload.items;
        S.itemsMeta = { token: itemsPayload.token || null, at: Date.now() };
        await LSF_DB.put("items", S.items);
        await LSF_DB.put("itemsMeta", S.itemsMeta);
      }
      S.lastSync = Date.now();
      await LSF_DB.put("lastSync", S.lastSync);
      S.needsLogin = false;
      S.online = true;
    } catch (error) {
      if (isNetworkError(error)) S.online = false;
      // A 5xx is not "offline": the server is reachable, leave the flag alone.
    }
  } finally {
    syncRunning = false;
  }
  S.pending = await LSF_DB.queueAll();
  renderPill();
  render(true); // background refresh: skipped unless the data actually moved
}

/* The pill always shows the pending count AND the last sync together — an
   engineer with a queue wants to know exactly how long the phone has been out
   of contact, not one or the other. */
function renderPill() {
  const pending = S.pending.filter((q) => q._status === "pending").length;
  const rejected = S.pending.filter((q) => q._status === "rejected").length;
  $pill.classList.toggle("offline", !S.online || S.needsLogin);
  $pill.classList.toggle("pending", pending > 0 || rejected > 0);
  const parts = [];
  if (pending) parts.push(`${pending} pending`);
  if (rejected) parts.push(`${rejected} rejected`);
  if (!S.online) parts.push("offline");
  else if (S.needsLogin) parts.push("sign-in needed");
  parts.push(S.lastSync ? `synced ${timeAgo(S.lastSync)}` : "never synced");
  $pillText.textContent = parts.join(" · ");
  const banner = document.getElementById("session-banner");
  if (banner) banner.hidden = !(S.needsLogin && S.user);
}

/* The four the stylesheet paints. A UI palette, not factory vocabulary — the
   server takes the same four and nothing branches on which one it is. */
const BANNER_COLORS = ["green", "yellow", "red", "neutral"];

/* The admin-set banner, shown to everyone who is not an admin. It is drawn from
   the cached reference payload rather than fetched, so a notice raised while
   the phone had signal stays up after it loses it — a banner that disappears in
   a dead spot is worse than none.

   Colour is never the only signal: the label spells it out, for gloved hands
   holding a phone in bad light and for anyone who cannot tell the reds from the
   greens. */
function renderStatusBanner() {
  const el = document.getElementById("status-banner");
  if (!el) return;
  const banner = S.ref?.banner;
  const show = !!S.user && !S.user.is_admin && !!banner &&
    (banner.color !== "neutral" || !!banner.message);
  el.hidden = !show;
  if (!show) { el.className = ""; el.textContent = ""; return; }
  el.className = BANNER_COLORS.includes(banner.color) ? banner.color : "neutral";
  el.innerHTML = `<span class="label">${esc(banner.color)}</span>` +
    (banner.message ? ` ${esc(banner.message)}` : "");
}

function timeAgo(timestamp) {
  const minutes = Math.round((Date.now() - timestamp) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  return hours < 24 ? `${hours}h ago` : `${Math.round(hours / 24)}d ago`;
}

window.addEventListener("online", () => { S.online = true; sync(); });
window.addEventListener("offline", () => { S.online = false; renderPill(); });
document.addEventListener("visibilitychange", () => {
  // A phone coming out of a pocket shouldn't wait for the 30s tick.
  if (!document.hidden) sync();
});
setInterval(sync, 30000);
setInterval(renderPill, 60000);

/* -------------------------------------------------------------- logging -- */

function newId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  // Insecure origins don't get crypto.randomUUID; build a v4 by hand rather
  // than silently failing on the one tap that matters.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function requestBackgroundSync() {
  // Ask the browser to drain the queue even if the app is closed before
  // connectivity returns. The 30s interval remains the fallback.
  if ("serviceWorker" in navigator && "SyncManager" in window) {
    navigator.serviceWorker.ready
      .then((registration) => registration.sync.register("lsf-drain"))
      .catch(() => {});
  }
}

async function logEvent(event) {
  try {
    await LSF_DB.enqueue(event);
  } catch {
    // IndexedDB refused the write (quota, private browsing, corruption). The
    // one thing worse than an error is pretending the entry was recorded.
    toast("NOT saved — device storage failed");
    return false;
  }
  S.pending = await LSF_DB.queueAll();
  renderPill();
  toast("Logged ✓");
  requestBackgroundSync();
  sync(); // fire and forget; the queue survives if this fails
  return true;
}

function toast(message) {
  const el = document.createElement("div");
  el.className = "toast";
  el.setAttribute("role", "status"); // announced by screen readers
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; }, 1400);
  setTimeout(() => el.remove(), 1800);
}

/* ------------------------------------------------------- derived helpers -- */

function stageById(id) { return S.ref?.stages.find((s) => s.id === id); }
function eventType(id) { return (S.ref?.event_types || []).find((t) => t.id === id); }
function finishType() { return (S.ref?.event_types || []).find((t) => t.is_finish && t.is_active); }
const activeStages = () =>
  (S.ref?.stages || []).filter((s) => s.is_active).sort((x, y) => x.sort_order - y.sort_order);

/* Where an item's units can rest: not started, any stage in the factory, or
   Done. There is no sequence between stages and no route an item must follow --
   units move from any place to any stage, or to Done. A place is named by one
   of these two words or by a stage id, in URLs and in logSel alike.
   A unit is at a stage or it is not: there is no queued / in progress /
   completed inside a stage. */
const UNSTARTED = "unstarted";
const DONE = "done";

function placeName(key) {
  if (key === UNSTARTED) return "Not started";
  if (key === DONE) return "Done";
  return stageById(key)?.name || "?";
}

/* An item is project + code + area + phase: the same code can be several
   batches in one project, one per area and phase, each with its own quantity
   and history. So a code is never shown alone -- wherever an item is named, its
   area and phase are named with it. Both are optional. Each project keeps its
   own list of each, edited in the office; nothing here knows what any of them
   is called. */
const NONE = "none"; // filter value for "filed under no area / no phase"

function labelRow(list, id) { return (S.ref?.[list] || []).find((row) => row.id === id) || null; }
const areaName = (item) => labelRow("areas", item?.area_id)?.name || "";
const phaseName = (item) => labelRow("phases", item?.phase_id)?.name || "";

/* "LF-01 (Guest rooms, Phase 2)" -- for sentences, prompts and plain text. */
function itemName(item) {
  const tags = [areaName(item), phaseName(item)].filter(Boolean);
  return tags.length ? `${item.code} (${tags.join(", ")})` : item.code;
}

/* The same, as badges beside a code. */
function itemTags(item) {
  return [areaName(item), phaseName(item)].filter(Boolean)
    .map((name) => `<span class="badge tag">${esc(name)}</span>`).join(" ");
}

/* What an Area or Phase filter offers. Areas and phases belong to a project,
   so with a project chosen it is that project's list; with none chosen it is
   every live project's, each named with its project ("HOTEL-B · Spa"), because
   two jobs can both have a Spa. Either way: every row still on the list, plus
   any retired one that items on screen are still filed under -- and "none",
   when some items are filed under nothing, so those can be found too. */
function labelOptions(list, field, items, noneLabel, projectId) {
  const used = new Set(items.map((item) => item[field]).filter(Boolean));
  const projects = new Map((S.ref?.projects || []).map((pr) => [pr.id, pr]));
  const rows = (S.ref?.[list] || []).filter((row) =>
    (row.is_active || used.has(row.id))
    && (projectId ? row.project_id === projectId
      : used.has(row.id) || projects.get(row.project_id)?.is_active !== false));
  const named = (row) => (projectId ? row.name : `${projects.get(row.project_id)?.code || "?"} · ${row.name}`);
  const options = rows.map((row) => ({ id: row.id, name: named(row) }));
  if (!projectId) options.sort((x, y) => x.name.localeCompare(y.name));
  if (rows.length && items.some((item) => !item[field])) options.push({ id: NONE, name: noneLabel });
  return options;
}
const matchesLabel = (item, field, filter) =>
  !filter || (filter === NONE ? !item[field] : item[field] === filter);

/* Has anyone touched it yet? Read off where the units are, queued moves
   included -- a fact about the log, never something typed in.
     unstarted: every unit is still "not started"
     finished:  every unit is Done
     progress:  anything in between */
function progressOf(item) {
  const { places, done } = localPlaces(item);
  const started = done > 0 || places.some((p) => p.key !== UNSTARTED);
  if (!started) return "unstarted";
  return places.length ? "progress" : "finished";
}
const PROGRESS = [
  ["unstarted", "Haven't started"],
  ["progress", "In progress"],
  ["finished", "Finished"],
];

/* A remembered filter, dropped if what it points at is no longer on offer.

   Filters live in sessionStorage, so one set before a project was archived
   would otherwise keep filtering with no option in the select to show for it --
   an empty list and nothing on screen explaining why. */
function validFilter(key, options) {
  const stored = sessionStorage.getItem(key) || "";
  if (stored && !options.some((o) => o.id === stored)) {
    sessionStorage.removeItem(key);
    return "";
  }
  return stored;
}

/* The place a queued entry takes its units from, and the place it puts them.
   null for an entry in the old sequence shape (a step, no source): only the
   server can place those. */
function queuedSource(q) {
  return q.from_unstarted ? UNSTARTED : q.from_done ? DONE : q.from_stage_id || null;
}
function queuedTarget(q) {
  return eventType(q.event_type_id)?.is_finish ? DONE : q.stage_id || null;
}

/* Where an item's units are *as this phone knows it*: the server's last answer
   with every entry still in the queue applied on top, oldest first.

   This is what lets logging update the screen at once with no signal. An
   engineer who moves 40 pcs to paint in a dead spot sees 40 at paint on the
   card straight away, and can tap that batch to move it on -- the whole chain
   queues, and the server replays it in the same order when it syncs. A place
   that only exists because of queued entries has no age yet (`days_in_state`
   null) and is marked `queued`. */
function localPlaces(item) {
  const state = item.state;
  const at = new Map();
  const place = (key) => {
    if (!at.has(key)) {
      at.set(key, { key, qty: 0, sample_qty: 0, reworked_qty: 0, days_in_state: null, overdue: false, queued: false });
    }
    return at.get(key);
  };
  for (const p of state?.positions || []) {
    Object.assign(place(p.is_unstarted ? UNSTARTED : p.stage_id), {
      qty: p.qty,
      sample_qty: p.sample_qty || 0,
      reworked_qty: p.reworked_qty || 0,
      days_in_state: p.days_in_state,
      overdue: !!p.overdue,
    });
  }
  let done = state?.done_qty || 0;
  let inSample = state?.phase === "sample";
  const sampleQty = state?.sample_qty || 0;

  const queue = S.pending
    .filter((q) => q._status === "pending" && q.item_id === item.id)
    .sort(byOccurredAt);
  for (const q of queue) {
    const type = eventType(q.event_type_id);
    if (!type?.moves_quantity || !q.qty) continue;
    const from = queuedSource(q);
    const to = queuedTarget(q);
    if (!from || !to) continue;
    const source = from === DONE ? null : at.get(from);
    // Never more than is there: an over-quantity entry was warned about when
    // it was logged and the server will refuse it, so it must not invent units.
    const moved = Math.min(q.qty, from === DONE ? done : source?.qty || 0);
    if (!moved) continue;
    let movedSample = 0;
    if (from === DONE) {
      done -= moved;
    } else {
      source.qty -= moved;
      movedSample = Math.min(moved, source.sample_qty);
      source.sample_qty -= movedSample;
      source.queued = true;
    }
    if (to === DONE) {
      done += moved;
    } else {
      const target = place(to);
      target.qty += moved;
      target.sample_qty += movedSample;
      if (type.is_rework) target.reworked_qty += moved;
      target.queued = true;
    }
    // A sample finished offline releases the batch on the server the moment
    // it syncs; show it here already, so the batch can be started now.
    if (inSample && done >= sampleQty) {
      inSample = false;
      const unstarted = place(UNSTARTED);
      unstarted.qty += item.total_qty;
      unstarted.queued = true;
    }
  }

  const order = (key) => (key === UNSTARTED ? -1 : stageById(key)?.sort_order ?? 1e9);
  const places = [...at.values()]
    .filter((p) => p.qty > 0)
    .sort((x, y) => order(x.key) - order(y.key))
    .map((p) => ({ ...p, name: placeName(p.key) }));
  return { places, done, inSample };
}

function pendingCountFor(itemId) {
  return S.pending.filter((q) => q._status === "pending" && q.item_id === itemId).length;
}

/* "Carpentry", "Not started", with the sample called out: until it is done,
   the sample is the only thing on the floor for that item. */
function whereLabel(p) {
  return p.sample_qty ? `${p.name} · sample` : p.name;
}

/* -------------------------------------------------------------- routing -- */

function navigate(hash) { location.hash = hash; }

// Wrapped so the Event object is not mistaken for the `background` flag.
window.addEventListener("hashchange", () => render());

/* A background sync re-renders whatever view is open, and a re-render throws
   away everything the user has done to the DOM that is not in the payload:
   text typed, boxes ticked, panels opened, where they had scrolled to. Two
   defences, in order of value:

   1. Don't re-render at all unless the data actually changed (fingerprint).
   2. When it does change, carry the user's work across (snapshot/restore).

   Together they mean a 30-second tick on an idle floor is invisible, and a
   tick that does carry news costs the user nothing. */

/* A stable name for a form field across renders. Ids where they exist;
   otherwise the chain of data-* attributes down from the view, because the
   office forms key their fields by entity id rather than by id attribute --
   and a bare `data-dist-seq="10"` repeats once per item, so the ancestor
   `data-dist-for="<item id>"` is what makes it unique. */
function fieldKey(el) {
  if (el.id) return `#${el.id}`;
  const parts = [];
  for (let node = el; node && node !== $view; node = node.parentElement) {
    const data = node.getAttributeNames().filter((name) => name.startsWith("data-"));
    if (data.length) parts.unshift(data.map((n) => `${n}=${node.getAttribute(n)}`).join(","));
  }
  return parts.length ? parts.join("/") : null;
}

/* Fields the user has actually touched since the last deliberate render. A
   background sync must not clobber them with freshly-rendered defaults. */
const dirtyFields = new Set();
$view.addEventListener("input", (e) => {
  const key = fieldKey(e.target);
  if (key) dirtyFields.add(key);
});

let lastSnapshot = { fields: {}, panels: {}, dirty: new Set(), scrollTop: 0, background: false };

function snapshotView(background) {
  const fields = {};
  $view.querySelectorAll("input, select, textarea").forEach((el) => {
    const key = fieldKey(el);
    // A file input's value can only ever be set back to "", so leave it be.
    if (!key || el.type === "file") return;
    if (el.type === "checkbox" || el.type === "radio") fields[key] = { checked: el.checked };
    else if (el.value) fields[key] = { value: el.value };
  });
  const panels = {};
  $view.querySelectorAll("details[data-panel]").forEach((el) => { panels[el.dataset.panel] = el.open; });
  return { fields, panels, dirty: new Set(dirtyFields), scrollTop: $view.scrollTop, background };
}

function restoreView(snapshot) {
  $view.querySelectorAll("input, select, textarea").forEach((el) => {
    const key = fieldKey(el);
    const saved = key && el.type !== "file" ? snapshot.fields[key] : null;
    if (!saved) return;
    // Restore into empty fields (text inputs with no default), and into any
    // field the user had edited — even if the fresh render gave it a default,
    // like the qty box does. An untouched field takes the fresh value, so an
    // edit made on another phone still shows up.
    if ("checked" in saved) {
      if (snapshot.dirty.has(key)) el.checked = saved.checked;
    } else if (!el.value || snapshot.dirty.has(key)) {
      el.value = saved.value;
    }
  });
  // Which panels were open belongs to the user's session, not to the data —
  // but only a background refresh has to preserve it. Deliberate navigation
  // should land on a fresh view, the way it does today.
  if (!snapshot.background) return;
  $view.querySelectorAll("details[data-panel]").forEach((el) => {
    if (el.dataset.panel in snapshot.panels) el.open = snapshot.panels[el.dataset.panel];
  });
}

/* Everything the open view is drawn from, normalised so that values which tick
   on their own do not read as news: the server clock, and ages that are only
   ever printed to one decimal place. Equal fingerprints mean a re-render would
   produce the same DOM, so it is skipped — which is most 30-second ticks. */
function viewFingerprint(hash) {
  return JSON.stringify(
    [
      hash,
      S.user?.id,
      S.online,
      S.needsLogin,
      // The sync screen is the one view that prints a relative time.
      hash.startsWith("#/sync") ? S.lastSync : null,
      S.pending,
      S.ref,
      S.items,
    ],
    (key, value) => {
      if (key === "server_time") return undefined;
      if (key === "days_in_state") return Math.round(value * 10);
      return value;
    },
  );
}

let renderedFingerprint = null;

function render(background = false) {
  const hash = location.hash || "#/items";
  document.getElementById("topbar").hidden = false;
  document.getElementById("tabs").hidden = S.needsLogin && !S.user;
  // Before every early return below: the banner lives in the shell, not the
  // view, and must not depend on whether this particular render went ahead.
  renderStatusBanner();

  // Only force the login screen when there is no cached identity to work
  // with. A session expiring mid-shift must NOT lock an engineer out of an
  // offline-first app: they keep logging from cache (the banner explains),
  // and the queue drains after the next successful sign-in.
  if (!S.user && (S.needsLogin || !S.ref)) {
    if (hash !== "#/login") { navigate("#/login"); return; }
  }

  const [, route, arg, arg2] = hash.split("/");
  document.querySelectorAll("#tabs a").forEach((a) =>
    a.classList.toggle("active", a.dataset.tab === route));

  // Skip the refresh entirely while a field is focused: the next sync catches
  // up, and nothing is worth interrupting someone mid-keystroke for.
  const typing = $view.contains(document.activeElement) &&
    /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName);
  if (background && typing) return;

  // Nothing the view is built from has moved, so rebuilding it would only
  // destroy what the user is in the middle of. Checked after the focus guard
  // so a deferred tick still renders once the field is left.
  const fingerprint = viewFingerprint(hash);
  if (background && fingerprint === renderedFingerprint) return;

  if (!background) dirtyFields.clear();
  lastSnapshot = snapshotView(background);

  if (route === "login") viewLogin();
  else if (route === "sync") viewSyncStatus();
  else if (route === "items" && arg) viewLogScreen(arg, arg2);
  else if (route === "reports") viewReports();
  else if (route === "office") viewOffice(arg);
  else viewItems();

  restoreView(lastSnapshot);
  // Scroll is restored here and not in restoreView(), because the async lists
  // call that back much later — by which time the user may have scrolled and
  // would not thank us for putting them back.
  if (background && lastSnapshot.scrollTop) $view.scrollTop = lastSnapshot.scrollTop;
  renderedFingerprint = fingerprint;
}

/* ---------------------------------------------------------------- login -- */

function viewLogin() {
  document.getElementById("tabs").hidden = true;
  const pendingCount = S.pending.filter((q) => q._status === "pending").length;
  $view.innerHTML = `
    <div class="login-wrap">
      <h2 style="text-align:center;font-size:22px;margin-bottom:12px">Life Style Track</h2>
      <form class="card" id="login-form">
        <label for="login-user">Username</label>
        <input id="login-user" autocapitalize="none" autocomplete="username" enterkeyhint="next" required>
        <label for="login-pass">Password</label>
        <input id="login-pass" type="password" autocomplete="current-password" enterkeyhint="go" required>
        <div style="height:14px"></div>
        <button class="primary" id="login-go" type="submit">Sign in</button>
        <p id="login-err" class="warn-text" role="alert" hidden></p>
        ${pendingCount ? `<p class="muted" style="margin-top:10px">${pendingCount} queued ${pendingCount === 1 ? "entry is" : "entries are"} safe on this device and will sync after sign-in.</p>` : ""}
      </form>
    </div>`;
  document.getElementById("login-form").onsubmit = async (e) => {
    e.preventDefault();
    const err = document.getElementById("login-err");
    const button = document.getElementById("login-go");
    err.hidden = true;
    button.disabled = true;
    try {
      const body = JSON.stringify({
        username: document.getElementById("login-user").value.trim(),
        password: document.getElementById("login-pass").value,
      });
      const { user } = await api("/api/auth/login", { method: "POST", body });
      S.user = user;
      S.needsLogin = false;
      await LSF_DB.put("user", user);
      document.getElementById("tabs").hidden = false;
      navigate("#/items");
      sync();
    } catch (error) {
      // "Wrong password" when the real problem is no signal is an infuriating
      // dead end on the floor — name the actual failure.
      if (error.auth) err.textContent = "Wrong username or password.";
      else if (error.status === 429) err.textContent = error.body?.detail || "Too many attempts — wait a minute and try again.";
      else if (error.status) err.textContent = "The server had a problem — try again shortly.";
      else err.textContent = "No connection. Sign-in needs the network; queued entries are safe and will sync later.";
      err.hidden = false;
    } finally {
      button.disabled = false;
    }
  };
}

/* ---------------------------------------------------------- sync status -- */

/* Tapping the pill lands here: what exactly is pending, what was rejected and
   why, and the two honest actions for a reject — retry (the false-rejection
   path: the entry it depends on may have landed since) or discard. */
function viewSyncStatus() {
  const pending = S.pending.filter((q) => q._status === "pending");
  const rejected = S.pending.filter((q) => q._status === "rejected");

  const describe = (q) => {
    const item = S.items.find((i) => i.id === q.item_id);
    const type = eventType(q.event_type_id);
    const from = queuedSource(q);
    const to = queuedTarget(q);
    const what = [
      type?.name || "entry",
      from && to ? `${placeName(from)} → ${placeName(to)}` : to ? placeName(to) : "",
      q.qty ? `${q.qty} pcs` : "",
    ].filter(Boolean).join(" · ");
    return { code: item ? itemName(item) : "unknown item", what, when: new Date(q._queued_at || Date.now()).toLocaleString() };
  };

  const entryRow = (q, actions) => {
    const d = describe(q);
    return `
      <div style="padding:8px 0;border-bottom:1px solid var(--line)">
        <div class="spread">
          <strong>${esc(d.code)}</strong>
          <span class="muted">${esc(d.when)}</span>
        </div>
        <div class="muted">${esc(d.what)}</div>
        ${q._reason ? `<div class="warn-text" style="margin:4px 0">${esc(q._reason)}</div>` : ""}
        ${actions ? `
        <div style="display:flex;gap:8px;margin-top:6px">
          <button class="ghost" data-retry="${q.id}" style="width:auto;padding:8px 16px">Retry</button>
          <button class="ghost" data-discard="${q.id}" style="width:auto;padding:8px 16px">Discard</button>
        </div>` : ""}
      </div>`;
  };

  $view.innerHTML = `
    <div class="card">
      <div class="spread">
        <span>Signed in as <strong>${esc(S.user?.display_name || S.user?.username || "nobody")}</strong>${S.user?.is_admin ? ` <span class="badge">admin</span>` : ""}</span>
        ${S.user ? `<button class="ghost" id="sign-out" style="width:auto;padding:8px 16px;flex:none">Sign out</button>` : ""}
      </div>
    </div>

    <div class="card">
      <h2>Waiting to sync (${pending.length})</h2>
      ${pending.map((q) => entryRow(q, false)).join("") || `<p class="muted">Nothing waiting — everything has reached the server.</p>`}
      <p class="muted" style="margin-top:8px">${S.lastSync ? `Last synced ${timeAgo(S.lastSync)}.` : "Never synced from this device."}${S.online ? "" : " Currently offline; entries are safe here until the network returns."}</p>
    </div>

    ${rejected.length ? `
    <div class="card">
      <h2>Rejected by the server (${rejected.length})</h2>
      <p class="muted" style="margin-bottom:6px">These did not go in. Retry if the situation has changed (e.g. the entry it depends on has since synced); discard if the entry was a mistake.</p>
      ${rejected.map((q) => entryRow(q, true)).join("")}
    </div>` : ""}`;

  $view.querySelectorAll("[data-retry]").forEach((button) => {
    button.onclick = async () => {
      await LSF_DB.markPending(button.dataset.retry);
      S.pending = await LSF_DB.queueAll();
      renderPill();
      render();
      sync();
    };
  });
  $view.querySelectorAll("[data-discard]").forEach((button) => {
    button.onclick = async () => {
      if (!confirm("Discard this entry? It was never stored on the server and will be gone for good.")) return;
      await LSF_DB.dropRejected(button.dataset.discard);
      S.pending = await LSF_DB.queueAll();
      renderPill();
      render();
    };
  });

  const signOut = document.getElementById("sign-out");
  if (signOut) signOut.onclick = async () => {
    const waiting = pending.length;
    if (waiting && !confirm(`${waiting} ${waiting === 1 ? "entry has" : "entries have"} not synced yet. ${waiting === 1 ? "It stays" : "They stay"} on this device and will sync after the next sign-in. Sign out anyway?`)) return;
    try {
      await api("/api/auth/logout", { method: "POST" });
    } catch {
      // Offline: the server cookie outlives this, but the local identity and
      // cached data still clear so the next user starts clean.
    }
    S.user = null;
    S.needsLogin = true;
    await LSF_DB.put("user", null);
    // The next user of a shared phone must not see this user's cached reports.
    navigator.serviceWorker?.controller?.postMessage("purge-data");
    renderPill();
    navigate("#/login");
  };
}

/* ---------------------------------------------------------------- items -- */

function viewItems() {
  // Back on the list: the next batch tap is a fresh arrival, even if it is
  // the same batch as last time.
  logSel._arrival = null;
  const projects = (S.ref?.projects || []).filter((p) => p.is_active !== false);
  const stages = activeStages();
  // A project archived since the filter was set would otherwise go on filtering
  // invisibly: its option is gone from the select, but the stored id still hides
  // every other item.
  const filterProject = validFilter("f-project", projects);
  const filterStage = sessionStorage.getItem("f-stage") || "";
  const inProject = S.items.filter((item) => !filterProject || item.project_id === filterProject);
  const areas = labelOptions("areas", "area_id", inProject, "No area", filterProject);
  const phases = labelOptions("phases", "phase_id", inProject, "No phase", filterProject);
  const filterArea = validFilter("f-area", areas);
  const filterPhase = validFilter("f-phase", phases);
  const filterProgress = validFilter("f-progress", PROGRESS.map(([id]) => ({ id })));

  const matches = (item, text) => {
    if (filterProject && item.project_id !== filterProject) return false;
    if (!matchesLabel(item, "area_id", filterArea)) return false;
    if (!matchesLabel(item, "phase_id", filterPhase)) return false;
    if (filterProgress && progressOf(item) !== filterProgress) return false;
    // "At paint" means some of it rests at paint right now, queued moves included.
    if (filterStage && !localPlaces(item).places.some((p) => p.key === filterStage)) return false;
    return !text || `${itemName(item)} ${item.description}`.toLowerCase().includes(text);
  };
  const list = (text) =>
    S.items.filter((item) => matches(item, text)).map(itemRow).join("")
    || `<p class="muted">No items match.</p>`;

  $view.innerHTML = `
    <input id="f-q" type="search" placeholder="Search code or description"
           value="${esc(sessionStorage.getItem("f-q") || "")}" style="margin-bottom:8px">
    <div class="filters">
      <select id="f-project">
        <option value="">All projects</option>
        ${projects.map((p) => `<option value="${p.id}" ${p.id === filterProject ? "selected" : ""}>${esc(p.code)}</option>`).join("")}
      </select>
      <select id="f-stage">
        <option value="">All stages</option>
        ${stages.map((s) => `<option value="${s.id}" ${s.id === filterStage ? "selected" : ""}>${esc(s.name)}</option>`).join("")}
      </select>
      ${areas.length ? `
      <select id="f-area" aria-label="Area">
        <option value="">All areas</option>
        ${areas.map((a) => `<option value="${a.id}" ${a.id === filterArea ? "selected" : ""}>${esc(a.name)}</option>`).join("")}
      </select>` : ""}
      ${phases.length ? `
      <select id="f-phase" aria-label="Phase">
        <option value="">All phases</option>
        ${phases.map((ph) => `<option value="${ph.id}" ${ph.id === filterPhase ? "selected" : ""}>${esc(ph.name)}</option>`).join("")}
      </select>` : ""}
      <select id="f-progress" aria-label="Progress">
        <option value="">Any progress</option>
        ${PROGRESS.map(([id, name]) => `<option value="${id}" ${id === filterProgress ? "selected" : ""}>${name}</option>`).join("")}
      </select>
    </div>
    <div class="row-list">
      ${list((sessionStorage.getItem("f-q") || "").toLowerCase())}
    </div>`;

  document.getElementById("f-project").onchange = (e) => {
    sessionStorage.setItem("f-project", e.target.value); render();
  };
  document.getElementById("f-stage").onchange = (e) => {
    sessionStorage.setItem("f-stage", e.target.value); render();
  };
  for (const id of ["f-area", "f-phase", "f-progress"]) {
    const select = document.getElementById(id);
    if (select) select.onchange = (e) => { sessionStorage.setItem(id, e.target.value); render(); };
  }
  // Filter the list live but don't re-render around the keyboard; the input
  // keeps focus and the rows below it re-draw.
  document.getElementById("f-q").oninput = (e) => {
    sessionStorage.setItem("f-q", e.target.value);
    const rows = document.querySelector(".row-list");
    if (rows) rows.innerHTML = list(e.target.value.toLowerCase());
  };
}

function itemRow(item) {
  // One line per place holding units, mirroring the aging report -- and each
  // line is its own tap target: tapping the 40 pcs at carpentry opens the log
  // screen aimed at moving THOSE 40, independent of the 50 sitting at paint.
  // This is the whole workflow: tap a batch, say where it went.
  const { places, done, inSample } = localPlaces(item);
  const pending = pendingCountFor(item.id);

  const lines = places.map((p) => `
    <a class="pos-row" href="#/items/${item.id}/${p.key}"
       aria-label="Move ${p.qty} pcs from ${esc(whereLabel(p))}">
      <span class="muted" style="min-width:0">${esc(whereLabel(p))}${p.reworked_qty ? ` <span class="badge rework">R</span>` : ""}</span>
      <span style="white-space:nowrap;display:flex;align-items:center;gap:6px">
        <span class="badge qty">${p.qty}</span>${p.key === UNSTARTED ? "" : p.days_in_state == null ? `
        <span class="badge pending">new</span>` : `
        <span class="badge${p.overdue ? " age-hot" : ""}">${p.days_in_state.toFixed(1)}d</span>`}
        <span class="chev" aria-hidden="true">›</span>
      </span>
    </a>`).join("");

  return `
    <div>
      <a class="item-head" href="#/items/${item.id}">
        <div style="display:flex;gap:10px;align-items:flex-start">
          ${item.icon_url ? `<img class="item-icon" src="${item.icon_url}" alt="" loading="lazy">` : ""}
          <div style="flex:1;min-width:0">
            <div class="spread">
              <strong>${esc(item.code)}</strong>
              <span class="muted">${item.total_qty} pcs · rev ${esc(item.drawing_revision)}</span>
            </div>
            ${itemTags(item) ? `<div style="margin-top:3px">${itemTags(item)}</div>` : ""}
            <div class="muted" style="margin:2px 0 6px">${esc(item.description)}</div>
            ${inSample ? `<div style="margin-bottom:6px"><span class="badge sample">Sample</span>
              <span class="muted">batch of ${item.total_qty} follows once the sample is done</span></div>` : ""}
          </div>
        </div>
      </a>
      ${lines}
      ${done ? `
      <a class="pos-row" href="#/items/${item.id}/${DONE}" style="border-left:2px solid var(--good)"
         aria-label="${done} pcs done">
        <span class="muted">Done</span>
        <span style="white-space:nowrap;display:flex;align-items:center;gap:6px">
          <span class="badge qty" style="color:var(--good)">${done}</span>
          <span class="chev" aria-hidden="true">›</span>
        </span>
      </a>` : ""}
      ${pending ? `<div style="margin-top:4px"><span class="badge pending">${pending} pending</span></div>` : ""}
    </div>`;
}

/* ----------------------------------------------------------- log screen -- */

/* What the form is set to. `from` and `to` are places: "unstarted", "done", or
   a stage id. `qty` is what the engineer typed, or null for "all of it". */
const logSel = {
  from: null, to: null, typeId: null, stationId: null, reasonId: null,
  qty: null, note: "", _arrival: null,
};

/* Where units last went from a given place, on this phone. There is no
   sequence to say what comes after carpentry, but the floor has habits: the
   stage picked last time is offered first next time, so the usual move is
   still two taps. Never applied without being shown -- it is only which
   button starts out selected. */
const usualDestination = (from) => localStorage.getItem(`next:${from}`);

function viewLogScreen(itemId, fromKey) {
  const item = S.items.find((i) => i.id === itemId);
  if (!item) { navigate("#/items"); return; }

  const { places, done, inSample } = localPlaces(item);
  const finish = finishType();
  // Finishing is not a type the engineer picks: it is what "Done" writes.
  const types = (S.ref.event_types || []).filter(
    (t) => t.is_active && t.moves_quantity && !t.is_finish);
  const plainType = types.find((t) => !t.is_rework) || types[0];
  const reworkType = types.find((t) => t.is_rework);

  // Every place holding units is somewhere a move can start. Done too, but
  // only as rework: a finished piece coming back has a reason.
  const sources = places.map((p) => ({ key: p.key, label: whereLabel(p), qty: p.qty }));
  if (done > 0 && reworkType) sources.push({ key: DONE, label: "Done", qty: done });

  // Arrived by tapping a batch on the item card: aim the form at moving THAT
  // batch. Applied once per arrival, so segment taps and background
  // re-renders don't fight the engineer's own adjustments afterwards.
  const arrival = `${itemId}/${fromKey || ""}`;
  if (logSel._arrival !== arrival) {
    logSel._arrival = arrival;
    logSel.from = sources.some((s) => s.key === fromKey) ? fromKey : null;
    logSel.to = null;
    logSel.typeId = plainType?.id || null;
    logSel.stationId = null;
    logSel.qty = null;
  }
  if (!sources.some((s) => s.key === logSel.from)) logSel.from = sources[0]?.key || null;
  const source = sources.find((s) => s.key === logSel.from) || null;

  if (logSel.from === DONE) logSel.typeId = reworkType.id;
  if (!types.some((t) => t.id === logSel.typeId)) logSel.typeId = plainType?.id || null;
  const type = types.find((t) => t.id === logSel.typeId);
  const shownTypes = logSel.from === DONE ? [reworkType] : types;

  // Anywhere: every stage in the factory, and Done. The stage the units are
  // already at is only offered when it has stations -- then it is a move to
  // another booth, not a move to nowhere.
  const targets = activeStages()
    .filter((st) => st.id !== logSel.from || st.requires_station)
    .map((st) => ({ key: st.id, label: st.name }));
  if (finish && !type?.is_rework && logSel.from !== DONE) targets.push({ key: DONE, label: "Done ✓" });
  if (!targets.some((t) => t.key === logSel.to)) {
    const usual = logSel.from ? usualDestination(logSel.from) : null;
    logSel.to = targets.some((t) => t.key === usual) ? usual : null;
  }
  const toDone = logSel.to === DONE;
  const stage = !toDone && logSel.to ? stageById(logSel.to) : null;
  const sameStage = !!stage && logSel.to === logSel.from;

  const stations = stage
    ? S.ref.stations.filter((st) => st.stage_id === stage.id && st.is_active)
    : [];
  if (stage?.requires_station && !stations.some((st) => st.id === logSel.stationId)) {
    logSel.stationId = localStorage.getItem(`station:${stage.id}`) || stations[0]?.id || null;
  }
  const available = source?.qty || 0;
  const reasons = (S.ref.reason_codes || []).filter((r) => r.is_active);
  // Keep the tracked selection valid: a re-render must not silently reset the
  // reason to the first option, and an empty reason list must never turn into
  // an empty-string id in the payload.
  if (type?.requires_reason_code && !reasons.some((r) => r.id === logSel.reasonId)) {
    logSel.reasonId = reasons[0]?.id || null;
  }
  const sampleQty = item.state?.sample_qty || 0;
  const seg = (id, label, options, picked) => `
      <label id="lbl-${id}">${label}</label>
      <div class="seg" id="seg-${id}" role="group" aria-labelledby="lbl-${id}">
        ${options.map((o) => `<button data-id="${o.key}" class="${o.key === picked ? "on" : ""}" aria-pressed="${o.key === picked}">${esc(o.label)}</button>`).join("")}
      </div>`;

  $view.innerHTML = `
    <div class="card">
      <div style="display:flex;gap:12px;align-items:center">
        ${item.icon_url ? `<img class="item-icon" src="${item.icon_url}" alt="">` : ""}
        <div style="flex:1;min-width:0">
          <div class="spread">
            <h2>${esc(item.code)}</h2>
            <span class="muted">${item.total_qty} pcs · rev ${esc(item.drawing_revision)}</span>
          </div>
          ${itemTags(item) ? `<div style="margin:2px 0">${itemTags(item)}</div>` : ""}
          <div class="muted">${esc(item.description)}</div>
        </div>
      </div>
      ${inSample ? `
      <p style="margin-top:8px"><span class="badge sample">Sample</span>
        <span class="muted">Only the sample (${sampleQty} pc${sampleQty === 1 ? "" : "s"}) is on the floor.
        Log it to Done and the batch of ${item.total_qty} is released.</span></p>` : ""}
    </div>

    <div class="card">
      ${!source ? `
      <p class="muted">Nothing here to move — every unit of ${esc(item.code)} is Done.</p>` : `
      ${seg("from", "From", sources.map((s) => ({ key: s.key, label: `${s.label} · ${s.qty}` })), logSel.from)}

      ${shownTypes.length > 1 ? seg("type", "Entry type", shownTypes.map((t) => ({ key: t.id, label: t.name })), logSel.typeId) : ""}
      ${logSel.from === DONE ? `<p class="muted" style="margin-top:6px">Taking a finished piece back out of Done is rework, and needs its reason.</p>` : ""}

      ${seg("to", type?.is_rework ? "Send back to" : "Now at", targets, logSel.to)}

      ${stage?.requires_station ? seg("station", "Station", stations.map((st) => ({ key: st.id, label: st.name })), logSel.stationId) : ""}

      ${type?.requires_reason_code ? `
      <label for="sel-reason">Reason</label>
      <select id="sel-reason">
        ${reasons.map((r) => `<option value="${r.id}" ${r.id === logSel.reasonId ? "selected" : ""}>${esc(r.name)}</option>`).join("")}
      </select>` : ""}

      <label for="qty">Quantity</label>
      <div class="qty-row">
        <button id="qty-minus" aria-label="One fewer">−</button>
        <input id="qty" type="number" inputmode="numeric" min="1"
               value="${esc(logSel.qty ?? (available || 1))}">
        <button id="qty-plus" aria-label="One more">+</button>
      </div>
      <p class="muted" style="margin-top:5px">${available} at ${esc(source.label)}</p>
      ${sameStage ? `<p class="muted">Same stage — this moves them to another station. Their age keeps counting.</p>` : ""}
      ${stage?.requires_external_po ? `<p class="muted">External supplier stage — time in state is supplier lead time.</p>` : ""}
      ${stage && !stage.allows_partial_qty ? `<p class="muted">Whole-lot stage — the move must take everything from where it comes.</p>` : ""}
      <p class="warn-text" id="qty-warn" hidden>More than is resting there — the server will reject this entry until the log shows that many. It will wait under the sync pill with a Retry button.</p>

      <details data-panel="log-note" ${logSel.note ? "open" : ""} style="margin-top:12px">
        <summary class="muted" style="cursor:pointer">Add note${logSel.note ? " ·" : ""}</summary>
        <input id="log-note" maxlength="2000" placeholder="e.g. rack 3, waiting on fittings"
               value="${esc(logSel.note)}" style="margin-top:6px">
      </details>

      <div style="height:12px" aria-hidden="true"></div>
      <div class="action-sticky">
        <button class="primary" id="log-go" ${logSel.to ? "" : "disabled"}>${logSel.to ? "Log entry" : "Pick where it went"}</button>
      </div>`}
    </div>

    <div class="card">
      <h2>Photos <span class="muted" id="photo-count"></span></h2>
      <div id="photos" class="muted">Loading…</div>
      <div style="height:8px"></div>
      <div id="photo-progress" hidden>
        <div class="bar-track" id="photo-track"><div class="bar-fill" id="photo-bar" style="width:0"></div></div>
        <p id="photo-status" class="muted" style="margin:4px 0 8px" role="status" aria-live="polite"></p>
      </div>
      <input id="photo-note" maxlength="255" placeholder="Describe the snag (optional)"
             style="margin-bottom:8px" ${S.online ? "" : "disabled"}>
      ${photoPicker("snag", { camera: "Take snag photo", gallery: "From gallery", single: "Add snag photo" })}
    </div>

    <div class="card">
      <h2>Recent entries</h2>
      <div id="recent" class="muted">Loading…</div>
    </div>`;

  const rerender = () => viewLogScreen(itemId, fromKey);
  wireSeg("seg-from", (key) => {
    // A different batch: where it goes and how many start over.
    Object.assign(logSel, { from: key, to: null, stationId: null, qty: null });
    rerender();
  });
  wireSeg("seg-type", (id) => { logSel.typeId = id; rerender(); });
  wireSeg("seg-to", (key) => { logSel.to = key; logSel.stationId = null; rerender(); });
  wireSeg("seg-station", (id) => {
    logSel.stationId = id;
    if (stage) localStorage.setItem(`station:${stage.id}`, id);
    rerender();
  });
  const $reason = document.getElementById("sel-reason");
  if ($reason) $reason.onchange = (e) => { logSel.reasonId = e.target.value || null; };
  // Tracked in logSel like every other selection, so a segment tap's re-render
  // cannot eat a half-typed note -- or a quantity.
  const $note = document.getElementById("log-note");
  if ($note) $note.oninput = (e) => { logSel.note = e.target.value; };

  const $qty = source ? document.getElementById("qty") : null;
  if ($qty) {
    const warn = () => {
      logSel.qty = $qty.value;
      document.getElementById("qty-warn").hidden = Number($qty.value) <= available;
    };
    $qty.oninput = warn;
    document.getElementById("qty-minus").onclick = () => { $qty.value = Math.max(1, Number($qty.value) - 1); warn(); };
    document.getElementById("qty-plus").onclick = () => { $qty.value = Number($qty.value) + 1; warn(); };
    document.getElementById("qty-warn").hidden = Number($qty.value) <= available;

    document.getElementById("log-go").onclick = async () => {
      if (!logSel.to) return;
      // A whole number of pieces, at least one. Anything else used to be
      // queued as typed and only refused by the server at the next sync -- or,
      // for an empty box, quietly logged as 1.
      const qty = Number($qty.value);
      if (!Number.isInteger(qty) || qty < 1) {
        toast("Enter a whole quantity of 1 or more");
        $qty.focus();
        return;
      }
      const logged = await logEvent({
        id: newId(),
        item_id: item.id,
        // Where the units are now. Done is not a stage: a finish entry names
        // none, and no station.
        stage_id: toDone ? null : logSel.to,
        station_id: !toDone && stage?.requires_station ? logSel.stationId : null,
        event_type_id: toDone ? finish.id : logSel.typeId,
        qty,
        // Where they came from. Always said: with no sequence, the entry
        // itself is the only record of the path the units took.
        from_stage_id: logSel.from !== UNSTARTED && logSel.from !== DONE ? logSel.from : null,
        from_unstarted: logSel.from === UNSTARTED,
        from_done: logSel.from === DONE,
        reason_code_id: type?.requires_reason_code ? logSel.reasonId : null,
        occurred_at: new Date().toISOString(),
        note: logSel.note.trim() || null,
        supersedes_event_id: null,
        user_id: S.user?.id || null,
      });
      if (!logged) return;
      // Remember the habit, not rework: sending work back is the exception.
      if (!type?.is_rework) localStorage.setItem(`next:${logSel.from}`, logSel.to);
      Object.assign(logSel, { note: "", qty: null, _arrival: null });
      if (fromKey) {
        // The batch has moved on; drop its key from the URL without a reload,
        // so going Back here later lands on the item rather than on a place it
        // has already left.
        history.replaceState(null, "", `#/items/${item.id}`);
        fromKey = undefined;
      }
      // The entry is in. Close the screen and go back to the list, rather than
      // sitting on a form that still looks armed: an engineer who is not sure
      // the tap registered taps again, and a screen that stays put invites
      // exactly that. The list shows the move at once -- the units are where
      // the entry put them, with the pending count in the header.
      dirtyFields.clear(); // nothing here should follow the user to the list
      // Re-opening this item should show what was just logged, not a
      // 30-second-old snapshot of the entries before it.
      logScreenCache.fetchedAt = 0;
      navigate("#/items");
    };
  }

  // Both halves of the picker land here; whichever one the user opened, the
  // upload is the same.
  const wireUpload = (key, kind, doneMessage, noteInputId) => {
    document.querySelectorAll(`[data-photo="${key}"]`).forEach((input) => {
      input.onchange = async () => {
        const file = input.files[0];
        if (!file) return;
        if (photoStatus.busy) { toast("A photo is already uploading."); return; }
        const noteInput = noteInputId ? document.getElementById(noteInputId) : null;
        photoStatus.start();
        try {
          await uploadImage(item.id, file, {
            kind,
            note: noteInput?.value.trim() || "",
            onProgress: photoStatus.set,
          });
          photoStatus.done();
          toast(doneMessage);
          if (noteInput) noteInput.value = ""; // kept on failure, so a retry has it
          logScreenCache.fetchedAt = 0; // the photo list just changed
          loadPhotos(item);
        } catch (error) {
          photoStatus.fail(error.detail);
        } finally {
          clearPicker(key);
        }
      };
    });
  };
  // Snags only. The item's icon is set in its editor: Office → Items.
  wireUpload("snag", "snag", "Snag photo added", "photo-note");

  loadRecent(item);
  loadPhotos(item);
}

function wireSeg(id, onPick) {
  const seg = document.getElementById(id);
  if (!seg) return;
  seg.querySelectorAll("button").forEach((b) => {
    b.onclick = () => onPick(b.dataset.id);
  });
}

/* Segment taps re-render the whole log screen; without this, every tap
   re-fires the photos and recent-entries requests over the patchy link the
   app is designed around. Cached per item for a short window. */
const logScreenCache = { itemId: null, images: null, events: null, fetchedAt: 0 };
const LOG_CACHE_MS = 30000;

function cachedFor(item, key) {
  const fresh = logScreenCache.itemId === item.id
    && Date.now() - logScreenCache.fetchedAt < LOG_CACHE_MS;
  return fresh ? logScreenCache[key] : null;
}

function cacheSet(item, key, value) {
  if (logScreenCache.itemId !== item.id) {
    logScreenCache.itemId = item.id;
    logScreenCache.images = null;
    logScreenCache.events = null;
  }
  logScreenCache[key] = value;
  logScreenCache.fetchedAt = Date.now();
}

/* ---------------------------------------------------------------- photos -- */

/* Camera or gallery, asked before the sheet opens.

   `capture="environment"` does not *suggest* the camera, it replaces the
   picker with it — no gallery, no way back. That is wrong about half the time:
   a snag is usually shot on the spot, but an icon is usually a render or a
   photo already in the roll, and an engineer who took the shot before opening
   the app had no route to it at all.

   Two inputs rather than a dialog: a dialog is a second tap and a second thing
   to read, and this app is used one-handed in gloves. On a desktop there is
   only ever one button — `capture` is ignored by a mouse-and-keyboard browser,
   so "Take photo" there would open the same file dialog and mean nothing. */
const hasCamera = () => window.matchMedia?.("(pointer: coarse)").matches ?? false;

function photoPicker(key, { camera = "Take photo", gallery = "From gallery", single = "Add photo", offline = "Photos need a connection" } = {}) {
  const box = (label, capture) => `
    <label class="photo-pick${S.online ? "" : " off"}">
      ${esc(label)}
      <input type="file" data-photo="${key}" accept="image/*" ${capture ? `capture="environment"` : ""}
             hidden ${S.online ? "" : "disabled"}>
    </label>`;
  if (!S.online) return `<div class="photo-picks">${box(offline, false)}</div>`;
  return `<div class="photo-picks">${
    hasCamera() ? box(camera, true) + box(gallery, false) : box(single, false)
  }</div>`;
}

function clearPicker(key) {
  document.querySelectorAll(`[data-photo="${key}"]`).forEach((input) => { input.value = ""; });
}

/* Photo upload is the one request in the app big enough to watch go by: a
   camera file over factory Wi-Fi is megabytes and seconds, and an unmarked
   wait reads as a freeze. fetch() cannot report how far a body has got, so
   this is the one XMLHttpRequest in the codebase.

   Rejects with .detail set to the server's own words — "not a recognised image
   format", "image exceeds 10MB" — because "Upload failed" leaves an engineer
   with a photo, a snag, and nothing to do about either. */
function uploadImage(itemId, file, { kind = "snag", note = "", onProgress } = {}) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("file", file);
    form.append("kind", kind);
    form.append("note", note);

    const fail = (detail) => reject(Object.assign(new Error("upload failed"), { detail }));
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/items/${itemId}/images`);
    xhr.withCredentials = true;
    xhr.timeout = 120000; // a dead spot must not leave the bar stuck forever
    xhr.upload.onprogress = (e) => {
      if (onProgress && e.lengthComputable) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      let body = {};
      try { body = JSON.parse(xhr.responseText); } catch { /* not JSON; fall through */ }
      if (xhr.status >= 200 && xhr.status < 300) { resolve(body); return; }
      if (xhr.status === 401) {
        S.needsLogin = true;
        fail("Session expired — sign in again, then add the photo.");
      } else {
        fail(body.detail ? rejectReason({ body }) : `The server refused it (HTTP ${xhr.status}).`);
      }
    };
    xhr.onerror = () => fail("No connection — the photo was not sent. Try again in range.");
    xhr.ontimeout = () => fail("Timed out — the connection dropped mid-upload. Try again.");
    xhr.onabort = () => fail("Upload cancelled.");
    xhr.send(form);
  });
}

/* Failures stay on screen rather than passing by in a toast: a message that
   vanishes is how "it sometimes just doesn't work" gets started. */
const photoStatus = {
  busy: false,
  start() {
    photoStatus.busy = true;
    const box = document.getElementById("photo-progress");
    if (!box) return;
    box.hidden = false;
    document.getElementById("photo-track").hidden = false;
    photoStatus.set(0);
  },
  set(fraction) {
    const bar = document.getElementById("photo-bar");
    if (!bar) return;
    const percent = Math.round(fraction * 100);
    bar.style.width = `${percent}%`;
    const text = document.getElementById("photo-status");
    text.className = "muted";
    // The bytes are up but the server still has to sniff, cap and store them.
    text.textContent = percent < 100 ? `Sending photo… ${percent}%` : "Saving on the server…";
  },
  done() {
    photoStatus.busy = false;
    const box = document.getElementById("photo-progress");
    if (box) box.hidden = true;
  },
  fail(reason) {
    photoStatus.busy = false;
    const box = document.getElementById("photo-progress");
    if (!box) return;
    box.hidden = false;
    document.getElementById("photo-track").hidden = true;
    const text = document.getElementById("photo-status");
    text.className = "warn-text";
    text.textContent = reason || "Upload failed.";
  },
};

async function loadPhotos(item) {
  const target = document.getElementById("photos");
  if (!target) return;
  try {
    const cached = cachedFor(item, "images");
    const { images } = cached ? { images: cached } : await api(`/api/items/${item.id}/images`);
    cacheSet(item, "images", images);
    if (!target.isConnected) return;
    document.getElementById("photo-count").textContent = images.length ? `(${images.length})` : "";
    target.innerHTML = images.length
      ? `<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px">
          ${images.map((img) => `
            <div>
              <a href="${img.url}" target="_blank" rel="noopener" style="position:relative;display:block">
                <img src="${img.url}" alt="${esc(img.note || img.filename)}" loading="lazy"
                     style="width:100%;aspect-ratio:1;object-fit:cover;border-radius:8px;border:1px solid var(--line)">
                ${img.kind === "icon" ? `<span class="badge" style="position:absolute;top:4px;left:4px;background:rgba(0,0,0,.55)">icon</span>` : ""}
              </a>
              <button class="ghost" data-note-for="${img.id}" ${S.online ? "" : "disabled"}
                      style="width:100%;margin-top:4px;padding:6px 4px;min-height:36px;font-size:12px;text-align:left;overflow:hidden;overflow-wrap:anywhere"
                      title="${esc(img.note || "")}">${img.note ? esc(img.note) : "+ note"}</button>
            </div>`).join("")}
         </div>`
      : `<span class="muted">No photos yet.</span>`;

    // The note is written after the shutter: on the floor you photograph the
    // snag first and find the words for it second.
    target.querySelectorAll("[data-note-for]").forEach((button) => {
      const image = images.find((img) => img.id === button.dataset.noteFor);
      button.onclick = async () => {
        const note = prompt("What is the snag?", image.note || "");
        if (note === null) return;
        try {
          await api(`/api/images/${image.id}`, {
            method: "PATCH", body: JSON.stringify({ note: note.trim() }),
          });
          toast(note.trim() ? "Note saved" : "Note cleared");
          logScreenCache.fetchedAt = 0;
          loadPhotos(item);
        } catch (error) {
          alert(error.body?.detail || "Could not save the note.");
        }
      };
    });
  } catch {
    if (target.isConnected) target.textContent = "Offline — photos unavailable.";
  }
}

async function loadRecent(item) {
  const target = document.getElementById("recent");
  const correctionType = (S.ref.event_types || []).find((t) => t.is_correction && t.is_active);
  try {
    const cached = cachedFor(item, "events");
    const { events } = cached ? { events: cached } : await api(`/api/items/${item.id}/events?limit=8`);
    cacheSet(item, "events", events);
    if (!target.isConnected) return;
    // A void still waiting in the queue counts: the entry is as good as voided
    // on this phone, and offering to void it again would log a second
    // correction of the same event.
    const queuedVoids = new Set(S.pending
      .filter((q) => q._status === "pending" && q.supersedes_event_id)
      .map((q) => q.supersedes_event_id));
    target.innerHTML = events.map((e) => {
      const type = eventType(e.event_type_id);
      // The server resolves a sequence-era row's step to its stage, so every
      // entry reads the same way here. Those rows have no "from".
      const to = type?.is_finish ? "Done" : e.to_stage_id ? placeName(e.to_stage_id) : "";
      const from = e.from_unstarted ? "Not started" : e.from_done ? "Done"
        : e.from_stage_id ? placeName(e.from_stage_id) : "";
      const label = [
        type?.name || "event",
        from && to ? `${from} → ${to}` : to,
        e.qty ? `${e.qty} pcs` : "",
        e.state_name || "", // rows logged before states were removed
      ].filter(Boolean).join(" · ");
      const voided = !!e.superseded || queuedVoids.has(e.id);
      const canVoid = correctionType && type?.moves_quantity && !voided;
      return `
        <div class="spread" style="padding:6px 0;border-bottom:1px solid var(--line)">
          <span><span${voided ? ` style="text-decoration:line-through;opacity:.6"` : ""}>${esc(label)}</span>${voided ? ` <span class="badge">voided</span>` : ""}<br><span class="muted">${new Date(e.occurred_at).toLocaleString()}</span>${e.note ? `<br><span class="muted">“${esc(e.note)}”</span>` : ""}</span>
          ${canVoid ? `<button class="ghost" data-void="${e.id}">Void</button>` : ""}
        </div>`;
    }).join("") || "No entries yet.";
    target.querySelectorAll("[data-void]").forEach((button) => {
      button.onclick = async () => {
        if (!confirm("Void this entry? A correction event will be logged; history is kept.")) return;
        await logEvent({
          id: newId(),
          item_id: item.id,
          item_step_id: null,
          station_id: null,
          event_type_id: correctionType.id,
          qty: 0,
          reason_code_id: null,
          occurred_at: new Date().toISOString(),
          note: null,
          supersedes_event_id: button.dataset.void,
          user_id: S.user?.id || null,
        });
        logScreenCache.fetchedAt = 0; // show the void immediately
        loadRecent(item);
      };
    });
  } catch {
    if (target.isConnected) target.textContent = "Offline — recent entries unavailable.";
  }
}

/* -------------------------------------------------------------- reports -- */

async function viewReports() {
  const projects = (S.ref?.projects || []).filter((p) => p.is_active !== false);
  const stages = (S.ref?.stages || []).filter((s) => s.is_active);
  const fProject = validFilter("r-project", projects);
  const fStage = sessionStorage.getItem("r-stage") || "";
  const fDays = sessionStorage.getItem("r-days") || "";
  // The reports are computed on the server, which narrows by area and phase
  // before it replays anything. No "none" here: that is a question for the list.
  const areas = labelOptions("areas", "area_id", [], "", fProject);
  const phases = labelOptions("phases", "phase_id", [], "", fProject);
  const fArea = validFilter("r-area", areas);
  const fPhase = validFilter("r-phase", phases);

  $view.innerHTML = `<p class="muted">Loading reports…</p>`;
  let wip, aging, exceptions;
  let cachedFrom = null; // set when the service worker served last-known data
  const fetchReport = async (path) => {
    const response = await apiResponse(path);
    const marker = response.headers.get("X-LSF-From-Cache");
    if (marker) cachedFrom = marker;
    return response.json();
  };
  try {
    const wipParams = new URLSearchParams();
    const agingParams = new URLSearchParams({ limit: "40" });
    for (const params of [wipParams, agingParams]) {
      if (fProject) params.set("project_id", fProject);
      if (fArea) params.set("area_id", fArea);
      if (fPhase) params.set("phase_id", fPhase);
    }
    if (fStage) agingParams.set("stage_id", fStage);
    if (fDays) agingParams.set("min_days", fDays);
    [wip, aging, exceptions] = await Promise.all([
      fetchReport(`/api/reports/wip?${wipParams}`),
      fetchReport(`/api/reports/aging?${agingParams}`),
      fetchReport("/api/reports/exceptions"),
    ]);
  } catch {
    $view.innerHTML = `<p class="muted">Reports need a connection — they are computed from the full log on the server.</p>`;
    return;
  }
  const staleNote = cachedFrom
    ? `<p class="warn-text" style="margin-bottom:8px">Offline — showing last-known reports${cachedFrom !== "1" ? ` from ${new Date(cachedFrom).toLocaleString()}` : ""}.</p>`
    : "";

  const maxQty = Math.max(1, ...wip.stages.map((s) => s.total_qty));

  $view.innerHTML = `
    ${staleNote}
    <div class="filters">
      <select id="r-project">
        <option value="">All projects</option>
        ${projects.map((p) => `<option value="${p.id}" ${p.id === fProject ? "selected" : ""}>${esc(p.code)}</option>`).join("")}
      </select>
      <select id="r-stage">
        <option value="">All stages</option>
        ${stages.map((s) => `<option value="${s.id}" ${s.id === fStage ? "selected" : ""}>${esc(s.name)}</option>`).join("")}
      </select>
      ${areas.length ? `
      <select id="r-area" aria-label="Area">
        <option value="">All areas</option>
        ${areas.map((a) => `<option value="${a.id}" ${a.id === fArea ? "selected" : ""}>${esc(a.name)}</option>`).join("")}
      </select>` : ""}
      ${phases.length ? `
      <select id="r-phase" aria-label="Phase">
        <option value="">All phases</option>
        ${phases.map((ph) => `<option value="${ph.id}" ${ph.id === fPhase ? "selected" : ""}>${esc(ph.name)}</option>`).join("")}
      </select>` : ""}
      <select id="r-days">
        <option value="">Any age</option>
        ${[1, 2, 3, 5, 7, 14].map((d) => `<option value="${d}" ${String(d) === fDays ? "selected" : ""}>≥ ${d}d</option>`).join("")}
      </select>
    </div>

    <div class="card">
      <h2>WIP by stage</h2>
      ${wip.stages.map((row) => `
        <div style="margin-bottom:12px">
          <div class="spread">
            <strong>${esc(row.stage.name)}</strong>
            <span class="muted">${row.total_qty} pcs · ${row.item_count} item${row.item_count === 1 ? "" : "s"}${row.sample_qty ? ` · <span class="badge sample">${row.sample_qty} sample</span>` : ""}${row.reworked_qty ? ` · <span class="badge rework">rework ${row.reworked_qty}</span>` : ""}</span>
          </div>
          <div class="bar-track" style="margin:4px 0"><div class="bar-fill" style="width:${(row.total_qty / maxQty) * 100}%"></div></div>
          ${row.stage.requires_station ? row.stations.map((s) =>
            `<div class="muted">· ${s.station ? esc(s.station.name) : "No station yet"}: ${s.total_qty}</div>`).join("") : ""}
        </div>`).join("") || `<p class="muted">Nothing in progress.</p>`}
      ${wip.unstarted.qty ? `<p class="muted">Not started: ${wip.unstarted.qty} pcs across ${wip.unstarted.item_count} items</p>` : ""}
    </div>

    <div class="card">
      <h2>Aging — days at current stage</h2>
      <div class="table-scroll">
      <table>
        <thead><tr><th scope="col">Item</th><th scope="col">Where</th><th scope="col" class="num">Qty</th><th scope="col" class="num">Days</th></tr></thead>
        <tbody>
          ${aging.rows.map((row) => `
            <tr>
              <td><a href="#/items/${row.item.id}" style="color:inherit">${esc(row.item.code)}</a>${
                row.item.area || row.item.phase ? `<br><span class="muted">${esc([row.item.area, row.item.phase].filter(Boolean).join(" · "))}</span>` : ""}</td>
              <td>${esc(row.label)}${row.sample_qty ? ` <span class="badge sample">sample</span>` : ""}${row.reworked_qty ? ` <span class="badge rework">R</span>` : ""}</td>
              <td class="num">${row.qty}</td>
              <td class="num" style="${row.overdue ? "color:var(--warn);font-weight:700" : ""}">${row.days_in_state.toFixed(1)}${row.overdue ? " ⚠" : ""}</td>
            </tr>`).join("")}
        </tbody>
      </table>
      </div>
    </div>

    ${exceptions.rows.length ? `
    <div class="card">
      <h2>Needs review (${exceptions.rows.length})</h2>
      ${exceptions.rows.slice(0, 20).map((row) => `
        <div style="padding:6px 0;border-bottom:1px solid var(--line)">
          <span class="badge">${esc(row.code)}</span> <strong>${esc(row.item_code || "")}</strong>${
            row.area || row.phase ? ` <span class="muted">${esc([row.area, row.phase].filter(Boolean).join(" · "))}</span>` : ""}
          <div class="muted">${esc(row.detail)}</div>
        </div>`).join("")}
    </div>` : ""}`;

  for (const id of ["r-project", "r-stage", "r-area", "r-phase", "r-days"]) {
    const select = document.getElementById(id);
    if (select) select.onchange = (e) => { sessionStorage.setItem(id, e.target.value); render(); };
  }
}

/* --------------------------------------------------------------- office -- */

const slugify = (text) =>
  text.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");

/* "B" -> "C", "C3" -> "C4" — a placeholder suggestion, never applied. */
function nextRevision(current) {
  const match = /^(.*?)(\d+)$/.exec(current);
  if (match) return match[1] + (Number(match[2]) + 1);
  if (/^[A-Za-z]$/.test(current) && current.toUpperCase() !== "Z") {
    return String.fromCharCode(current.toUpperCase().charCodeAt(0) + 1);
  }
  return `${current}1`;
}

/* The office is six workspaces rather than one long page: import, items,
   projects, stages, users, status. Each is a subpage under #/office/<section>,
   so an editor is somewhere you navigate to and finish, not something you
   scroll past on the way to something else.

   Projects and their items arrive by file (Import), or one at a time from the
   Items page. There is no Routes subpage and nothing to set up per item: an
   item can be logged at any stage. */
const OFFICE_SECTIONS = [
  { key: "import", label: "Import" },
  { key: "items", label: "Items" },
  { key: "projects", label: "Projects" },
  { key: "stages", label: "Stages", admin: true },
  { key: "areas", label: "Areas & phases", admin: true },
  { key: "users", label: "Users", admin: true },
  { key: "status", label: "Status", admin: true },
];

function viewOffice(section) {
  const sections = OFFICE_SECTIONS.filter((s) => !s.admin || S.user?.is_admin);
  const current = sections.find((s) => s.key === section) || sections[0];

  $view.innerHTML = `
    <nav class="subnav">
      ${sections.map((s) => `
        <a href="#/office/${s.key}"${s.key === current.key ? ` class="on" aria-current="page"` : ""}>${esc(s.label)}</a>`).join("")}
    </nav>
    <div id="office-body"></div>`;

  const body = document.getElementById("office-body");
  ({
    import: officeImport,
    items: officeItems,
    projects: officeProjects,
    stages: officeStages,
    areas: officeAreas,
    users: officeUsers,
    status: officeStatus,
  }[current.key])(body);
}

/* A one-line upload status for the editors, same contract as the snag-photo
   bar: percentage while it goes up, the server's own words if it refuses. */
function inlineUpload(el) {
  return {
    set: (fraction) => {
      const percent = Math.round(fraction * 100);
      el.hidden = false;
      el.className = "muted";
      el.textContent = percent < 100 ? `Uploading… ${percent}%` : "Saving on the server…";
    },
    done: () => { el.hidden = true; },
    fail: (reason) => {
      el.hidden = false;
      el.className = "warn-text";
      el.textContent = reason || "Upload failed.";
    },
  };
}

const inFlightQty = (item) =>
  (item.state?.positions || [])
    .filter((p) => !p.is_unstarted)
    .reduce((sum, p) => sum + p.qty, 0);

/* --------------------------------------------------------- office: import -- */

/* The file picked for import and what the server made of it. Kept outside the
   DOM because a background sync re-renders the Office view, and a file input
   does not survive that. */
const importState = { file: null, preview: null, error: null, busy: false };

function officeImport(body) {
  const { file, preview, error, busy } = importState;
  const errors = preview?.errors || [];
  const items = preview?.items || [];
  const ready = preview && !errors.length && items.length;
  const withPictures = items.filter((i) => i.has_picture).length;

  body.innerHTML = `
    <div class="card">
      <h2>Import a project</h2>
      <p class="muted" style="margin-bottom:8px">One spreadsheet (.xlsx or .csv), one row per item.
      Every item starts as a sample; when the sample is logged Done, the batch is released.
      There are no stages to list: an item can be logged at any stage, in any order.
      For a single item, use <a href="#/office/items">Items → New item</a>.</p>
      <div class="table-scroll">
      <table>
        <thead><tr><th scope="col">Column</th><th scope="col">What goes in it</th></tr></thead>
        <tbody>
          <tr><td><strong>project</strong></td><td>Project code. New codes create the project; existing ones are added to.</td></tr>
          <tr><td>project name, client</td><td>Optional. Only used when the project is new.</td></tr>
          <tr><td><strong>item</strong></td><td>Item code. It may repeat on several rows, as long as each
            has a different area or phase: those are separate batches.</td></tr>
          <tr><td>area, phase</td><td>Optional. Which part of the job, and which phase of it, the batch is
            for. Each project has its own; a name it does not have yet is added to it.</td></tr>
          <tr><td>description</td><td>Optional.</td></tr>
          <tr><td><strong>qty</strong></td><td>The production batch.</td></tr>
          <tr><td>sample qty</td><td>Optional, default 1. Built first; 0 for no sample.</td></tr>
          <tr><td>revision</td><td>Optional drawing revision, default A.</td></tr>
          <tr><td>picture</td><td>Optional, .xlsx only. Put a picture on the item's row
            (Insert → Pictures → <em>Place over Cells</em>) and it becomes the item's icon.</td></tr>
        </tbody>
      </table>
      </div>
      <p style="margin:10px 0"><a href="/api/import/template" download>Download a template</a></p>

      <label for="imp-file">File</label>
      <input id="imp-file" type="file" accept=".xlsx,.csv,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" ${S.online ? "" : "disabled"}>
      ${file ? `<p class="muted" style="margin-top:4px">${esc(file.name)}</p>` : ""}
      ${S.online ? "" : `<p class="warn-text">Importing needs a connection.</p>`}
      ${busy ? `<p class="muted">Reading the file…</p>` : ""}
      ${error ? `<p class="warn-text">${esc(error)}</p>` : ""}
    </div>

    ${preview ? `
    <div class="card">
      <h2>${errors.length ? `${errors.length} problem${errors.length === 1 ? "" : "s"} — nothing will be imported` : "Ready to import"}</h2>
      ${errors.length ? `
      <p class="muted" style="margin-bottom:8px">Fix these in the spreadsheet and choose the file again.
      Row numbers are the spreadsheet's own.</p>
      ${errors.map((e) => `
        <div style="padding:4px 0;border-bottom:1px solid var(--line)">
          ${e.row ? `<span class="badge">row ${e.row}</span> ` : ""}${esc(e.message)}
        </div>`).join("")}` : ""}
      ${(preview.notes || []).map((note) => `<p class="muted" style="margin-bottom:6px">${esc(note)}</p>`).join("")}
      ${withPictures ? `<p class="muted" style="margin-bottom:6px">${withPictures} of ${items.length} item${items.length === 1 ? "" : "s"} ${withPictures === 1 ? "has" : "have"} a picture, which will be set as ${withPictures === 1 ? "its" : "their"} icon.</p>` : ""}
      ${(preview.projects || []).map((p) => `
        <p style="margin-top:8px"><strong>${esc(p.code)}</strong> ${p.is_new ? `<span class="badge">new</span>` : `<span class="badge">existing</span>`}
          <span class="muted">${esc(p.name || "")}${p.client ? ` · ${esc(p.client)}` : ""} · ${p.item_count} item${p.item_count === 1 ? "" : "s"}</span></p>`).join("")}
      ${items.length ? `
      <div class="table-scroll" style="margin-top:8px">
      <table>
        <thead><tr><th scope="col">Item</th><th scope="col" class="num">Qty</th><th scope="col" class="num">Sample</th><th scope="col">Picture</th></tr></thead>
        <tbody>
          ${items.map((i) => `
            <tr>
              <td><strong>${esc(i.code)}</strong>${[i.area, i.phase].filter(Boolean).map((t) => ` <span class="badge tag">${esc(t)}</span>`).join("")}<br><span class="muted">${esc(i.description)} · rev ${esc(i.revision)}</span></td>
              <td class="num">${i.qty}</td>
              <td class="num">${i.sample_qty}</td>
              <td>${i.has_picture ? "✓" : `<span class="muted">—</span>`}</td>
            </tr>`).join("")}
        </tbody>
      </table>
      </div>` : ""}
      ${ready ? `
      <div style="height:12px"></div>
      <button class="primary" id="imp-go" ${S.online && !busy ? "" : "disabled"}>Import ${items.length} item${items.length === 1 ? "" : "s"}</button>` : ""}
    </div>` : ""}`;

  const send = async (commit) => {
    const form = new FormData();
    form.append("file", importState.file);
    form.append("commit", commit ? "true" : "false");
    // No JSON Content-Type: the browser sets the multipart boundary itself.
    return api("/api/import", { method: "POST", body: form, headers: {} });
  };
  const rerender = () => { if (body.isConnected) officeImport(body); };

  document.getElementById("imp-file").onchange = async (event) => {
    const picked = event.target.files[0];
    if (!picked) return;
    Object.assign(importState, { file: picked, preview: null, error: null, busy: true });
    rerender();
    try {
      importState.preview = await send(false);
    } catch (error) {
      const detail = error.body?.detail;
      importState.preview = detail?.errors ? { ...detail, items: detail.items || [] } : null;
      importState.error = importState.preview ? null
        : (detail?.reason || detail || (isNetworkError(error) ? "No connection." : "Could not read the file."));
    }
    importState.busy = false;
    rerender();
  };

  const go = document.getElementById("imp-go");
  if (go) {
    go.onclick = async () => {
      importState.busy = true;
      rerender();
      try {
        const done = await send(true);
        Object.assign(importState, { file: null, preview: null, error: null, busy: false });
        const pictures = done.items.filter((i) => i.has_picture).length;
        toast(`Imported ${done.items.length} item${done.items.length === 1 ? "" : "s"}${pictures ? ` with ${pictures} picture${pictures === 1 ? "" : "s"}` : ""}`);
        await sync();
        navigate("#/items");
      } catch (error) {
        const detail = error.body?.detail;
        importState.busy = false;
        // Someone else created one of these codes since the preview: show the
        // fresh list of problems rather than a bare refusal.
        if (detail?.errors) importState.preview = { ...detail, items: detail.items || [] };
        else importState.error = detail?.reason || detail || "Import failed.";
        rerender();
      }
    };
  }
}

/* ---------------------------------------------------------- office: items -- */

/* The area and phase pickers of the New item form, for one project. */
function newItemLabels(projectId) {
  return `
          <div><label for="ni-area">Area</label>${labelSelect("areas", sessionStorage.getItem("ni-area"), `id="ni-area"`, projectId)}</div>
          <div><label for="ni-phase">Phase</label>${labelSelect("phases", sessionStorage.getItem("ni-phase"), `id="ni-phase"`, projectId)}</div>`;
}

/* One item, added by hand: the extra wardrobe the client asked for after the
   import, the piece nobody put on the schedule. Open to every engineer, like
   the import -- adding work to a project is not an admin job; rewriting what
   is already there is. The form is the item's facts and nothing else: it has
   no stages to pick, because an item can be logged at any stage. */
function officeItems(body) {
  const projects = (S.ref?.projects || []).filter((p) => p.is_active !== false);
  const remembered = sessionStorage.getItem("ni-project") || "";

  body.innerHTML = `
    <div class="card">
      <details data-panel="new-item">
        <summary style="cursor:pointer"><strong>New item</strong>
          <span class="muted">— add one to a project without an import</span></summary>
        ${projects.length ? `
        <label for="ni-project" style="margin-top:10px">Project</label>
        <select id="ni-project">
          ${projects.map((p) => `<option value="${p.id}" ${p.id === remembered ? "selected" : ""}>${esc(p.code)} — ${esc(p.name)}</option>`).join("")}
        </select>
        <div class="field-grid">
          <div><label for="ni-code">Item code</label><input id="ni-code" maxlength="64" autocapitalize="characters" autocomplete="off"></div>
          <div><label for="ni-rev">Drawing revision</label><input id="ni-rev" maxlength="32" autocapitalize="characters" placeholder="A"></div>
        </div>
        <label for="ni-desc">Description</label>
        <input id="ni-desc" maxlength="255" autocomplete="off">
        <div class="field-grid" id="ni-labels">${newItemLabels(projects.some((p) => p.id === remembered) ? remembered : projects[0].id)}</div>
        <p class="muted" style="margin-top:6px">Optional, and from the project's own lists. The same code can
        be added again for another area or phase — each is its own batch.</p>
        <div class="field-grid">
          <div><label for="ni-qty">Batch qty</label><input id="ni-qty" type="number" inputmode="numeric" min="1"></div>
          <div><label for="ni-sample">Sample qty</label><input id="ni-sample" type="number" inputmode="numeric" min="0" placeholder="1"></div>
        </div>
        <p class="muted" style="margin-top:6px">The sample is built first; the batch is released when the
        sample is logged Done. Sample 0 puts the whole batch on the floor at once.</p>
        <div style="height:8px"></div>
        <button class="primary" id="ni-go" ${S.online ? "" : "disabled"}>${S.online ? "Add item" : "Adding an item needs a connection"}</button>
        <p class="warn-text" id="ni-err" role="alert" hidden></p>`
        : `<p class="muted" style="margin-top:10px">There is no active project to add it to. Projects
        arrive through <a href="#/office/import">Import</a>.</p>`}
      </details>
    </div>

    <div class="card">
      <h2>Edit items</h2>
      <p class="muted" style="margin-bottom:8px">Every item, archived ones included. Open one to
      correct its details, set its icon, bump its drawing revision or remove it.</p>
      <div id="of-items" class="muted">Loading…</div>
    </div>`;

  const go = document.getElementById("ni-go");
  if (go) {
    const value = (id) => document.getElementById(id).value.trim();
    // The next one is usually for the same job, area and phase.
    const remember = () => {
      for (const id of ["ni-area", "ni-phase"]) {
        document.getElementById(id).onchange = (e) => { sessionStorage.setItem(id, e.target.value); };
      }
    };
    document.getElementById("ni-project").onchange = (e) => {
      sessionStorage.setItem("ni-project", e.target.value);
      // Another project, another pair of lists.
      document.getElementById("ni-labels").innerHTML = newItemLabels(e.target.value);
      remember();
    };
    remember();
    go.onclick = async () => {
      const err = document.getElementById("ni-err");
      const fail = (message) => { err.textContent = message; err.hidden = false; };
      err.hidden = true;
      const code = value("ni-code");
      const qty = Number(value("ni-qty"));
      const sample = value("ni-sample") === "" ? 1 : Number(value("ni-sample"));
      if (!code) return fail("The item needs a code.");
      if (!Number.isInteger(qty) || qty < 1) return fail("Batch qty must be a whole number, 1 or more.");
      if (!Number.isInteger(sample) || sample < 0) return fail("Sample qty must be a whole number, 0 or more.");
      go.disabled = true;
      try {
        const created = await api("/api/items", {
          method: "POST",
          body: JSON.stringify({
            project_id: value("ni-project"),
            code,
            description: value("ni-desc"),
            area_id: value("ni-area") || null,
            phase_id: value("ni-phase") || null,
            total_qty: qty,
            sample_qty: sample,
            drawing_revision: value("ni-rev") || "A",
          }),
        });
        toast(`${itemName(created)} added — it is on the Items list now`);
        // Clear what was typed, but not the project: the next one is usually
        // for the same job.
        for (const id of ["ni-code", "ni-desc", "ni-qty", "ni-sample", "ni-rev"]) {
          document.getElementById(id).value = "";
        }
        dirtyFields.clear();
        await sync();
        loadItemEditors();
      } catch (error) {
        const detail = error.body?.detail;
        fail(typeof detail === "string" ? detail
          : Array.isArray(detail) ? rejectReason(error)
          : isNetworkError(error) ? "No connection — the item was not added."
          : "Could not add the item.");
      } finally {
        go.disabled = !S.online;
      }
    };
  }

  loadItemEditors();
}

/* The item editor. Fetched with archived items included, because a mistyped
   batch that was archived last week is exactly the thing someone comes here
   to delete for good. */
/* Photo indexes for the item editor, keyed by item id. Fetched when a panel is
   opened and kept until something changes them, so the 30-second background
   re-render redraws an open panel from memory instead of refetching. */
const editorPhotos = new Map();

/* The one screen photos can be deleted from, and admin-only like the rest of
   the destructive actions here. Deleting is deliberately not on the log screen:
   that screen is for recording what happened at speed, in gloves, and nothing
   irreversible belongs under a thumb that is moving quickly. */
async function loadEditorPhotos(itemId, canDelete) {
  const target = document.querySelector(`[data-it-photos="${itemId}"]`);
  if (!target) return;

  let images = editorPhotos.get(itemId);
  if (!images) {
    try {
      ({ images } = await api(`/api/items/${itemId}/images`));
      editorPhotos.set(itemId, images);
    } catch {
      if (target.isConnected) target.textContent = "Photos need a connection.";
      return;
    }
  }
  if (!target.isConnected) return; // the panel closed, or a re-render replaced it

  target.classList.toggle("muted", images.length === 0);
  target.innerHTML = images.length
    ? `<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px">
        ${images.map((img) => `
          <div class="photo-tile">
            <a href="${img.url}" target="_blank" rel="noopener">
              <img src="${img.url}" alt="${esc(img.note || img.filename)}" loading="lazy">
            </a>
            ${img.kind === "icon" ? `<span class="badge" style="position:absolute;bottom:4px;left:4px;background:rgba(0,0,0,.55)">icon</span>` : ""}
            ${canDelete ? `<button class="drop" data-drop="${img.id}" aria-label="Delete this photo"
                    title="Delete this photo" ${S.online ? "" : "disabled"}>✕</button>` : ""}
          </div>`).join("")}
       </div>
       ${canDelete ? "" : `<p class="muted" style="margin-top:6px">Deleting a photo is an admin job.</p>`}`
    : "No photos on this item.";

  target.querySelectorAll("[data-drop]").forEach((button) => {
    const image = images.find((img) => img.id === button.dataset.drop);
    button.onclick = async () => {
      const what = image.kind === "icon" ? "this icon"
        : image.note ? `the photo "${image.note}"`
        : "this photo";
      if (!confirm(`Delete ${what}? The picture goes for good. The item and its logged history are untouched.`)) return;
      try {
        await api(`/api/images/${image.id}`, { method: "DELETE" });
        // The device that just deleted it must not keep serving it back out of
        // its own image cache. Other devices hold a copy until theirs trims --
        // image URLs name immutable bytes and are cached hard, which is the
        // price of photos that load instantly in a dead spot.
        navigator.serviceWorker?.controller?.postMessage({ type: "drop-image", url: image.url });
        editorPhotos.delete(itemId);
        logScreenCache.fetchedAt = 0; // the log screen's copy is stale too
        toast("Photo deleted");
        // Only an icon changes anything the item list is drawn from.
        if (image.kind === "icon") await sync();
        await loadEditorPhotos(itemId, canDelete);
      } catch (error) {
        alert(error.body?.detail || "Could not delete the photo.");
      }
    };
  });
}

async function loadItemEditors() {
  const target = document.getElementById("of-items");
  if (!target) return;

  let items;
  try {
    ({ items } = await api("/api/items?include_archived=true"));
  } catch {
    if (target.isConnected) target.textContent = "Editing items needs a connection.";
    return;
  }
  if (!target.isConnected) return;

  const allProjects = S.ref?.projects || [];
  // The badge must name an archived project too, so the lookup keeps every one.
  const projectCode = new Map(allProjects.map((p) => [p.id, p.code]));
  const projects = allProjects.filter((p) => p.is_active !== false);
  const isAdmin = !!S.user?.is_admin;
  // An archived project is never an option in a project picker -- here or in
  // the Items and Reports filters. For an item that sits inside one, that
  // leaves nothing in the list to show as selected, and a select that showed
  // some other project instead would move the item on the next Save. So such
  // an item gets its project as plain text, and the picker becomes "move to":
  // empty by default, which Save reads as "leave the project alone".
  const projectField = (item) => {
    const lock = isAdmin ? "" : "disabled";
    const option = (p, selected) =>
      `<option value="${p.id}" ${selected ? "selected" : ""}>${esc(p.code)}</option>`;
    if (projects.some((p) => p.id === item.project_id)) {
      return `
        <label>Project</label>
        <select data-it-project="${item.id}" ${lock}>
          ${projects.map((p) => option(p, p.id === item.project_id)).join("")}
        </select>`;
    }
    return `
        <label>Project</label>
        <p class="muted">${esc(projectCode.get(item.project_id) || "no project")} — archived</p>
        <label>Move to a live project</label>
        <select data-it-project="${item.id}" ${lock}>
          <option value="" selected>Leave it where it is</option>
          ${projects.map((p) => option(p, false)).join("")}
        </select>`;
  };
  target.classList.remove("muted");
  // An item's area and phase come from its project's lists. Shown for another
  // project (the one it is about to be moved to), nothing is selected: its
  // current area does not exist there.
  const editorLabels = (item, projectId) => {
    const own = projectId === item.project_id;
    const lock = isAdmin ? "" : "disabled";
    return `
          <div><label>Area</label>${labelSelect("areas", own ? item.area_id : null, `data-it-area="${item.id}" ${lock}`, projectId)}</div>
          <div><label>Phase</label>${labelSelect("phases", own ? item.phase_id : null, `data-it-phase="${item.id}" ${lock}`, projectId)}</div>`;
  };

  target.innerHTML = items.map((item) => `
    <details data-panel="item:${item.id}" style="padding:4px 0;border-bottom:1px solid var(--line)">
      <summary style="cursor:pointer;padding:6px 0">
        <strong>${esc(item.code)}</strong>
        <span class="badge">${esc(projectCode.get(item.project_id) || "no project")}</span>
        ${itemTags(item)}
        ${item.is_active ? "" : `<span class="badge">archived</span>`}
        <br><span class="muted">${esc(item.description)} · ${item.total_qty} pcs${item.sample_qty ? ` + ${item.sample_qty} sample` : ""} · rev ${esc(item.drawing_revision)}</span>
      </summary>
      <div style="padding:8px 0 12px">
        <!-- Readable by anyone, editable by admins: a code or a batch size is
             what the rest of the floor reads the work by. -->
        <div class="field-grid">
          <div><label>Code</label><input data-it-code="${item.id}" value="${esc(item.code)}" ${isAdmin ? "" : "disabled"}></div>
          <div><label>Batch qty</label><input data-it-qty="${item.id}" type="number" inputmode="numeric" min="1" value="${item.total_qty}" ${isAdmin ? "" : "disabled"}></div>
        </div>
        <label>Description</label>
        <input data-it-desc="${item.id}" value="${esc(item.description)}" ${isAdmin ? "" : "disabled"}>
        ${projectField(item)}
        <div class="field-grid" data-it-labels="${item.id}">${editorLabels(item, item.project_id)}</div>
        ${isAdmin ? `
        <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
          <button class="ghost" data-it-save="${item.id}" style="width:auto;padding:10px 16px" ${S.online ? "" : "disabled"}>Save</button>
        </div>`
        : `<p class="muted" style="margin-top:10px">Changing these is an admin job. Creating
        items, logging entries and bumping the revision are not.</p>`}

        <label style="margin-top:12px">Item icon</label>
        <div style="display:flex;gap:10px;align-items:center">
          ${item.icon_url ? `<img class="item-icon" src="${item.icon_url}" alt="">` : `<span class="muted">None yet.</span>`}
          <div style="flex:1">
            ${photoPicker(`icon:${item.id}`, {
              camera: "Photograph it",
              gallery: "From gallery",
              single: `${item.icon_url ? "Replace" : "Set"} icon`,
              offline: "Needs a connection",
            })}
          </div>
        </div>
        <p data-it-upload="${item.id}" class="muted" style="margin-top:6px" hidden></p>

        <label style="margin-top:12px">Photos</label>
        <div data-it-photos="${item.id}" class="muted">${
          editorPhotos.has(item.id) ? "" : "Open to load."
        }</div>

        <label style="margin-top:12px">Drawing revision — currently ${esc(item.drawing_revision)}</label>
        <div class="qty-row">
          <input data-it-rev="${item.id}" placeholder="new rev, e.g. ${esc(nextRevision(item.drawing_revision))}"
                 autocapitalize="characters" maxlength="32" style="flex:1;text-align:left;padding:10px">
          <button class="ghost" data-it-bump="${item.id}" style="width:auto;padding:10px 16px" ${S.online ? "" : "disabled"}>Bump</button>
        </div>
        <p class="muted" style="margin-top:4px">${inFlightQty(item)} pcs in flight were built to rev ${esc(item.drawing_revision)}.</p>

        ${isAdmin ? `
        <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap">
          ${item.is_active
            ? `<button class="ghost" data-it-archive="${item.id}" style="width:auto;padding:10px 16px" ${S.online ? "" : "disabled"}>Archive</button>`
            : `<button class="ghost" data-it-restore="${item.id}" style="width:auto;padding:10px 16px" ${S.online ? "" : "disabled"}>Reactivate</button>`}
          <button class="ghost" data-it-purge="${item.id}" style="width:auto;padding:10px 16px;border-color:var(--warn);color:var(--warn)" ${S.online ? "" : "disabled"}>Delete for good</button>
        </div>
        <p class="muted" style="margin-top:6px">Archiving hides the item everywhere and keeps its history;
        an archived item can be reactivated. Deleting for good removes the item <em>and its logged
        events</em>, and cannot be undone.</p>` : ""}
      </div>
    </details>`).join("") || `<span class="muted">No items yet.</span>`;

  restoreView(lastSnapshot); // this list lands after render() restored the view

  const byId = new Map(items.map((i) => [i.id, i]));
  const field = (attr, id) => target.querySelector(`[${attr}="${id}"]`);

  // Picking another project swaps the area and phase pickers to its lists.
  target.querySelectorAll("[data-it-project]").forEach((select) => {
    select.onchange = () => {
      const item = byId.get(select.dataset.itProject);
      const holder = field("data-it-labels", item.id);
      if (holder) holder.innerHTML = editorLabels(item, select.value || item.project_id);
    };
  });

  target.querySelectorAll("[data-it-save]").forEach((button) => {
    button.onclick = async () => {
      const id = button.dataset.itSave;
      const code = field("data-it-code", id).value.trim();
      if (!code) return;
      const body = {
        code,
        description: field("data-it-desc", id).value.trim(),
        total_qty: Number(field("data-it-qty", id).value) || byId.get(id).total_qty,
        area_id: field("data-it-area", id).value || null,
        phase_id: field("data-it-phase", id).value || null,
      };
      // Empty means "leave it where it is": the item's project is archived and
      // nothing was picked to move it to.
      const projectId = field("data-it-project", id).value;
      if (projectId) body.project_id = projectId;
      try {
        await api(`/api/items/${id}`, { method: "PATCH", body: JSON.stringify(body) });
        toast("Item updated");
        await sync();
      } catch (error) {
        alert(error.body?.detail || "Could not update the item.");
      }
    };
  });

  target.querySelectorAll(`[data-photo^="icon:"]`).forEach((input) => {
    input.onchange = async () => {
      const id = input.dataset.photo.slice("icon:".length);
      const file = input.files[0];
      if (!file) return;
      const status = inlineUpload(field("data-it-upload", id));
      status.set(0);
      try {
        await uploadImage(id, file, { kind: "icon", onProgress: status.set });
        status.done();
        toast("Item icon updated");
        editorPhotos.delete(id); // the item just gained a photo
        await sync();
      } catch (error) {
        status.fail(error.detail);
      } finally {
        clearPicker(`icon:${id}`);
      }
    };
  });

  // Photos load per item, when its panel is opened: this editor lists every
  // item in the factory, and fetching a photo index for each one on every
  // render would be dozens of requests for panels nobody looked at.
  target.querySelectorAll(`details[data-panel^="item:"]`).forEach((panel) => {
    const id = panel.dataset.panel.slice("item:".length);
    const load = () => { if (panel.open) loadEditorPhotos(id, isAdmin); };
    panel.ontoggle = load;
    load(); // a background re-render lands with the panel already open
  });

  target.querySelectorAll("[data-it-bump]").forEach((button) => {
    button.onclick = async () => {
      const id = button.dataset.itBump;
      const item = byId.get(id);
      const revision = field("data-it-rev", id).value.trim();
      if (!revision) return;
      const affected = inFlightQty(item);
      const detail = affected
        ? `${affected} pcs in flight were built to rev ${item.drawing_revision}.`
        : "Nothing is in flight yet.";
      if (!confirm(`Bump ${item.code} from rev ${item.drawing_revision} to ${revision}? ${detail}`)) return;
      try {
        await api(`/api/items/${id}/revision`, {
          method: "POST",
          body: JSON.stringify({ drawing_revision: revision }),
        });
        toast(`${item.code} bumped to rev ${revision}`);
        await sync();
      } catch (error) {
        alert(error.body?.detail || "Could not bump the revision.");
      }
    };
  });

  target.querySelectorAll("[data-it-archive]").forEach((button) => {
    button.onclick = async () => {
      const item = byId.get(button.dataset.itArchive);
      if (!confirm(`Archive ${item.code}? It disappears from lists and reports; its history stays.`)) return;
      try {
        const result = await api(`/api/items/${item.id}`, { method: "DELETE" });
        toast(result.archived ? "Item archived (history kept)" : "Item deleted");
        await sync();
        loadItemEditors();
      } catch (error) {
        alert(error.body?.detail || "Could not remove the item.");
      }
    };
  });

  target.querySelectorAll("[data-it-restore]").forEach((button) => {
    button.onclick = async () => {
      const item = byId.get(button.dataset.itRestore);
      try {
        await api(`/api/items/${item.id}`, {
          method: "PATCH", body: JSON.stringify({ is_active: true }),
        });
        toast(`${item.code} is back on the floor's lists`);
        await sync();
        loadItemEditors();
      } catch (error) {
        alert(error.body?.detail || "Could not reactivate the item.");
      }
    };
  });

  target.querySelectorAll("[data-it-purge]").forEach((button) => {
    button.onclick = async () => {
      const item = byId.get(button.dataset.itPurge);
      const events = item.state?.event_count || 0;
      // Typing the code, because this deletes the item's logged events along
      // with it and there is no undo behind it.
      const typed = prompt(
        `Delete ${itemName(item)} for good?\n\nThis destroys the item and its ${events} logged event(s). ` +
        `It cannot be undone — archiving is what you want for anything really built.\n\n` +
        `Type ${item.code} to confirm:`
      );
      if (typed === null) return;
      if (typed.trim() !== item.code) { alert("The code did not match; nothing was deleted."); return; }
      try {
        const result = await api(`/api/items/${item.id}?purge=true`, { method: "DELETE" });
        toast(`${item.code} deleted for good (${result.purged_events} event(s) destroyed)`);
        await sync();
        loadItemEditors();
      } catch (error) {
        alert(error.body?.detail || "Could not delete the item.");
      }
    };
  });
}

/* ------------------------------------------------------- office: projects -- */

function officeProjects(body) {
  const projects = S.ref?.projects || [];
  const isAdmin = !!S.user?.is_admin;

  body.innerHTML = `
    <div class="card">
      <h2>Edit projects</h2>
      <p class="muted" style="margin-bottom:8px">New projects arrive with their items through
      <a href="#/office/import">Import</a>.</p>
      ${projects.map((p) => `
        <details data-panel="project:${p.id}" style="padding:4px 0;border-bottom:1px solid var(--line)">
          <summary style="cursor:pointer;padding:6px 0">
            <strong>${esc(p.code)}</strong>${p.is_active ? "" : ` <span class="badge">archived</span>`}
            <br><span class="muted">${esc(p.name)}${p.client ? ` · ${esc(p.client)}` : ""}</span>
          </summary>
          <div style="padding:8px 0 12px">
            <label>Name</label><input data-pr-name="${p.id}" value="${esc(p.name)}">
            <label>Client</label><input data-pr-client="${p.id}" value="${esc(p.client || "")}">
            ${isAdmin ? `
            <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
              <button class="ghost" data-pr-save="${p.id}" style="width:auto;padding:10px 16px" ${S.online ? "" : "disabled"}>Save</button>
              <button class="ghost" data-pr-toggle="${p.id}" style="width:auto;padding:10px 16px" ${S.online ? "" : "disabled"}>${p.is_active ? "Archive" : "Reactivate"}</button>
            </div>
            <p class="muted" style="margin-top:6px">Archiving a project archives its items with it. Their
            history stays, and both can be reactivated. The code is fixed — it appears on drawings and
            emails that outlive this app.</p>`
            : `<p class="muted" style="margin-top:10px">Editing projects is an admin job.</p>`}
          </div>
        </details>`).join("") || `<span class="muted">No projects yet.</span>`}
    </div>`;

  const patchProject = async (id, patch, doneMessage) => {
    try {
      await api(`/api/projects/${id}`, { method: "PATCH", body: JSON.stringify(patch) });
      toast(doneMessage);
      await sync();
    } catch (error) {
      alert(error.body?.detail || "Could not update the project.");
    }
  };

  body.querySelectorAll("[data-pr-save]").forEach((button) => {
    button.onclick = () => {
      const id = button.dataset.prSave;
      const name = body.querySelector(`[data-pr-name="${id}"]`).value.trim();
      if (!name) return;
      patchProject(id, {
        name,
        client: body.querySelector(`[data-pr-client="${id}"]`).value.trim() || null,
      }, "Project updated");
    };
  });

  body.querySelectorAll("[data-pr-toggle]").forEach((button) => {
    button.onclick = async () => {
      const project = projects.find((p) => p.id === button.dataset.prToggle);
      if (!project.is_active) {
        patchProject(project.id, { is_active: true }, "Project reactivated");
        return;
      }
      // S.items is the active list, so this is what will be archived with it.
      const live = S.items.filter((i) => i.project_id === project.id).length;
      const question = live
        ? `Archive ${project.code} and its ${live} active item${live === 1 ? "" : "s"}? They leave every list and report; their history stays, and they can be reactivated.`
        : `Archive ${project.code}?`;
      if (!confirm(question)) return;
      try {
        const result = await api(`/api/projects/${project.id}?with_items=true`, { method: "DELETE" });
        toast(result.archived_items
          ? `Project archived with ${result.archived_items} item${result.archived_items === 1 ? "" : "s"}`
          : "Project archived");
        await sync();
      } catch (error) {
        alert(error.body?.detail || "Could not archive the project.");
      }
    };
  });
}

/* A picker over one project's areas or phases. Retired rows are left off,
   except the one an item is already filed under: drop that and the select
   would quietly show "none", and the next Save would un-file the item. */
function labelSelect(list, selected, attrs, projectId) {
  const rows = (S.ref?.[list] || []).filter((row) =>
    row.project_id === projectId && (row.is_active || row.id === selected));
  const picked = rows.some((row) => row.id === selected) ? selected : null;
  return `
    <select ${attrs}>
      <option value="">— none —</option>
      ${rows.map((row) => `<option value="${row.id}" ${row.id === picked ? "selected" : ""}>${esc(row.name)}${row.is_active ? "" : " (retired)"}</option>`).join("")}
    </select>`;
}

/* ------------------------------------------------- office: areas & phases -- */

/* Two lists of the same shape, edited the same way: which part of a job a
   batch is for, and which phase of it. They are what the Area and Phase
   filters are made of. Renaming one re-labels every item filed under it;
   retiring one takes it off the pickers and leaves its items where they are. */
const LABEL_LISTS = [
  { list: "areas", field: "area_id", noun: "area", title: "Areas",
    blurb: "The parts of a job: guest rooms, the spa, the all-day dining room.", example: "Guest rooms" },
  { list: "phases", field: "phase_id", noun: "phase", title: "Phases",
    blurb: "The phases a job is built and delivered in.", example: "Phase 1" },
];

/* Filing many items at once: search, tick, search again, tick more, then put
   everything ticked under an area and/or a phase.

   The ticks are kept here, not in the DOM, for two reasons. They have to
   outlive a search -- a batch is usually gathered from several ("bed", then
   "bench", then "LF-07") -- and the list they sit in is repainted on every
   keystroke and on every background sync. */
const KEEP = "keep"; // "leave this one as it is"
const filing = {
  // `project` is the one the whole Areas & phases page is showing.
  q: "", project: "", inArea: "", inPhase: "", onlyTicked: false,
  ticked: new Set(), area: KEEP, phase: KEEP,
};
const FILING_SHOWN = 300; // rows painted; ticking "all" still takes every match

/* A search box that takes several things at once. Commas separate alternatives
   and the words inside one must all be there: "bed base, LF-03" finds every
   bed base and LF-03 as well. Matched against the code, the description, the
   project, and the area and phase an item is already under. */
function filingMatcher(query) {
  const alternatives = query.toLowerCase().split(",")
    .map((part) => part.trim().split(/\s+/).filter(Boolean))
    .filter((words) => words.length);
  if (!alternatives.length) return () => true;
  const projectCode = new Map((S.ref?.projects || []).map((pr) => [pr.id, pr.code]));
  return (item) => {
    const text = `${item.code} ${item.description} ${projectCode.get(item.project_id) || ""} ${areaName(item)} ${phaseName(item)}`.toLowerCase();
    return alternatives.some((words) => words.every((word) => text.includes(word)));
  };
}

function filingMatches() {
  const matches = filingMatcher(filing.q);
  return S.items.filter((item) =>
    item.project_id === filing.project
    && matchesLabel(item, "area_id", filing.inArea)
    && matchesLabel(item, "phase_id", filing.inPhase)
    && (!filing.onlyTicked || filing.ticked.has(item.id))
    && matches(item));
}

function filingCard() {
  const own = (list) => (S.ref?.[list] || []).filter((row) => row.project_id === filing.project);
  const current = (list, picked, any, none) => `
        <option value="">${any}</option>
        <option value="${NONE}" ${picked === NONE ? "selected" : ""}>${none}</option>
        ${own(list).map((row) => `<option value="${row.id}" ${row.id === picked ? "selected" : ""}>${esc(row.name)}${row.is_active ? "" : " (retired)"}</option>`).join("")}`;
  const target = (list, picked) => `
        <option value="${KEEP}">— leave as it is —</option>
        <option value="${NONE}" ${picked === NONE ? "selected" : ""}>— none —</option>
        ${own(list).filter((row) => row.is_active).map((row) => `<option value="${row.id}" ${row.id === picked ? "selected" : ""}>${esc(row.name)}</option>`).join("")}`;
  return `
    <div class="card">
      <h2>File items under an area or phase</h2>
      <p class="muted" style="margin-bottom:8px">Search this project's items, tick them, choose where they
      go, apply. The ticks stay while you search again, so one batch can be gathered from several searches.</p>
      <input id="fl-q" type="search" value="${esc(filing.q)}" style="margin-bottom:8px"
             placeholder="Search — several at once with commas: bed base, LF-03">
      <div class="filters">
        <select id="fl-in-area" aria-label="Currently in area">${current("areas", filing.inArea, "In any area", "In no area")}</select>
        <select id="fl-in-phase" aria-label="Currently in phase">${current("phases", filing.inPhase, "In any phase", "In no phase")}</select>
      </div>
      <div class="spread" style="margin-bottom:6px;flex-wrap:wrap;gap:8px">
        <span class="muted" id="fl-count"></span>
        <span style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="ghost" id="fl-all" style="width:auto;padding:8px 14px">Tick all found</button>
          <button class="ghost" id="fl-none" style="width:auto;padding:8px 14px">Untick everything</button>
        </span>
      </div>
      <label class="pick-row" style="border:0;min-height:0;padding:2px 0 8px">
        <input type="checkbox" id="fl-only" ${filing.onlyTicked ? "checked" : ""}>
        <span class="muted">Show only what is ticked</span>
      </label>
      <div id="fl-results" class="pick-list"></div>
      <div class="field-grid" style="margin-top:12px">
        <div><label for="fl-area">Set area to</label><select id="fl-area">${target("areas", filing.area)}</select></div>
        <div><label for="fl-phase">Set phase to</label><select id="fl-phase">${target("phases", filing.phase)}</select></div>
      </div>
      <div style="height:10px"></div>
      <button class="primary" id="fl-go"></button>
      <p class="warn-text" id="fl-err" role="alert" hidden></p>
    </div>`;
}

function bindFiling(body) {
  const $results = document.getElementById("fl-results");
  if (!$results) return;
  const projectCode = new Map((S.ref?.projects || []).map((pr) => [pr.id, pr.code]));

  // Ticks for items that have since been archived or deleted mean nothing.
  const live = new Set(S.items.map((item) => item.id));
  for (const id of filing.ticked) if (!live.has(id)) filing.ticked.delete(id);

  const label = () => {
    const n = filing.ticked.size;
    const chosen = filing.area !== KEEP || filing.phase !== KEEP;
    const go = document.getElementById("fl-go");
    go.textContent = !S.online ? "Filing needs a connection"
      : !n ? "Tick the items to file"
      : !chosen ? "Choose an area or a phase to set"
      : `File ${n} item${n === 1 ? "" : "s"}`;
    go.disabled = !S.online || !n || !chosen;
  };
  const paint = () => {
    const found = filingMatches();
    const shown = found.slice(0, FILING_SHOWN);
    const tickedHere = found.filter((item) => filing.ticked.has(item.id)).length;
    document.getElementById("fl-count").textContent =
      `${found.length} found · ${filing.ticked.size} ticked${filing.ticked.size !== tickedHere ? ` (${tickedHere} of them here)` : ""}`;
    $results.innerHTML = shown.map((item) => `
      <label class="pick-row">
        <input type="checkbox" data-fl-pick="${item.id}" ${filing.ticked.has(item.id) ? "checked" : ""}>
        <span style="min-width:0">
          <strong>${esc(item.code)}</strong> ${itemTags(item)}
          <br><span class="muted">${esc(projectCode.get(item.project_id) || "")} · ${item.total_qty} pcs${item.description ? ` · ${esc(item.description)}` : ""}</span>
        </span>
      </label>`).join("")
      + (found.length > shown.length ? `<p class="muted" style="padding:8px 0">Showing the first ${shown.length} of ${found.length}. Narrow the search to see the rest — "Tick all found" still takes all ${found.length}.</p>` : "")
      || `<p class="muted" style="padding:8px 0">${filing.onlyTicked ? "Nothing is ticked." : "No items match."}</p>`;
    label();
  };

  // One listener for the whole list: it is repainted on every keystroke.
  $results.onchange = (e) => {
    const id = e.target?.dataset?.flPick;
    if (!id) return;
    if (e.target.checked) filing.ticked.add(id); else filing.ticked.delete(id);
    // Repainting is cheap and keeps the count, the list and the button honest
    // together; the box that was ticked keeps its place because the order is
    // the same.
    const top = $results.scrollTop;
    paint();
    $results.scrollTop = top;
  };
  document.getElementById("fl-q").oninput = (e) => { filing.q = e.target.value; paint(); };
  for (const [id, key] of [["fl-in-area", "inArea"], ["fl-in-phase", "inPhase"]]) {
    document.getElementById(id).onchange = (e) => { filing[key] = e.target.value; paint(); };
  }
  for (const [id, key] of [["fl-area", "area"], ["fl-phase", "phase"]]) {
    document.getElementById(id).onchange = (e) => { filing[key] = e.target.value; label(); };
  }
  document.getElementById("fl-only").onchange = (e) => { filing.onlyTicked = e.target.checked; paint(); };
  document.getElementById("fl-all").onclick = () => {
    for (const item of filingMatches()) filing.ticked.add(item.id);
    paint();
  };
  document.getElementById("fl-none").onclick = () => { filing.ticked.clear(); filing.onlyTicked = false; document.getElementById("fl-only").checked = false; paint(); };

  document.getElementById("fl-go").onclick = async () => {
    const err = document.getElementById("fl-err");
    err.hidden = true;
    const ids = [...filing.ticked];
    if (!ids.length || (filing.area === KEEP && filing.phase === KEEP)) return;
    const payload = { item_ids: ids };
    const where = [];
    for (const [key, list, field, noun] of [["area", "areas", "area_id", "area"], ["phase", "phases", "phase_id", "phase"]]) {
      if (filing[key] === KEEP) continue;
      payload[field] = filing[key] === NONE ? null : filing[key];
      where.push(filing[key] === NONE ? `no ${noun}` : labelRow(list, filing[key])?.name || noun);
    }
    if (!confirm(`File ${ids.length} item${ids.length === 1 ? "" : "s"} under ${where.join(" and ")}?`)) return;
    const go = document.getElementById("fl-go");
    go.disabled = true;
    try {
      const done = await api("/api/items/assign", { method: "POST", body: JSON.stringify(payload) });
      toast(`${done.updated} item${done.updated === 1 ? "" : "s"} filed under ${where.join(" and ")}`);
      // Done with this batch: start the next one clean, but keep the search --
      // the next thing is usually to look at what is left.
      filing.ticked.clear();
      filing.onlyTicked = false;
      filing.area = KEEP;
      filing.phase = KEEP;
      dirtyFields.clear();
      await sync();
      if (body.isConnected) officeAreas(body);
    } catch (error) {
      const detail = error.body?.detail;
      err.textContent = typeof detail === "string" ? detail
        : isNetworkError(error) ? "No connection — nothing was changed."
        : "Could not file the items.";
      err.hidden = false;
      label();
    }
  };

  paint();
}

function officeAreas(body) {
  // The whole page is one project's: its areas, its phases, its items.
  const projects = (S.ref?.projects || []).filter((pr) => pr.is_active !== false);
  if (!projects.length) {
    body.innerHTML = `
    <div class="card">
      <h2>Areas &amp; phases</h2>
      <p class="muted">Areas and phases belong to a project, and there is no active project yet.
      Projects arrive through <a href="#/office/import">Import</a>.</p>
    </div>`;
    return;
  }
  const stored = sessionStorage.getItem("lb-project");
  const projectId = projects.some((pr) => pr.id === stored) ? stored : projects[0].id;
  if (filing.project !== projectId) {
    // Another job: its items are a different set, and so are its lists.
    filing.ticked.clear();
    Object.assign(filing, { project: projectId, inArea: "", inPhase: "", onlyTicked: false, area: KEEP, phase: KEEP });
  }
  const project = projects.find((pr) => pr.id === projectId);

  body.innerHTML = `
    <div class="card">
      <label for="lb-project" style="margin-top:0">Project</label>
      <select id="lb-project">
        ${projects.map((pr) => `<option value="${pr.id}" ${pr.id === projectId ? "selected" : ""}>${esc(pr.code)} — ${esc(pr.name)}</option>`).join("")}
      </select>
      <p class="muted" style="margin-top:6px">Each project has its own areas and phases. Everything on
      this page is ${esc(project.code)}'s.</p>
    </div>` + filingCard() + LABEL_LISTS.map(({ list, field, noun, title, blurb, example }) => {
    const rows = (S.ref?.[list] || []).filter((row) => row.project_id === projectId);
    return `
    <div class="card">
      <h2>${title} of ${esc(project.code)}</h2>
      <p class="muted" style="margin-bottom:8px">${blurb} Every item of this project can be filed under
      one, from the import file, its editor or the tool above, and the Items list and Reports can be
      narrowed to it.</p>
      ${rows.map((row) => {
        const count = S.items.filter((item) => item[field] === row.id).length;
        return `
        <details data-panel="${list}:${row.id}" style="padding:4px 0;border-bottom:1px solid var(--line)">
          <summary style="cursor:pointer;padding:6px 0">
            <strong>${esc(row.name)}</strong>${row.is_active ? "" : ` <span class="badge">retired</span>`}
            <span class="muted"> · ${count} item${count === 1 ? "" : "s"} on the floor</span>
          </summary>
          <div style="padding:8px 0 12px">
            <div class="field-grid">
              <div><label>Name</label><input data-lb-name="${list}:${row.id}" maxlength="120" value="${esc(row.name)}"></div>
              <div><label>Sort order</label><input data-lb-sort="${list}:${row.id}" type="number" inputmode="numeric" value="${row.sort_order}"></div>
            </div>
            <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap">
              <button class="ghost" data-lb-save="${list}:${row.id}" style="width:auto;padding:10px 16px" ${S.online ? "" : "disabled"}>Save</button>
              <button class="ghost" data-lb-toggle="${list}:${row.id}" style="width:auto;padding:10px 16px" ${S.online ? "" : "disabled"}>${row.is_active ? "Retire" : "Reactivate"}</button>
              ${count ? "" : `<button class="ghost" data-lb-delete="${list}:${row.id}" style="width:auto;padding:10px 16px;border-color:var(--warn);color:var(--warn)" ${S.online ? "" : "disabled"}>Delete</button>`}
            </div>
            <p class="muted" style="margin-top:6px">${count
              ? "Retiring takes it off the pickers; the items filed under it stay there."
              : "Nothing on the floor is filed under it, so it can be deleted outright."}</p>
          </div>
        </details>`;
      }).join("") || `<p class="muted">No ${noun}s yet.</p>`}
      <label style="margin-top:12px">Add ${noun === "area" ? "an" : "a"} ${noun}</label>
      <div class="qty-row">
        <input data-lb-new="${list}" maxlength="120" placeholder="e.g. ${example}" style="flex:1;text-align:left;padding:10px">
        <button class="ghost" data-lb-add="${list}" style="width:auto;padding:10px 16px" ${S.online ? "" : "disabled"}>Add</button>
      </div>
      <p class="warn-text" data-lb-err="${list}" role="alert" hidden></p>
    </div>`;
  }).join("");

  document.getElementById("lb-project").onchange = (e) => {
    sessionStorage.setItem("lb-project", e.target.value);
    dirtyFields.clear();
    officeAreas(body);
  };
  bindFiling(body);

  const meta = (list) => LABEL_LISTS.find((entry) => entry.list === list);
  const refuse = (error, fallback) => alert(typeof error.body?.detail === "string" ? error.body.detail : fallback);
  const patch = async (key, payload, done) => {
    const [list, id] = key.split(":");
    try {
      await api(`/api/${list}/${id}`, { method: "PATCH", body: JSON.stringify(payload) });
      toast(done);
      await sync(); // refreshes the lists, and the filters made from them
    } catch (error) {
      refuse(error, `Could not update the ${meta(list).noun}.`);
    }
  };

  body.querySelectorAll("[data-lb-save]").forEach((button) => {
    button.onclick = () => {
      const key = button.dataset.lbSave;
      const name = body.querySelector(`[data-lb-name="${key}"]`).value.trim();
      if (!name) return;
      patch(key, { name, sort_order: Number(body.querySelector(`[data-lb-sort="${key}"]`).value) || 0 }, "Saved");
    };
  });
  body.querySelectorAll("[data-lb-toggle]").forEach((button) => {
    button.onclick = () => {
      const [list, id] = button.dataset.lbToggle.split(":");
      const row = labelRow(list, id);
      const { field, noun } = meta(list);
      const count = S.items.filter((item) => item[field] === id).length;
      if (row.is_active && count && !confirm(
        `Retire ${row.name}? ${count} item${count === 1 ? " is" : "s are"} filed under it and will stay there; it just cannot be picked for new ones.`)) return;
      patch(button.dataset.lbToggle, { is_active: !row.is_active },
        row.is_active ? `${row.name} retired` : `${row.name} is back on the list`);
    };
  });
  body.querySelectorAll("[data-lb-delete]").forEach((button) => {
    button.onclick = async () => {
      const [list, id] = button.dataset.lbDelete.split(":");
      const row = labelRow(list, id);
      if (!confirm(`Delete ${row.name}? It is removed from ${project.code}'s ${list} for good.`)) return;
      try {
        await api(`/api/${list}/${id}`, { method: "DELETE" });
        toast(`${row.name} deleted`);
        await sync();
        if (body.isConnected) officeAreas(body);
      } catch (error) {
        // The server counts archived items too, which this phone does not hold.
        refuse(error, `Could not delete the ${meta(list).noun}.`);
      }
    };
  });
  body.querySelectorAll("[data-lb-add]").forEach((button) => {
    button.onclick = async () => {
      const list = button.dataset.lbAdd;
      const input = body.querySelector(`[data-lb-new="${list}"]`);
      const err = body.querySelector(`[data-lb-err="${list}"]`);
      const name = input.value.trim();
      err.hidden = true;
      if (!name) return;
      const last = Math.max(0, ...(S.ref?.[list] || [])
        .filter((row) => row.project_id === projectId).map((row) => row.sort_order));
      try {
        await api(`/api/${list}`, {
          method: "POST", body: JSON.stringify({ project_id: projectId, name, sort_order: last + 10 }),
        });
        input.value = "";
        dirtyFields.clear();
        toast(`${name} added`);
        await sync();
      } catch (error) {
        err.textContent = typeof error.body?.detail === "string" ? error.body.detail : `Could not add the ${meta(list).noun}.`;
        err.hidden = false;
      }
    };
  });
}

/* --------------------------------------------------------- office: stages -- */

function officeStages(body) {
  body.innerHTML = `
    <div class="card">
      <h2>Stages &amp; stations</h2>
      <p class="muted" style="margin-bottom:8px">Adding a stage needs no deploy and no code
      change: add it here with its behaviour flags, add stations if it has physical
      instances, and every item can be logged at it straight away. Sort order is only the
      order stages are listed in — units move between them freely.</p>
      <div id="mg-stages" class="muted">Loading…</div>
      <details data-panel="new-stage" style="margin-top:12px">
        <summary class="muted" style="cursor:pointer">New stage</summary>
        <div class="field-grid" style="margin-top:8px">
          <div><label for="ns-name">Name</label><input id="ns-name" placeholder="Glass shop"></div>
          <div><label for="ns-sort">Sort order</label><input id="ns-sort" type="number" inputmode="numeric"></div>
        </div>
        <label for="ns-days">Flag as aging after (days — empty for never)</label>
        <input id="ns-days" type="number" inputmode="numeric" min="1" max="365" value="3">
        <div id="ns-flags"></div>
        <div style="height:8px"></div>
        <button class="primary" id="ns-go" ${S.online ? "" : "disabled"}>Add stage</button>
        <p class="warn-text" id="ns-err" hidden></p>
      </details>
    </div>`;
  renderStageAdmin();
}

/* ---------------------------------------------------------- office: users -- */

function officeUsers(body) {
  body.innerHTML = `
    <div class="card">
      <h2>Users</h2>
      <div id="mg-users" class="muted">Loading…</div>
      <details data-panel="new-user" style="margin-top:12px">
        <summary class="muted" style="cursor:pointer">New user</summary>
        <div class="field-grid" style="margin-top:8px">
          <div><label for="nu-username">Username</label><input id="nu-username" autocapitalize="none" autocomplete="off"></div>
          <div><label for="nu-display">Display name</label><input id="nu-display" autocomplete="off"></div>
        </div>
        <label for="nu-pass">Password (min 8 characters)</label>
        <input id="nu-pass" type="password" autocomplete="new-password">
        <label style="display:flex;align-items:center;gap:10px;margin-top:10px;font-size:14px;color:var(--text)">
          <input type="checkbox" id="nu-admin" style="width:22px;height:22px;flex:none">
          Admin — can manage stages, users, and removals
        </label>
        <div style="height:8px"></div>
        <button class="primary" id="nu-go" ${S.online ? "" : "disabled"}>Create user</button>
        <p class="warn-text" id="nu-err" hidden></p>
      </details>
    </div>`;
  loadUsers();
}

/* --------------------------------------------------------- office: status -- */

/* The banner an admin has composed but not yet published. Null means "whatever
   is live", so opening the page shows the truth rather than a stale pick from
   an hour ago; both clear once a publish lands.

   The message is held here rather than left in the DOM for the same reason the
   log screen holds its note: a background sync re-renders this view every 30
   seconds, and an empty field is indistinguishable from an untouched one to the
   snapshot logic — so an admin who deleted the message would watch it come
   back. */
let bannerPick = null;
let bannerMsg = null;

/* Both halves of the admin status page.

   The banner is a broadcast: an admin sets it here, every non-admin sees it at
   the top of the app until it is set back to neutral with no message. Admins
   do not get the bar themselves — this card is where they see and change it,
   which is the one place the bar would be redundant.

   The accounts list is the other direction: last sign-in, last logged entry
   and lifetime entry count per account, so a phone that has quietly stopped
   reaching the server shows up as a row that has gone still. Every account is
   listed, admins badged, because an admin who logs work from the floor is as
   worth seeing as anyone else. */
function officeStatus(body) {
  const live = S.ref?.banner || { color: "neutral", message: "" };
  const picked = bannerPick || live.color;

  body.innerHTML = `
    <div class="card">
      <h2>Floor banner</h2>
      <p class="muted" style="margin-bottom:10px">Shown at the top of the app to everyone
      who is not an admin, offline included — the phones keep showing the last banner they
      received. Neutral with no message shows nothing at all.</p>
      <div id="st-live" class="muted"></div>
      <label>Colour</label>
      <div class="seg" id="st-colors">
        ${BANNER_COLORS.map((c) => `
          <button data-id="${c}" class="${c === picked ? "on" : ""}" aria-pressed="${c === picked}">
            ${c === "neutral" ? "Neutral" : c[0].toUpperCase() + c.slice(1)}
          </button>`).join("")}
      </div>
      <label for="st-msg">Message (optional, 200 characters)</label>
      <input id="st-msg" maxlength="200" placeholder="e.g. Paint booth 2 down until Thursday"
             value="${esc(bannerMsg ?? live.message ?? "")}">
      <div style="height:10px"></div>
      <button class="primary" id="st-go" ${S.online ? "" : "disabled"}>
        ${S.online ? "Publish banner" : "Publishing needs a connection"}</button>
      <p class="warn-text" id="st-err" hidden></p>
    </div>

    <div class="card">
      <h2>Accounts</h2>
      <p class="muted" style="margin-bottom:10px">Entries are counted against the person
      they were logged for, and timed by when the work happened on the floor.</p>
      <div id="st-users" class="muted">Loading…</div>
    </div>

    <div class="card">
      <h2>Database backups</h2>
      <div id="st-backup" class="muted">Loading…</div>
    </div>`;

  renderLiveBanner(live);
  loadBackupStatus();

  // Repainted in place rather than re-rendered: nothing else on the page
  // depends on the pick, and a re-render here would reach into the message
  // field the admin is halfway through typing.
  wireSeg("st-colors", (color) => {
    bannerPick = color;
    document.querySelectorAll("#st-colors button").forEach((b) => {
      const on = b.dataset.id === color;
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", String(on));
    });
  });

  document.getElementById("st-msg").oninput = (e) => { bannerMsg = e.target.value; };

  document.getElementById("st-go").onclick = async () => {
    const err = document.getElementById("st-err");
    err.hidden = true;
    try {
      await api("/api/status/banner", {
        method: "POST",
        body: JSON.stringify({
          color: bannerPick || live.color,
          message: (bannerMsg ?? live.message ?? "").trim(),
        }),
      });
      bannerPick = null;
      bannerMsg = null;
      toast("Banner published to the floor");
      sync(); // the banner reaches other phones through the reference payload
    } catch (error) {
      err.textContent = error.body?.detail || "Could not publish the banner.";
      err.hidden = false;
    }
  };

  loadUserStatus();
}

function renderLiveBanner(live) {
  const el = document.getElementById("st-live");
  if (!el) return;
  const showing = live.color !== "neutral" || !!live.message;
  el.innerHTML = showing
    ? `<span class="badge">live</span> <strong>${esc(live.color)}</strong>${live.message ? ` — ${esc(live.message)}` : ""}
       <br>set by ${esc(live.set_by || "someone")} ${live.set_at ? timeAgo(Date.parse(live.set_at)) : ""}`
    : `<span class="badge">live</span> Nothing showing on the floor.`;
}

/* "never" beats a blank cell: an account that has not signed in since this
   shipped is a fact worth reading as one, not an empty space that looks like a
   rendering bug. Relative time on top because that is the question being asked;
   the wall clock underneath because "3d ago" is not something to put in an
   email about it. */
function whenCell(iso) {
  if (!iso) return `<td class="muted">never</td>`;
  const when = new Date(iso);
  return `<td>${timeAgo(when.getTime())}<br><span class="muted">${
    when.toLocaleString([], { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })
  }</span></td>`;
}

/* The backup job runs in its own container, and a failed night only ever
   reached that container's log. A dump is due every 24 hours; a day and a half
   without one is a missed run, said in the warning colour. */
const BACKUP_STALE_MS = 36 * 3600 * 1000;

async function loadBackupStatus() {
  const target = document.getElementById("st-backup");
  if (!target) return;

  let status;
  try {
    status = await api("/api/status/backup");
  } catch {
    if (target.isConnected) target.textContent = "Backup status needs a connection.";
    return;
  }
  if (!target.isConnected) return;

  if (!status.available) {
    target.className = "warn-text";
    target.textContent = "Could not read the backup storage — check the server log.";
  } else if (!status.count) {
    target.className = "warn-text";
    target.textContent = "No backups in storage yet. The first is written about a minute after the stack starts.";
  } else if (!status.last_at) {
    target.className = "muted";
    target.textContent = `${status.count} kept. Newest: ${status.last_key}`;
  } else {
    const when = new Date(status.last_at);
    const stale = Date.now() - when.getTime() > BACKUP_STALE_MS;
    target.className = stale ? "warn-text" : "";
    target.textContent = `Last backup ${timeAgo(when.getTime())} (${when.toLocaleString()}) · ${status.count} kept`
      + (stale ? " — the nightly backup has not run; check the backup container's log." : "");
  }
}

async function loadUserStatus() {
  const target = document.getElementById("st-users");
  if (!target) return;

  let users;
  try {
    ({ users } = await api("/api/status/users"));
  } catch {
    target.textContent = "Account status needs a connection — it is counted from the log on the server.";
    return;
  }
  if (!target.isConnected) return;

  target.classList.remove("muted");
  target.innerHTML = `
    <div class="table-scroll">
    <table>
      <thead><tr>
        <th scope="col">Person</th>
        <th scope="col">Last sign-in</th>
        <th scope="col">Last entry</th>
        <th scope="col" class="num">Entries</th>
      </tr></thead>
      <tbody>
        ${users.map((u) => `
          <tr>
            <td>
              <strong>${esc(u.display_name)}</strong>
              ${u.is_admin ? ` <span class="badge">admin</span>` : ""}${u.is_active ? "" : ` <span class="badge">off</span>`}
              <br><span class="muted">${esc(u.username)}</span>
            </td>
            ${whenCell(u.last_login_at)}
            ${whenCell(u.last_event_at)}
            <td class="num">${u.actions}</td>
          </tr>`).join("")}
      </tbody>
    </table>
    </div>`;
}

/* ----------------------------------------------- stage admin (runtime) -- */

/* The behaviour flags a stage can carry. Labels only — nothing here branches
   on what any particular stage means. */
const STAGE_FLAGS = [
  ["requires_station", "Has physical stations"],
  ["requires_external_po", "External supplier (lead time, PO)"],
  ["allows_partial_qty", "Allows partial quantities (off: a move in takes the whole lot)"],
];

function flagCheckboxes(prefix, values) {
  return STAGE_FLAGS.map(([key, label]) => `
    <label style="display:flex;align-items:center;gap:10px;font-size:14px;color:var(--text);margin:6px 0">
      <input type="checkbox" data-flag="${prefix}:${key}" ${values[key] ? "checked" : ""}
             style="width:22px;height:22px;flex:none">
      ${label}
    </label>`).join("");
}

function readFlags(prefix) {
  const out = {};
  for (const [key] of STAGE_FLAGS) {
    const box = document.querySelector(`[data-flag="${prefix}:${key}"]`);
    if (box) out[key] = box.checked;
  }
  return out;
}

function renderStageAdmin() {
  const target = document.getElementById("mg-stages");
  if (!target) return;

  const stages = [...(S.ref?.stages || [])].sort((a, b) => a.sort_order - b.sort_order);
  target.classList.remove("muted");
  target.innerHTML = stages.map((s) => {
    const stations = (S.ref?.stations || []).filter((st) => st.stage_id === s.id);
    const flagSummary = [
      ...STAGE_FLAGS.filter(([k]) => s[k]).map(([, l]) => l.split(" — ")[0].split(" (")[0]),
      s.max_days_in_state != null ? `ages at ${s.max_days_in_state}d` : "",
    ].filter(Boolean).join(" · ");
    return `
      <details data-panel="stage:${s.id}" style="padding:4px 0;border-bottom:1px solid var(--line)">
        <summary style="cursor:pointer;padding:6px 0">
          <strong>${esc(s.name)}</strong>${s.is_active ? "" : ` <span class="badge">inactive</span>`}
          <br><span class="muted">${esc(flagSummary || "no flags")}${stations.length ? ` · stations: ${stations.map((st) => esc(st.name) + (st.is_active ? "" : " (off)")).join(", ")}` : ""}</span>
        </summary>
        <div style="padding:8px 0 12px">
          <div class="field-grid">
            <div><label>Name</label><input data-stage-name="${s.id}" value="${esc(s.name)}"></div>
            <div><label>Sort order</label><input data-stage-sort="${s.id}" type="number" inputmode="numeric" value="${s.sort_order}"></div>
          </div>
          <label>Flag as aging after (days — empty for never, e.g. outsourced)</label>
          <input data-stage-days="${s.id}" type="number" inputmode="numeric" min="1" max="365"
                 placeholder="no threshold" value="${s.max_days_in_state ?? ""}">
          ${flagCheckboxes(s.id, s)}
          <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap">
            <button class="ghost" data-stage-save="${s.id}" style="width:auto;padding:10px 16px" ${S.online ? "" : "disabled"}>Save</button>
            <button class="ghost" data-stage-toggle="${s.id}" style="width:auto;padding:10px 16px" ${S.online ? "" : "disabled"}>${s.is_active ? "Deactivate" : "Reactivate"}</button>
          </div>
          ${stations.map((st) => `
          <div style="border:1px solid var(--line);border-radius:var(--radius);padding:8px 10px;margin-top:10px">
            <div class="field-grid">
              <div><label>Station${st.is_active ? "" : ` <span class="badge">inactive</span>`}</label><input data-st-name="${st.id}" value="${esc(st.name)}"></div>
              <div><label>Sort order</label><input data-st-sort="${st.id}" type="number" inputmode="numeric" value="${st.sort_order}"></div>
            </div>
            <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap">
              <button class="ghost" data-st-save="${st.id}" style="width:auto;padding:10px 16px" ${S.online ? "" : "disabled"}>Save</button>
              <button class="ghost" data-st-toggle="${st.id}" style="width:auto;padding:10px 16px" ${S.online ? "" : "disabled"}>${st.is_active ? "Deactivate" : "Reactivate"}</button>
            </div>
          </div>`).join("")}
          <label style="margin-top:10px">Add station</label>
          <div class="qty-row">
            <input data-station-name="${s.id}" placeholder="e.g. ${esc(s.name)} ${stations.length + 1}" style="flex:1;text-align:left;padding:10px">
            <button class="ghost" data-station-add="${s.id}" style="width:auto;padding:10px 16px" ${S.online ? "" : "disabled"}>Add</button>
          </div>
        </div>
      </details>`;
  }).join("") || `<span class="muted">No stages yet.</span>`;

  const patchStage = async (stageId, body, doneMessage) => {
    try {
      await api(`/api/stages/${stageId}`, { method: "PATCH", body: JSON.stringify(body) });
      toast(doneMessage);
      await sync(); // refreshes S.ref, re-renders the office view
    } catch (error) {
      alert(error.body?.detail || "Could not update the stage.");
    }
  };

  target.querySelectorAll("[data-stage-save]").forEach((button) => {
    button.onclick = () => {
      const id = button.dataset.stageSave;
      const days = document.querySelector(`[data-stage-days="${id}"]`).value.trim();
      patchStage(id, {
        name: document.querySelector(`[data-stage-name="${id}"]`).value.trim(),
        sort_order: Number(document.querySelector(`[data-stage-sort="${id}"]`).value) || 0,
        max_days_in_state: days ? Number(days) : null,
        ...readFlags(id),
      }, "Stage updated");
    };
  });
  target.querySelectorAll("[data-stage-toggle]").forEach((button) => {
    button.onclick = () => {
      const stage = S.ref.stages.find((s) => s.id === button.dataset.stageToggle);
      patchStage(stage.id, { is_active: !stage.is_active },
        stage.is_active ? "Stage deactivated" : "Stage reactivated");
    };
  });
  const patchStation = async (stationId, body, doneMessage) => {
    try {
      await api(`/api/stations/${stationId}`, { method: "PATCH", body: JSON.stringify(body) });
      toast(doneMessage);
      await sync();
    } catch (error) {
      alert(error.body?.detail || "Could not update the station.");
    }
  };

  target.querySelectorAll("[data-st-save]").forEach((button) => {
    button.onclick = () => {
      const id = button.dataset.stSave;
      const name = document.querySelector(`[data-st-name="${id}"]`).value.trim();
      if (!name) return;
      patchStation(id, {
        name,
        sort_order: Number(document.querySelector(`[data-st-sort="${id}"]`).value) || 0,
      }, "Station updated");
    };
  });
  target.querySelectorAll("[data-st-toggle]").forEach((button) => {
    button.onclick = () => {
      const station = S.ref.stations.find((st) => st.id === button.dataset.stToggle);
      const stage = S.ref.stages.find((s) => s.id === station.stage_id);
      // Deactivating the last one doesn't block logging — the station picker
      // just disappears and entries land without one. Say so rather than
      // forbid it: a stage can legitimately lose its last machine.
      const lastActive = station.is_active && stage?.requires_station && !S.ref.stations.some(
        (st) => st.stage_id === station.stage_id && st.id !== station.id && st.is_active);
      if (lastActive && !confirm(`${station.name} is the last active station at ${stage.name}. Entries logged there will carry no station until one is added back. Deactivate anyway?`)) return;
      patchStation(station.id, { is_active: !station.is_active },
        station.is_active ? "Station deactivated" : "Station reactivated");
    };
  });
  target.querySelectorAll("[data-station-add]").forEach((button) => {
    button.onclick = async () => {
      const id = button.dataset.stationAdd;
      const name = document.querySelector(`[data-station-name="${id}"]`).value.trim();
      if (!name) return;
      try {
        await api("/api/stations", {
          method: "POST",
          body: JSON.stringify({ stage_id: id, code: slugify(name), name }),
        });
        toast("Station added");
        await sync();
      } catch (error) {
        alert(error.body?.detail || "Could not add the station.");
      }
    };
  });

  // ---- new stage form ----
  const flags = document.getElementById("ns-flags");
  if (flags && !flags.innerHTML) {
    flags.innerHTML = flagCheckboxes("new", { allows_partial_qty: true });
    const maxSort = Math.max(0, ...stages.map((s) => s.sort_order));
    document.getElementById("ns-sort").value = maxSort + 10;
  }
  const go = document.getElementById("ns-go");
  if (go) go.onclick = async () => {
    const err = document.getElementById("ns-err");
    err.hidden = true;
    const name = document.getElementById("ns-name").value.trim();
    if (!name) { err.textContent = "The stage needs a name."; err.hidden = false; return; }
    try {
      const days = document.getElementById("ns-days").value.trim();
      await api("/api/stages", {
        method: "POST",
        body: JSON.stringify({
          code: slugify(name),
          name,
          sort_order: Number(document.getElementById("ns-sort").value) || 0,
          max_days_in_state: days ? Number(days) : null,
          ...readFlags("new"),
        }),
      });
      toast(`Stage ${name} added — items can be logged there now`);
      await sync();
    } catch (error) {
      err.textContent = error.body?.detail || "Could not add the stage.";
      err.hidden = false;
    }
  };
}

/* ---------------------------------------------------------- user admin -- */

async function loadUsers() {
  const target = document.getElementById("mg-users");
  if (!target) return;

  let users;
  try {
    ({ users } = await api("/api/users"));
  } catch {
    target.textContent = "User management needs a connection.";
    return;
  }
  if (!target.isConnected) return;

  target.classList.remove("muted");
  target.innerHTML = users.map((u) => `
    <details data-panel="user:${u.id}" style="padding:4px 0;border-bottom:1px solid var(--line)">
      <summary style="cursor:pointer;padding:6px 0">
        <strong>${esc(u.display_name)}</strong> <span class="muted">${esc(u.username)}</span>
        ${u.is_admin ? `<span class="badge">admin</span>` : ""}${u.is_active ? "" : ` <span class="badge">deactivated</span>`}
        ${u.id === S.user?.id ? ` <span class="badge qty">you</span>` : ""}
      </summary>
      <div style="padding:8px 0 12px">
        <label>Display name</label>
        <input data-user-display="${u.id}" value="${esc(u.display_name)}">
        <label>Reset password (signs them out everywhere)</label>
        <div class="qty-row">
          <input data-user-pass="${u.id}" type="password" autocomplete="new-password"
                 placeholder="min 8 characters" style="flex:1;text-align:left;padding:10px">
          <button class="ghost" data-user-reset="${u.id}" style="width:auto;padding:10px 16px">Reset</button>
        </div>
        <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
          <button class="ghost" data-user-save="${u.id}" style="width:auto;padding:10px 16px">Save name</button>
          ${u.id === S.user?.id ? "" : `
          <button class="ghost" data-user-admin="${u.id}" style="width:auto;padding:10px 16px">${u.is_admin ? "Remove admin" : "Make admin"}</button>
          <button class="ghost" data-user-active="${u.id}" style="width:auto;padding:10px 16px">${u.is_active ? "Deactivate" : "Reactivate"}</button>`}
        </div>
      </div>
    </details>`).join("");

  const patchUser = async (id, body, doneMessage) => {
    try {
      await api(`/api/users/${id}`, { method: "PATCH", body: JSON.stringify(body) });
      toast(doneMessage);
      loadUsers();
    } catch (error) {
      alert(error.body?.detail || "Could not update the user.");
    }
  };

  // This list lands after render() has already restored the view, so it has to
  // put the user's open panels and half-typed names back itself.
  restoreView(lastSnapshot);

  target.querySelectorAll("[data-user-save]").forEach((b) => {
    b.onclick = () => patchUser(b.dataset.userSave, {
      display_name: target.querySelector(`[data-user-display="${b.dataset.userSave}"]`).value.trim(),
    }, "Name updated");
  });
  target.querySelectorAll("[data-user-reset]").forEach((b) => {
    b.onclick = () => {
      const field = target.querySelector(`[data-user-pass="${b.dataset.userReset}"]`);
      if (field.value.length < 8) { alert("Password needs at least 8 characters."); return; }
      patchUser(b.dataset.userReset, { password: field.value }, "Password reset — their old sessions are signed out");
    };
  });
  target.querySelectorAll("[data-user-admin]").forEach((b) => {
    const user = users.find((u) => u.id === b.dataset.userAdmin);
    b.onclick = () => patchUser(user.id, { is_admin: !user.is_admin },
      user.is_admin ? "Admin removed" : "Now an admin");
  });
  target.querySelectorAll("[data-user-active]").forEach((b) => {
    const user = users.find((u) => u.id === b.dataset.userActive);
    b.onclick = () => {
      if (user.is_active && !confirm(`Deactivate ${user.display_name}? They are signed out on their next request; their logged events stay.`)) return;
      patchUser(user.id, { is_active: !user.is_active },
        user.is_active ? "User deactivated" : "User reactivated");
    };
  });

  const go = document.getElementById("nu-go");
  if (go) go.onclick = async () => {
    const err = document.getElementById("nu-err");
    err.hidden = true;
    try {
      const created = await api("/api/users", {
        method: "POST",
        body: JSON.stringify({
          username: document.getElementById("nu-username").value.trim().toLowerCase(),
          display_name: document.getElementById("nu-display").value.trim(),
          password: document.getElementById("nu-pass").value,
          is_admin: document.getElementById("nu-admin").checked,
        }),
      });
      toast(`User ${created.username} created`);
      loadUsers();
    } catch (error) {
      const detail = error.body?.detail;
      err.textContent = typeof detail === "string" ? detail
        : Array.isArray(detail) && detail.length ? `${(detail[0].loc || []).slice(1).join(".")}: ${detail[0].msg}`
        : "Could not create the user.";
      err.hidden = false;
    }
  };
}

/* ----------------------------------------------------------------- boot -- */

async function boot() {
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
    // The worker pings after a Background Sync drain so an open page's pill
    // catches up immediately.
    navigator.serviceWorker.addEventListener("message", (e) => {
      if (e.data === "queue-drained") sync();
    });
    // A new version of the app has taken over the page's requests while this
    // page is still running the old script against it. Reload onto the new
    // one -- but never out from under someone's thumb: not while a field has
    // focus, and the queue is in IndexedDB, so nothing logged is lost.
    let controlled = !!navigator.serviceWorker.controller;
    navigator.serviceWorker.addEventListener("controllerchange", () => {
      if (!controlled) { controlled = true; return; } // first install, nothing to replace
      const reload = () => {
        const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement?.tagName || "");
        if (typing) { window.addEventListener("hashchange", reload, { once: true }); return; }
        location.reload();
      };
      reload();
    });
  }
  // Unsynced events must not sit in storage the browser considers evictable.
  if (navigator.storage?.persist) navigator.storage.persist().catch(() => {});

  try {
    // Come up from cache first so the app is usable before any network round trip.
    S.ref = (await LSF_DB.get("reference")) || null;
    S.items = (await LSF_DB.get("items")) || [];
    S.itemsMeta = (await LSF_DB.get("itemsMeta")) || { token: null, at: 0 };
    S.user = (await LSF_DB.get("user")) || null;
    S.lastSync = (await LSF_DB.get("lastSync")) || null;
    S.pending = await LSF_DB.queueAll();
  } catch (error) {
    // IndexedDB refused to open (private browsing, corruption, storage full).
    // A blank white page tells the engineer nothing; this at least says why
    // the app can't keep its offline promise on this browser.
    document.getElementById("topbar").hidden = false;
    $view.innerHTML = `
      <div class="card">
        <h2>Storage unavailable</h2>
        <p class="muted">This browser blocked the on-device database the app needs for
        offline logging (private browsing does this). Entries cannot be queued safely —
        close this tab and open the app in a normal browser window.</p>
      </div>`;
    return;
  }
  renderPill();
  render();
  sync();
}

boot();
