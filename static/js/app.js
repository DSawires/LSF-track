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
  (a.occurred_at < b.occurred_at ? -1 : a.occurred_at > b.occurred_at ? 1
    // The entries of one split or assembly share a moment; keep them together.
    : (a.link_id || "") < (b.link_id || "") ? -1 : (a.link_id || "") > (b.link_id || "") ? 1 : 0);

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
    // A split or an assembly is several entries stored all together or not
    // at all, so a chunk never ends in the middle of one.
    let chunk = queue.slice(0, BATCH_CHUNK);
    const links = new Set(chunk.map((q) => q.link_id).filter(Boolean));
    if (links.size) chunk = chunk.concat(queue.slice(BATCH_CHUNK).filter((q) => links.has(q.link_id)));
    const events = chunk.map(({ _queued_at, _status, _acked_at, _reason, ...event }) => event);
    let results;
    try {
      ({ results } = await api("/api/events/batch", {
        method: "POST",
        body: JSON.stringify({ events }),
      }));
    } catch (error) {
      // A batch refused as a whole falls back to one-at-a-time posting: one
      // malformed entry fails request validation for all 100, and the poison
      // entry must quarantine alone instead of wedging the queue. Anything
      // else -- no network, a 5xx, a proxy's 403 or 429 -- says nothing about
      // the entries, and the whole batch is retried next sync.
      if (!aboutTheEntries(error)) throw error;
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
      toast(tn(divergent, "{n} entry already existed with different details", "{n} entries already existed with different details"));
    }
    if (!progressed) return; // all errors: stop looping, wait for the next sync
  }
}

/* Is this answer a verdict on what was sent? 422 is the server refusing an
   entry (or a batch it cannot parse); 413 is a batch too big to take whole.
   Every other failure is about the connection, the session or something in
   between -- a 403 from the origin check after a proxy change, a 429 -- and an
   entry must never be quarantined for one of those: that would mark the whole
   queue "not accepted" on every phone for a fault that is nobody's entry. */
const aboutTheEntries = (error) => error.status === 422 || error.status === 413;

async function drainOneByOne(events) {
  const results = [];
  // Linked entries go together even here: one alone is always refused.
  const groups = new Map();
  for (const event of events) {
    if (event.link_id) groups.set(event.link_id, [...(groups.get(event.link_id) || []), event]);
  }
  for (const group of groups.values()) {
    try {
      const answer = await api("/api/events/batch", { method: "POST", body: JSON.stringify({ events: group }) });
      results.push(...answer.results);
    } catch (error) {
      if (error.status !== 422) throw error;
      for (const event of group) results.push({ id: event.id, status: "rejected", reason: rejectReason(error) });
    }
  }
  for (const event of events.filter((e) => !e.link_id)) {
    try {
      const { divergent } = await api("/api/events", {
        method: "POST",
        body: JSON.stringify(event),
      });
      results.push({ id: event.id, status: "stored", divergent });
    } catch (error) {
      if (error.status !== 422) throw error;
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
  return t("rejected by server");
}

async function sync() {
  if (syncRunning) return;
  syncRunning = true;
  try {
    // Signed out while there was no signal: finish the job now, before
    // anything else is sent under a session that was meant to have ended.
    if (!S.user) {
      try {
        if (await LSF_DB.get("logoutOwed")) {
          await api("/api/auth/logout", { method: "POST" });
          await LSF_DB.put("logoutOwed", false);
        }
      } catch {
        // Still no signal (or no storage): it stays owed.
      }
    }
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
      // Entries the server has taken are kept ("acked") and still drawn on
      // the cards until the list has been fetched again: this list predates
      // them. So while any are held the list is fetched whatever the token
      // says, and they go the moment a list fetched after them is in place --
      // not before, or the units jump back; not after, or they count twice.
      const fetchStarted = Date.now();
      const holding = (await LSF_DB.queueAll()).some((q) => q._status === "acked");
      const current = !holding
        && !!S.itemsMeta.token
        && S.itemsMeta.token === S.ref.items_token
        && Date.now() - S.itemsMeta.at < ITEMS_MAX_AGE_MS;
      if (!current) {
        const itemsPayload = await apiLive("/api/items");
        // Which entries this list already contains: read from storage, where
        // the drain marked them -- the copy in memory still says "pending".
        const reflected = new Set((await LSF_DB.queueAll())
          .filter((q) => q._status === "acked" && (q._acked_at || 0) <= fetchStarted)
          .map((q) => q.id));
        // No await between these two: nothing may draw the new list with the
        // old entries still on top of it.
        S.items = itemsPayload.items;
        S.pending = S.pending.filter((q) => !reflected.has(q.id));
        S.itemsMeta = { token: itemsPayload.token || null, at: Date.now() };
        // On disk, the list first and the entries after. Stopped in between,
        // the next start draws a move twice until its first sync; the other
        // order would draw it not at all, which is the one that gets a move
        // logged a second time.
        await LSF_DB.put("items", S.items);
        await LSF_DB.put("itemsMeta", S.itemsMeta);
        await LSF_DB.dropAcked(fetchStarted);
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
  // A refused entry needs someone to act; a pending one only needs signal.
  // They must not look alike.
  $pill.classList.toggle("rejected", rejected > 0);
  const parts = [];
  if (pending) parts.push(t("{n} pending", { n: pending }));
  if (rejected) parts.push(t("{n} not accepted", { n: rejected }));
  if (!S.online) parts.push(t("offline"));
  else if (S.needsLogin) parts.push(t("sign-in needed"));
  parts.push(S.lastSync ? t("synced {when}", { when: timeAgo(S.lastSync) }) : t("never synced"));
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
  el.innerHTML = `<span class="label">${esc(bannerColorName(banner.color))}</span>` +
    (banner.message ? ` ${esc(banner.message)}` : "");
}

/* The banner's colour, in words. The label is what makes the colour readable
   to someone who cannot tell it apart, so it is said in the reader's language. */
const BANNER_COLOR_NAMES = { green: N_("Green"), yellow: N_("Yellow"), red: N_("Red"), neutral: N_("Neutral") };
const bannerColorName = (color) => t(BANNER_COLOR_NAMES[color] || BANNER_COLOR_NAMES.neutral);

function timeAgo(timestamp) {
  const minutes = Math.round((Date.now() - timestamp) / 60000);
  if (minutes < 1) return t("just now");
  if (minutes < 60) return t("{n}m ago", { n: minutes });
  const hours = Math.round(minutes / 60);
  return hours < 24 ? t("{n}h ago", { n: hours }) : t("{n}d ago", { n: Math.round(hours / 24) });
}

/* "5 Nov, 03:49": the day first and the month by name, so a date cannot be read
   two ways (11/5 is November to one reader and May to the next), and no
   seconds. The year only when it is not this one. */
function fmtWhen(value) {
  const when = new Date(value);
  if (Number.isNaN(when.getTime())) return "";
  const options = { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" };
  if (when.getFullYear() !== new Date().getFullYear()) options.year = "numeric";
  return when.toLocaleString(LSF_I18N.locale(), options);
}

/* "1 pc", "40 pcs". */
const pcs = (n) => tn(n, "{n} pc", "{n} pcs");

/* "rev C": a drawing revision, beside a code. */
const revLabel = (rev) => t("rev {rev}", { rev });

/* "2.5d": days at a place, on the cards and in the aging report. */
const daysLabel = (days) => t("{n}d", { n: days.toFixed(1) });

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
  return logEvents([event]);
}

/* Several entries that belong together -- a split, an assembly -- queued
   at once. */
async function logEvents(events) {
  try {
    for (const event of events) await LSF_DB.enqueue(event);
  } catch {
    // IndexedDB refused the write (quota, private browsing, corruption). The
    // one thing worse than an error is pretending the entry was recorded.
    toast(t("NOT saved — device storage failed"));
    return false;
  }
  S.pending = await LSF_DB.queueAll();
  renderPill();
  toast(t("Logged ✓"));
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

/* A picture's small copy, for icons and grids: a few KB where the picture is a
   few hundred. The server falls back to the whole picture for one stored
   before thumbnails existed. */
const thumbOf = (url) => (url ? `${url}/thumb` : url);

/* After deleting a picture: this device must not go on serving it out of its
   own image cache, where picture URLs are kept for good. Other devices hold a
   copy until theirs trims -- the price of photos that load in a dead spot. */
function forgetImage(url) {
  for (const each of [url, thumbOf(url)]) {
    navigator.serviceWorker?.controller?.postMessage({ type: "drop-image", url: each });
  }
}

/* A stage's or station's name as this phone reads it: the Arabic one when the
   phone is set to Arabic and the factory has given one, the name otherwise.
   Names are data (rule 2), so they are never run through t(). */
const localName = (row) => (row && LSF_I18N.lang() === "ar" && row.name_ar) || row?.name || "";
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
// The other side of a linked entry: another item (a split, an assembly, or
// another phase's batch of the same code).
const LINK = "link";

/* Parts. A part is an item of its own, tracked until it is assembled back
   into the piece it belongs to. */
const partsOf = (item) => S.items.filter((i) => i.parent_item_id === item.id && i.is_active !== false);
const pieceOf = (item) => (item?.parent_item_id ? S.items.find((i) => i.id === item.parent_item_id) || null : null);
const partName = (part) => part.description || part.code;
// An item with every part under it, and theirs: what one card shows.
const familyOf = (item) => [item, ...partsOf(item).flatMap(familyOf)];

/* "216 pcs" for a batch -- units moved between phases included -- and for a
   part, how many go into each piece of what it belongs to. */
function batchLabel(item) {
  if (item.parent_item_id) {
    const piece = pieceOf(item);
    return piece ? t("×{n} per {code}", { n: item.per_unit, code: piece.code }) : t("×{n} per piece", { n: item.per_unit });
  }
  return pcs(item.state?.batch_qty ?? item.total_qty);
}

function placeName(key) {
  if (key === UNSTARTED) return t("Not started");
  if (key === DONE) return t("Done");
  return localName(stageById(key)) || "?";
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
  if (!projectId) options.sort((x, y) => x.name.localeCompare(y.name, LSF_I18N.locale()));
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
  const { places, done, inParts } = localPlaces(item);
  // Pieces out in parts are being worked on, wherever the parts are.
  const started = done > 0 || inParts > 0 || places.some((p) => p.key !== UNSTARTED);
  if (!started) return "unstarted";
  if (inParts > 0) return "progress";
  return places.length ? "progress" : "finished";
}
const PROGRESS = [
  ["unstarted", N_("Haven't started")],
  ["progress", N_("In progress")],
  ["finished", N_("Finished")],
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
  return q.from_link ? LINK : q.from_unstarted ? UNSTARTED : q.from_done ? DONE : q.from_stage_id || null;
}
function queuedTarget(q) {
  if (q.to_link) return LINK;
  return eventType(q.event_type_id)?.is_finish ? DONE : q.stage_id || (q.from_link ? UNSTARTED : null);
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
  let inParts = state?.in_parts_qty || 0;
  // Released by another item's sample: nothing logged here releases it.
  const waits = !!state?.waits_on_sample;
  const sampleQty = state?.sample_qty || 0;

  // Pending, and taken by the server but newer than the list in hand: both
  // are moves this list does not show yet.
  const queue = S.pending
    .filter((q) => (q._status === "pending" || q._status === "acked") && q.item_id === item.id)
    .sort(byOccurredAt);
  for (const q of queue) {
    const type = eventType(q.event_type_id);
    if (!type?.moves_quantity || !q.qty) continue;
    const from = queuedSource(q);
    const to = queuedTarget(q);
    if (!from || !to) continue;
    let moved;
    let movedSample = 0;
    if (from === LINK) {
      // Arriving from another item: split off the piece, or assembled.
      moved = q.qty;
      inParts = Math.max(0, inParts - moved);
    } else {
      const source = from === DONE ? null : at.get(from);
      // Never more than is there: an over-quantity entry was warned about when
      // it was logged and the server will refuse it, so it must not invent units.
      moved = Math.min(q.qty, from === DONE ? done : source?.qty || 0);
      if (!moved) continue;
      if (from === DONE) {
        done -= moved;
      } else {
        source.qty -= moved;
        movedSample = Math.min(moved, source.sample_qty);
        source.sample_qty -= movedSample;
        source.queued = true;
      }
    }
    if (to === LINK) {
      inParts += moved;
    } else if (to === DONE) {
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
    if (inSample && !waits && done >= sampleQty) {
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
  return { places, done, inSample, inParts };
}

function pendingCountFor(itemId) {
  return S.pending.filter((q) => q._status === "pending" && q.item_id === itemId).length;
}

function rejectedCountFor(itemId) {
  return S.pending.filter((q) => q._status === "rejected" && q.item_id === itemId).length;
}

/* "Carpentry", "Not started", with the sample called out: until it is done,
   the sample is the only thing on the floor for that item. */
function whereLabel(p) {
  return p.sample_qty ? `${p.name} · ${t("sample")}` : p.name;
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

/* Every view is drawn into the one scrolling element, so where it is scrolled
   to has to be decided on each change of screen, or the new screen inherits
   the old one's offset: a batch tapped far down the list opened its log screen
   part-way down, with the item's name off the top. A screen opens at its top.
   The item list is the exception -- it is what people come back to after every
   entry, and it comes back where they left it. */
let renderedHash = null;
const listScroll = { top: 0 };
const isItemList = (hash) => hash === "#/items" || hash === "#/items/";

function render(background = false) {
  const hash = location.hash || "#/items";
  const moved = renderedHash !== null && hash !== renderedHash;
  if (moved && isItemList(renderedHash)) listScroll.top = $view.scrollTop;
  document.getElementById("topbar").hidden = false;
  document.getElementById("tabs").hidden = !S.user;
  // Before every early return below: the banner lives in the shell, not the
  // view, and must not depend on whether this particular render went ahead.
  renderStatusBanner();

  // Only force the login screen when there is no cached identity to work
  // with. A session expiring mid-shift must NOT lock an engineer out of an
  // offline-first app: they keep logging from cache (the banner explains),
  // and the queue drains after the next successful sign-in. That engineer
  // still has `S.user`. Nobody signed in at all -- a fresh phone, or one
  // someone signed out of -- gets the login screen and nothing else: with the
  // lists cached, this used to let the app be reopened signed out, and
  // whatever was logged then was credited to whoever signed in next.
  if (!S.user) {
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
  else if (route === "office") viewOffice(arg, arg2);
  else viewItems();

  restoreView(lastSnapshot);
  // Scroll is restored here and not in restoreView(), because the async lists
  // call that back much later — by which time the user may have scrolled and
  // would not thank us for putting them back.
  if (background && lastSnapshot.scrollTop) $view.scrollTop = lastSnapshot.scrollTop;
  if (moved) $view.scrollTop = isItemList(hash) ? listScroll.top : 0;
  renderedHash = hash;
  renderedFingerprint = fingerprint;
}

/* ------------------------------------------------------------- language -- */

/* English / العربية, on the sign-in screen and the account screen. Each name
   is written in its own language, so whoever cannot read the one in use can
   still find theirs. The choice belongs to the phone (see i18n.js). */
function langSwitch() {
  const now = LSF_I18N.lang();
  const button = (code, name) =>
    `<button type="button" class="ghost${code === now ? " on" : ""}" data-lang="${code}" lang="${code}" aria-pressed="${code === now}">${name}</button>`;
  return `<div class="lang-switch" role="group" aria-label="${esc(t("Language"))}">${button("en", "English")}${button("ar", "العربية")}</div>`;
}

function bindLangSwitch() {
  $view.querySelectorAll("[data-lang]").forEach((button) => {
    button.onclick = () => {
      if (button.dataset.lang === LSF_I18N.lang()) return;
      LSF_I18N.setLang(button.dataset.lang);
      renderPill();
      render();
    };
  });
}

/* ---------------------------------------------------------------- login -- */

function viewLogin() {
  document.getElementById("tabs").hidden = true;
  const pendingCount = S.pending.filter((q) => q._status === "pending").length;
  $view.innerHTML = `
    <div class="login-wrap">
      <h2 style="text-align:center;font-size:22px;margin-bottom:12px">Life Style Track</h2>
      <form class="card" id="login-form">
        <label for="login-user">${t("Username")}</label>
        <input id="login-user" autocapitalize="none" autocomplete="username" enterkeyhint="next" dir="ltr" required>
        <label for="login-pass">${t("Password")}</label>
        <input id="login-pass" type="password" autocomplete="current-password" enterkeyhint="go" required>
        <div style="height:14px"></div>
        <button class="primary" id="login-go" type="submit">${t("Sign in")}</button>
        <p id="login-err" class="warn-text" role="alert" hidden></p>
        ${pendingCount ? `<p class="muted" style="margin-top:10px">${tn(pendingCount, "{n} queued entry is safe on this device and will sync after sign-in.", "{n} queued entries are safe on this device and will sync after sign-in.")}</p>` : ""}
      </form>
      ${langSwitch()}
    </div>`;
  bindLangSwitch();
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
      // A sign-out still owed from before is void: it would end this session.
      await LSF_DB.put("logoutOwed", false).catch(() => {});
      document.getElementById("tabs").hidden = false;
      navigate("#/items");
      sync();
    } catch (error) {
      // "Wrong password" when the real problem is no signal is an infuriating
      // dead end on the floor — name the actual failure.
      if (error.auth) err.textContent = t("Wrong username or password.");
      else if (error.status === 429) err.textContent = error.body?.detail || t("Too many attempts — wait a minute and try again.");
      else if (error.status) err.textContent = t("The server had a problem — try again shortly.");
      else err.textContent = t("No connection. Sign-in needs the network; queued entries are safe and will sync later.");
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
      type?.name || t("entry"),
      from && to ? `${placeName(from)} → ${placeName(to)}` : to ? placeName(to) : "",
      q.qty ? pcs(q.qty) : "",
    ].filter(Boolean).join(" · ");
    return { code: item ? itemName(item) : t("unknown item"), what, when: fmtWhen(q._queued_at || Date.now()) };
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
          <button class="ghost" data-retry="${q.id}" style="width:auto;padding:8px 16px">${t("Retry")}</button>
          <button class="ghost" data-discard="${q.id}" style="width:auto;padding:8px 16px">${t("Discard")}</button>
        </div>` : ""}
      </div>`;
  };

  $view.innerHTML = `
    <div class="card">
      <div class="spread">
        <span>${t("Signed in as")} <strong>${esc(S.user?.display_name || S.user?.username || t("nobody"))}</strong>${S.user?.is_admin ? ` <span class="badge">${t("admin")}</span>` : ""}</span>
        ${S.user ? `<button class="ghost" id="sign-out" style="width:auto;padding:8px 16px;flex:none">${t("Sign out")}</button>` : ""}
      </div>
      ${langSwitch()}
    </div>

    <div class="card">
      <h2>${t("Waiting to sync ({n})", { n: pending.length })}</h2>
      ${pending.map((q) => entryRow(q, false)).join("") || `<p class="muted">${rejected.length
        ? t("Nothing waiting — but see below: not everything was accepted.")
        : t("Nothing waiting — everything has reached the server.")}</p>`}
      <p class="muted" style="margin-top:8px">${S.lastSync ? t("Last synced {when}.", { when: timeAgo(S.lastSync) }) : t("Never synced from this device.")}${S.online ? "" : ` ${t("Currently offline; entries are safe here until the network returns.")}`}</p>
    </div>

    ${rejected.length ? `
    <div class="card">
      <h2>${t("Not accepted ({n})", { n: rejected.length })}</h2>
      <p class="muted" style="margin-bottom:6px">${t("These are not in the log. Retry if things have changed since — the entry it depends on may have synced from another phone. Discard it if it was a mistake, and log it again correctly.")}</p>
      ${rejected.map((q) => entryRow(q, true)).join("")}
    </div>` : ""}`;
  bindLangSwitch();

  // A split or an assembly was refused as a group, and goes back (or goes)
  // as one: a single row of it on its own is never stored.
  const groupOf = (id) => {
    const entry = S.pending.find((q) => q.id === id);
    return entry?.link_id ? S.pending.filter((q) => q.link_id === entry.link_id).map((q) => q.id) : [id];
  };
  $view.querySelectorAll("[data-retry]").forEach((button) => {
    button.onclick = async () => {
      for (const id of groupOf(button.dataset.retry)) await LSF_DB.markPending(id);
      S.pending = await LSF_DB.queueAll();
      renderPill();
      render();
      sync();
    };
  });
  $view.querySelectorAll("[data-discard]").forEach((button) => {
    button.onclick = async () => {
      const ids = groupOf(button.dataset.discard);
      if (!confirm(ids.length > 1
        ? t("Discard this split or assembly ({n} entries together)? It was never stored on the server and will be gone for good.", { n: ids.length })
        : t("Discard this entry? It was never stored on the server and will be gone for good."))) return;
      for (const id of ids) await LSF_DB.dropRejected(id);
      S.pending = await LSF_DB.queueAll();
      renderPill();
      render();
    };
  });

  const signOut = document.getElementById("sign-out");
  if (signOut) signOut.onclick = async () => {
    const waiting = pending.length;
    if (waiting && !confirm(tn(waiting,
      "{n} entry has not synced yet. It stays on this device and will sync after the next sign-in. Sign out anyway?",
      "{n} entries have not synced yet. They stay on this device and will sync after the next sign-in. Sign out anyway?"))) return;
    try {
      await api("/api/auth/logout", { method: "POST" });
    } catch {
      // Offline: the server cookie outlives this, but the local identity and
      // cached data still clear so the next user starts clean. The sign-out
      // itself is owed to the server, and sync() sends it when it can.
      await LSF_DB.put("logoutOwed", true).catch(() => {});
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
  // The same goes for a stage taken off the floor.
  const filterStage = validFilter("f-stage", stages);
  const inProject = S.items.filter((item) => !filterProject || item.project_id === filterProject);
  const areas = labelOptions("areas", "area_id", inProject, t("No area"), filterProject);
  const phases = labelOptions("phases", "phase_id", inProject, t("No phase"), filterProject);
  const filterArea = validFilter("f-area", areas);
  const filterPhase = validFilter("f-phase", phases);
  const filterProgress = validFilter("f-progress", PROGRESS.map(([id]) => ({ id })));

  const matches = (item, text) => {
    if (filterProject && item.project_id !== filterProject) return false;
    if (!matchesLabel(item, "area_id", filterArea)) return false;
    if (!matchesLabel(item, "phase_id", filterPhase)) return false;
    if (filterProgress && progressOf(item) !== filterProgress) return false;
    // Its parts are on its card, so a card matches by any of them: "at paint"
    // means the piece or one of its parts rests at paint right now, queued
    // moves included, and a search finds a piece by a part's name.
    const family = familyOf(item);
    if (filterStage && !family.some((member) => localPlaces(member).places.some((p) => p.key === filterStage))) return false;
    return !text || family.some((member) => `${itemName(member)} ${member.description}`.toLowerCase().includes(text));
  };
  // A part has no card of its own: it is listed under the piece it belongs to.
  const list = (text) =>
    S.items.filter((item) => !pieceOf(item) && matches(item, text)).map(itemRow).join("")
    || `<p class="muted">${t("No items match.")}</p>`;

  // Search is how a batch is found among a few hundred; the filters are the
  // slower second tool, and five dropdowns above every list cost a quarter of
  // a phone screen. They fold away behind one button, and what is switched on
  // stays in sight while they are folded -- a filter nobody can see is a list
  // that is quietly missing things.
  const active = [
    projects.find((p) => p.id === filterProject)?.code,
    localName(stages.find((s) => s.id === filterStage)),
    areas.find((a) => a.id === filterArea)?.name,
    phases.find((ph) => ph.id === filterPhase)?.name,
    filterProgress ? t(PROGRESS.find(([id]) => id === filterProgress)?.[1] || "") : "",
  ].filter(Boolean);
  const filtersOpen = sessionStorage.getItem("f-open") === "1";

  $view.innerHTML = `
    <div class="find-row">
      <input id="f-q" type="search" placeholder="${esc(t("Search code or description"))}"
             value="${esc(sessionStorage.getItem("f-q") || "")}">
      <button class="ghost${active.length ? " on" : ""}" id="f-toggle" aria-expanded="${filtersOpen}" aria-controls="f-panel">${t("Filters")}${active.length ? ` · ${active.length}` : ""}</button>
    </div>
    ${active.length && !filtersOpen ? `
    <div class="filter-summary">
      <span class="muted">${t("Showing only")} ${esc(active.join(" · "))}</span>
      <button class="ghost" id="f-clear">${t("Show all")}</button>
    </div>` : ""}
    <div class="filters" id="f-panel" ${filtersOpen ? "" : "hidden"}>
      <select id="f-project" aria-label="${esc(t("Project"))}">
        <option value="">${t("All projects")}</option>
        ${projects.map((p) => `<option value="${p.id}" ${p.id === filterProject ? "selected" : ""}>${esc(p.code)}</option>`).join("")}
      </select>
      <select id="f-stage" aria-label="${esc(t("Stage"))}">
        <option value="">${t("All stages")}</option>
        ${stages.map((s) => `<option value="${s.id}" ${s.id === filterStage ? "selected" : ""}>${esc(localName(s))}</option>`).join("")}
      </select>
      ${areas.length ? `
      <select id="f-area" aria-label="${esc(t("Area"))}">
        <option value="">${t("All areas")}</option>
        ${areas.map((a) => `<option value="${a.id}" ${a.id === filterArea ? "selected" : ""}>${esc(a.name)}</option>`).join("")}
      </select>` : ""}
      ${phases.length ? `
      <select id="f-phase" aria-label="${esc(t("Phase"))}">
        <option value="">${t("All phases")}</option>
        ${phases.map((ph) => `<option value="${ph.id}" ${ph.id === filterPhase ? "selected" : ""}>${esc(ph.name)}</option>`).join("")}
      </select>` : ""}
      <select id="f-progress" aria-label="${esc(t("Progress"))}">
        <option value="">${t("Any progress")}</option>
        ${PROGRESS.map(([id, name]) => `<option value="${id}" ${id === filterProgress ? "selected" : ""}>${t(name)}</option>`).join("")}
      </select>
    </div>
    <div class="row-list">
      ${list((sessionStorage.getItem("f-q") || "").toLowerCase())}
    </div>`;

  document.getElementById("f-toggle").onclick = () => {
    sessionStorage.setItem("f-open", filtersOpen ? "0" : "1"); render();
  };
  const $clear = document.getElementById("f-clear");
  if ($clear) $clear.onclick = () => {
    for (const id of ["f-project", "f-stage", "f-area", "f-phase", "f-progress"]) {
      sessionStorage.removeItem(id);
      // Emptied here too: the re-render carries a field's value across, and
      // would put the old choice back into a dropdown that no longer filters.
      const select = document.getElementById(id);
      if (select) select.value = "";
    }
    render();
  };
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

/* The rows of one item's units: a tap target per place holding some, and Done. */
function placeRows(item, places, done) {
  return places.map((p) => `
    <a class="pos-row" href="#/items/${item.id}/${p.key}"
       aria-label="${esc(t("Move {qty} of {code} from {place}", { qty: pcs(p.qty), code: item.code, place: whereLabel(p) }))}">
      <span class="muted" style="min-width:0">${esc(whereLabel(p))}${p.reworked_qty ? ` <span class="badge rework" title="${esc(t("Reworked"))}">${t("R")}</span>` : ""}</span>
      <span style="white-space:nowrap;display:flex;align-items:center;gap:6px">
        <span class="badge qty">${p.qty}</span>${p.key === UNSTARTED ? "" : p.days_in_state == null ? `
        <span class="badge pending">${t("new")}</span>` : `
        <span class="badge${p.overdue ? " age-hot" : ""}">${daysLabel(p.days_in_state)}</span>`}
        <span class="chev" aria-hidden="true">›</span>
      </span>
    </a>`).join("") + (done ? `
      <a class="pos-row" href="#/items/${item.id}/${DONE}" style="border-inline-start:2px solid var(--good)"
         aria-label="${esc(t("{qty} of {code} done", { qty: pcs(done), code: item.code }))}">
        <span class="muted">${t("Done")}</span>
        <span style="white-space:nowrap;display:flex;align-items:center;gap:6px">
          <span class="badge qty" style="color:var(--good)">${done}</span>
          <span class="chev" aria-hidden="true">›</span>
        </span>
      </a>` : "");
}

/* An item's parts, under it on its own card: each part with the places its
   units are at, tapped and moved exactly like the piece's own. Parts of parts
   nest one step further in. */
function partBlocks(item) {
  return partsOf(item).map((part) => {
    const { places, done } = localPlaces(part);
    return `
      <div class="part-block">
        <a class="part-head" href="#/items/${part.id}">
          <span><strong>${esc(partName(part))}</strong> <span class="muted">${t("×{n} per piece", { n: part.per_unit })}</span></span>
          <span class="muted">${esc(part.code)}</span>
        </a>
        ${placeRows(part, places, done) || `<div class="muted part-empty">${t("None on the floor")}</div>`}
        ${partBlocks(part)}
      </div>`;
  }).join("");
}

function itemRow(item) {
  // One line per place holding units, mirroring the aging report -- and each
  // line is its own tap target: tapping the 40 pcs at carpentry opens the log
  // screen aimed at moving THOSE 40, independent of the 50 sitting at paint.
  // This is the whole workflow: tap a batch, say where it went.
  const { places, done, inSample, inParts } = localPlaces(item);
  // The card speaks for the piece and every part under it.
  const family = familyOf(item);
  const pending = family.reduce((sum, member) => sum + pendingCountFor(member.id), 0);
  const rejected = family.reduce((sum, member) => sum + rejectedCountFor(member.id), 0);
  const piece = pieceOf(item);
  const waits = !!item.state?.waits_on_sample;
  const sampler = waits ? S.items.find((i) => i.id === item.sample_item_id) : null;
  const parts = partsOf(item);
  const partsOnFloor = family.slice(1).some((member) => {
    const local = localPlaces(member);
    return local.places.length > 0 || local.done > 0;
  });

  const lines = placeRows(item, places, 0);

  return `
    <div>
      <a class="item-head" href="#/items/${item.id}">
        <div style="display:flex;gap:10px;align-items:flex-start">
          ${item.icon_url ? `<img class="item-icon" src="${thumbOf(item.icon_url)}" alt="" loading="lazy">` : ""}
          <div style="flex:1;min-width:0">
            <div class="spread">
              <strong>${esc(item.code)}</strong>
              <span class="muted">${esc(batchLabel(item))} · ${esc(revLabel(item.drawing_revision))}</span>
            </div>
            ${itemTags(item) ? `<div style="margin-top:3px">${itemTags(item)}</div>` : ""}
            <div class="muted" style="margin:2px 0 6px"><bdi>${esc(item.description)}</bdi></div>
            ${piece ? `<div style="margin-bottom:6px"><span class="badge tag">${esc(t("part of {code}", { code: piece.code }))}</span></div>` : ""}
            ${waits ? `<div style="margin-bottom:6px"><span class="badge sample">${t("Waiting")}</span>
              <span class="muted">${esc(t("released when the sample of {name} is done", { name: sampler ? itemName(sampler) : t("the other phase") }))}</span></div>`
            : inSample ? `<div style="margin-bottom:6px"><span class="badge sample">${t("Sample")}</span>
              <span class="muted">${t("batch of {n} follows once the sample is done", { n: item.total_qty })}</span></div>` : ""}
          </div>
        </div>
      </a>
      ${rejected ? `
      <a class="reject-row" href="#/sync">
        <span>${tn(rejected, "{n} entry was not accepted — see why", "{n} entries were not accepted — see why")}</span>
        <span class="chev" aria-hidden="true">›</span>
      </a>` : ""}
      ${pending ? `<div class="card-flags"><span class="badge pending">${t("{n} not synced yet", { n: pending })}</span></div>` : ""}
      ${lines}
      ${placeRows(item, [], done)}
      ${inParts ? `
      <div class="pos-row" style="border-inline-start:2px dashed var(--line)">
        <span class="muted">${t("In parts — below")}</span>
        <span class="badge qty">${inParts}</span>
      </div>` : ""}
      ${partBlocks(item)}
      ${places.length || partsOnFloor ? `
      <div class="card-actions">
        ${places.length ? `<a href="#/items/${item.id}/split">${t("Split into parts")}</a>` : ""}
        ${parts.length && partsOnFloor ? `<a href="#/items/${item.id}/assemble">${t("Assemble")}</a>` : ""}
      </div>` : ""}
    </div>`;
}

/* ----------------------------------------------------------- log screen -- */

/* What the form is set to. `from` and `to` are places: "unstarted", "done", or
   a stage id. `qty` is what the engineer typed, or null for "all of it". */
const logSel = {
  from: null, to: null, typeId: null, stationId: null, reasonId: null,
  qty: null, note: "", _arrival: null,
  // "move", or "split": the units at `from` become their parts at `to`.
  mode: "move",
  // Whether the "from" picker is unfolded (see viewLogScreen).
  fromOpen: false,
};

/* The station a stage's picker starts on: the one this phone used last at that
   stage, if it is still on offer, or the only one there is. Otherwise none --
   the engineer says which. Starting on "the first in the list" filled the log
   with a station nobody chose, and station counts are what load balancing is
   read from. */
function usualStation(stage, stations) {
  const remembered = localStorage.getItem(`station:${stage.id}`);
  if (stations.some((st) => st.id === remembered)) return remembered;
  return stations.length === 1 ? stations[0].id : null;
}

/* The assembly form on an item with parts: which place each part is taken
   from, where the finished pieces go, how many. */
const assembleSel = { itemId: null, qty: null, from: {}, to: null, stationId: null };

/* A part being named on the split screen, before it is added. */
const partDraft = { name: "", per: "1", open: false };

/* What a piece is made of, on its split screen: the parts it has, and a place
   to name another. Naming parts needs the server (a part is an item, and
   items are made there); splitting into parts already named does not. */
function splitPartsBlock(item, parts) {
  const out = item.state?.in_parts_qty || 0;
  return `
      <label>${t("Its parts")}</label>
      ${parts.length
        ? `<p style="margin:0 0 4px">${parts.map((part) => `${part.per_unit} × ${esc(partName(part))}`).join(t(", "))} <span class="muted">${t("per piece")}</span></p>`
        : `<p class="muted" style="margin:0 0 4px">${t("None yet. Name what this piece is made of — a back, a set of legs — then split.")}</p>`}
      <details data-panel="split-parts" ${partDraft.open || !parts.length ? "open" : ""}>
        <summary class="muted" style="cursor:pointer">${t("Add a part")}</summary>
        <div class="field-grid" style="margin-top:6px">
          <div><label for="np-name">${t("Name")}</label><input id="np-name" maxlength="60" autocomplete="off" placeholder="${esc(t("e.g. Back"))}" value="${esc(partDraft.name)}"></div>
          <div><label for="np-per">${t("How many per piece")}</label><input id="np-per" type="number" inputmode="numeric" min="1" value="${esc(partDraft.per)}"></div>
        </div>
        <button class="ghost" id="np-add" style="width:auto;padding:10px 16px;margin-top:8px" ${S.online && !out ? "" : "disabled"}>${t("Add part")}</button>
        ${!S.online ? `<p class="muted" style="margin-top:6px">${t("Naming a new part needs a connection. Splitting into the parts already named does not.")}</p>`
          : out ? `<p class="muted" style="margin-top:6px">${t("{n} piece(s) are out in parts by the present list. Assemble them before changing it.", { n: out })}</p>` : ""}
        <p class="warn-text" id="np-err" role="alert" hidden></p>
      </details>`;
}

/* The entries of one split or assembly: same moment, same link, one row per
   item. Every row names where its own units come from or go. */
function linkedRow(fields, link, at) {
  return {
    id: newId(), stage_id: null, station_id: null, qty: 0,
    from_stage_id: null, from_unstarted: false, from_done: false,
    from_link: false, to_link: false, link_id: link,
    reason_code_id: null, occurred_at: at, note: null,
    supersedes_event_id: null, user_id: S.user?.id || null,
    ...fields,
  };
}
const sourceFields = (key) => ({
  from_stage_id: key !== UNSTARTED && key !== DONE ? key : null,
  from_unstarted: key === UNSTARTED,
  from_done: key === DONE,
});

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
  const parts = partsOf(item);
  // Finishing is not a type the engineer picks: it is what "Done" writes.
  const types = (S.ref.event_types || []).filter(
    (t) => t.is_active && t.moves_quantity && !t.is_finish);
  const plainType = types.find((t) => !t.is_rework) || types[0];
  const reworkType = types.find((t) => t.is_rework);

  // Every place holding units is somewhere a move can start. Done too, but
  // only as rework: a finished piece coming back has a reason.
  const sources = places.map((p) => ({ key: p.key, label: whereLabel(p), qty: p.qty }));
  if (done > 0 && reworkType) sources.push({ key: DONE, label: t("Done"), qty: done });

  // Arrived by tapping a batch on the item card: aim the form at moving THAT
  // batch. Applied once per arrival, so segment taps and background
  // re-renders don't fight the engineer's own adjustments afterwards.
  const arrival = `${itemId}/${fromKey || ""}`;
  if (logSel._arrival !== arrival) {
    logSel._arrival = arrival;
    // "Split into parts" on the card arrives here already splitting.
    logSel.mode = fromKey === "split" ? "split" : "move";
    logSel.from = sources.some((s) => s.key === fromKey) ? fromKey : null;
    logSel.to = null;
    logSel.typeId = plainType?.id || null;
    logSel.stationId = null;
    logSel.qty = null;
    // A note typed on another item and abandoned there is not about this one.
    logSel.note = "";
    // Nor is an "Add a part" form left open there.
    partDraft.open = false;
    // The batch was chosen by tapping it on the card, so the picker for it
    // starts folded to one line; with no batch chosen it starts open.
    logSel.fromOpen = !logSel.from;
  }
  // Splitting takes pieces that are still pieces: never finished ones.
  const splitting = logSel.mode === "split";
  if (splitting) {
    const at = sources.findIndex((s) => s.key === DONE);
    if (at >= 0) sources.splice(at, 1);
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
  const targets = splitting
    // The parts start wherever they start: not started, or at a stage.
    ? [{ key: UNSTARTED, label: t("Not started") }, ...activeStages().map((st) => ({ key: st.id, label: localName(st) }))]
    : activeStages()
      .filter((st) => st.id !== logSel.from || st.requires_station)
      .map((st) => ({ key: st.id, label: localName(st) }));
  // Plain "Done": a tick in the label reads as "selected", which is what a
  // tick means on every other button of these pickers.
  if (!splitting && finish && !type?.is_rework && logSel.from !== DONE) targets.push({ key: DONE, label: t("Done") });
  if (!targets.some((t) => t.key === logSel.to)) {
    const usual = logSel.from && !splitting ? usualDestination(logSel.from) : null;
    logSel.to = targets.some((t) => t.key === usual) ? usual : null;
  }
  const toDone = logSel.to === DONE;
  const stage = !toDone && logSel.to && logSel.to !== UNSTARTED ? stageById(logSel.to) : null;
  const sameStage = !!stage && logSel.to === logSel.from;

  const stations = stage
    ? S.ref.stations.filter((st) => st.stage_id === stage.id && st.is_active)
    : [];
  if (stage?.requires_station && !stations.some((st) => st.id === logSel.stationId)) {
    logSel.stationId = usualStation(stage, stations);
  }
  // A stage with booths needs to know which one, unless it has none to offer.
  const needsStation = !!stage?.requires_station && stations.length > 0 && !logSel.stationId;
  const available = source?.qty || 0;
  const reasons = (S.ref.reason_codes || []).filter((r) => r.is_active);
  // Keep the tracked selection valid: a re-render must not silently reset the
  // reason to the first option, and an empty reason list must never turn into
  // an empty-string id in the payload.
  if (type?.requires_reason_code && !reasons.some((r) => r.id === logSel.reasonId)) {
    logSel.reasonId = reasons[0]?.id || null;
  }
  const sampleQty = item.state?.sample_qty || 0;
  const seg = (id, label, options, picked, compact = false) => `
      <label id="lbl-${id}">${label}</label>
      <div class="seg${compact ? " compact" : ""}" id="seg-${id}" role="group" aria-labelledby="lbl-${id}">
        ${options.map((o) => `<button data-id="${o.key}" class="${o.key === picked ? "on" : ""}" aria-pressed="${o.key === picked}">${esc(o.label)}</button>`).join("")}
      </div>`;
  // Where the units are taken from. Tapping a batch on the card already said
  // which, so it is one line here with a way to change it -- the full picker
  // took the top third of the screen to ask a question that had been answered,
  // and pushed the quantity below the fold.
  const fromLabel = splitting ? t("Split the pieces at") : t("From");
  const fromPicker = logSel.fromOpen || !source
    ? seg("from", fromLabel, sources.map((s) => ({ key: s.key, label: `${s.label} · ${s.qty}` })), logSel.from)
    : `
      <label id="lbl-from">${fromLabel}</label>
      <div class="picked-row" role="group" aria-labelledby="lbl-from">
        <strong>${esc(source.label)} · ${source.qty}</strong>
        ${sources.length > 1 ? `<button class="ghost" id="from-change">${t("Change")}</button>` : ""}
      </div>`;
  const goLabel = splitting && !parts.length ? t("Name the parts first")
    : !logSel.to ? (splitting ? t("Pick where the parts go") : t("Pick where it went"))
    : needsStation ? t("Pick the station")
    : splitting ? t("Split into parts") : t("Log entry");
  const goReady = !!logSel.to && !needsStation && !(splitting && !parts.length);

  // "Assemble" on the card arrives here for the assembly form: it goes first.
  const assembleFirst = fromKey === "assemble";
  const assembleHtml = parts.length ? assembleCard(item, parts) : "";

  $view.innerHTML = `
    <div class="card">
      <div style="display:flex;gap:12px;align-items:center">
        ${item.icon_url ? `<img class="item-icon" src="${thumbOf(item.icon_url)}" alt="">` : ""}
        <div style="flex:1;min-width:0">
          <div class="spread">
            <h2>${esc(item.code)}</h2>
            <span class="muted">${esc(batchLabel(item))} · ${esc(revLabel(item.drawing_revision))}</span>
          </div>
          ${itemTags(item) ? `<div style="margin:2px 0">${itemTags(item)}</div>` : ""}
          <div class="muted"><bdi>${esc(item.description)}</bdi></div>
        </div>
      </div>
      ${inSample ? `
      <p style="margin-top:8px"><span class="badge sample">${t("Sample")}</span>
        <span class="muted">${t("Only the sample ({qty}) is on the floor. Log it to Done and the batch of {n} is released.", { qty: pcs(sampleQty), n: item.total_qty })}</span></p>` : ""}
    </div>

    ${assembleFirst ? assembleHtml : ""}
    <div class="card">
      ${!source ? `
      <p class="muted">${esc(item.state?.in_parts_qty
        ? t("Nothing here to move — the pieces of {code} are out in parts.", { code: item.code })
        : t("Nothing here to move — every unit of {code} is Done.", { code: item.code }))}</p>` : `
      ${parts.length || splitting ? seg("mode", t("What happened"), [{ key: "move", label: t("Moved") }, { key: "split", label: t("Split into parts") }], logSel.mode) : ""}

      ${fromPicker}
      ${splitting ? splitPartsBlock(item, parts) : ""}

      ${!splitting && shownTypes.length > 1 ? seg("type", t("Entry type"), shownTypes.map((ty) => ({ key: ty.id, label: ty.name })), logSel.typeId, true) : ""}
      ${logSel.from === DONE ? `<p class="muted" style="margin-top:6px">${t("Taking a finished piece back out of Done is rework, and needs its reason.")}</p>` : ""}

      ${seg("to", splitting ? t("The parts go to") : type?.is_rework ? t("Send back to") : t("Now at"), targets, logSel.to)}

      ${stage?.requires_station ? seg("station", t("Station"), stations.map((st) => ({ key: st.id, label: localName(st) })), logSel.stationId) : ""}

      ${!splitting && type?.requires_reason_code ? `
      <label for="sel-reason">${t("Reason")}</label>
      <select id="sel-reason">
        ${reasons.map((r) => `<option value="${r.id}" ${r.id === logSel.reasonId ? "selected" : ""}>${esc(r.name)}</option>`).join("")}
      </select>` : ""}

      <label for="qty">${t("Quantity")}</label>
      <div class="qty-row">
        <button id="qty-minus" aria-label="${esc(t("One fewer"))}">−</button>
        <input id="qty" type="number" inputmode="numeric" min="1"
               value="${esc(logSel.qty ?? (available || 1))}">
        <button id="qty-plus" aria-label="${esc(t("One more"))}">+</button>
      </div>
      <p class="muted" style="margin-top:5px">${esc(t("{n} at {place}", { n: available, place: source.label }))}</p>
      ${splitting ? `<p class="muted" id="split-preview"></p>` : ""}
      ${sameStage ? `<p class="muted">${t("Same stage — this moves them to another station. Their age keeps counting.")}</p>` : ""}
      ${stage?.requires_external_po ? `<p class="muted">${t("External supplier stage — time in state is supplier lead time.")}</p>` : ""}
      ${stage && !stage.allows_partial_qty ? `<p class="muted">${t("Whole-lot stage — the move must take everything from where it comes.")}</p>` : ""}
      <p class="warn-text" id="qty-warn" hidden>${t("More than is resting there — the server will reject this entry until the log shows that many. It will wait under the sync pill with a Retry button.")}</p>

      <details data-panel="log-note" ${logSel.note ? "open" : ""} style="margin-top:12px">
        <summary class="muted" style="cursor:pointer">${t("Add note")}${logSel.note ? " ·" : ""}</summary>
        <input id="log-note" maxlength="2000" placeholder="${esc(t("e.g. rack 3, waiting on fittings"))}"
               value="${esc(logSel.note)}" style="margin-top:6px">
      </details>

      <div style="height:12px" aria-hidden="true"></div>
      <div class="action-sticky">
        <button class="primary" id="log-go" ${goReady ? "" : "disabled"}>${goLabel}</button>
      </div>`}
    </div>
    ${assembleFirst ? "" : assembleHtml}
    ${pieceOf(item) ? `<div class="card"><p class="muted">${t("Part of {name}, {n} per piece. It is split off that item and assembled back into it from that item's screen.", { name: `<strong>${esc(itemName(pieceOf(item)))}</strong>`, n: item.per_unit })}</p></div>` : ""}

    <div class="card">
      <h2>${t("Photos")} <span class="muted" id="photo-count"></span></h2>
      <div id="photos" class="muted">${t("Loading…")}</div>
      <div style="height:8px"></div>
      <div id="photo-progress" hidden>
        <div class="bar-track" id="photo-track"><div class="bar-fill" id="photo-bar" style="width:0"></div></div>
        <p id="photo-status" class="muted" style="margin:4px 0 8px" role="status" aria-live="polite"></p>
      </div>
      <input id="photo-note" maxlength="255" placeholder="${esc(t("Describe the snag (optional)"))}"
             style="margin-bottom:8px" ${S.online ? "" : "disabled"}>
      ${photoPicker("snag", { camera: t("Take snag photo"), gallery: t("From gallery"), single: t("Add snag photo") })}
    </div>

    <div class="card">
      <h2>${t("Recent entries")}</h2>
      <div id="recent" class="muted">${t("Loading…")}</div>
    </div>`;

  const rerender = () => viewLogScreen(itemId, fromKey);
  wireSeg("seg-from", (key) => {
    // A different batch: where it goes and how many start over. Chosen, so
    // the picker folds back to its one line.
    Object.assign(logSel, { from: key, to: null, stationId: null, qty: null, fromOpen: false });
    rerender();
  });
  const $fromChange = document.getElementById("from-change");
  if ($fromChange) $fromChange.onclick = () => { logSel.fromOpen = true; rerender(); };
  wireSeg("seg-type", (id) => { logSel.typeId = id; rerender(); });
  wireSeg("seg-mode", (key) => { Object.assign(logSel, { mode: key, to: null, stationId: null }); rerender(); });

  // Naming a part, on the split screen. Typed text lives in partDraft so a
  // segment tap's re-render cannot eat it.
  const $partName = document.getElementById("np-name");
  const $partAdd = document.getElementById("np-add");
  // Whether "Add a part" is unfolded is the engineer's, and every tap on this
  // screen redraws it: remembered here, or the form would fold itself shut
  // after the first part of several is added.
  const $partPanel = $view.querySelector('details[data-panel="split-parts"]');
  if ($partPanel) $partPanel.ontoggle = () => { if ($partPanel.isConnected) partDraft.open = $partPanel.open; };
  if ($partName && $partAdd) {
    const $per = document.getElementById("np-per");
    $partName.oninput = () => { partDraft.name = $partName.value; };
    $per.oninput = () => { partDraft.per = $per.value; };
    $partAdd.onclick = async () => {
      const err = document.getElementById("np-err");
      const fail = (message) => { err.textContent = message; err.hidden = false; };
      err.hidden = true;
      const name = partDraft.name.trim();
      const perUnit = Number(partDraft.per);
      if (!name) return fail(t("Give the part a name."));
      if (!Number.isInteger(perUnit) || perUnit < 1) return fail(t("How many per piece: a whole number, 1 or more."));
      // The part's code is the piece's with the name on the end: BED-01-BACK.
      const code = `${item.code}-${slugify(name).replace(/_/g, "-").toUpperCase()}`.slice(0, 64);
      $partAdd.disabled = true;
      try {
        const made = await api(`/api/items/${item.id}/parts`, {
          method: "POST", body: JSON.stringify({ code, description: name, per_unit: perUnit }),
        });
        // Cleared for the next one, and left open: a piece is rarely one part.
        Object.assign(partDraft, { name: "", per: "1", open: true });
        toast(t("{name} added as a part of {code}", { name, code: item.code }));
        // On this phone at once, from the server's own answer: a sync already
        // in flight may have read the list before the part existed. Nothing of
        // it is on the floor yet, which is what an empty state says.
        if (!S.items.some((i) => i.id === made.id)) {
          S.items = [...S.items, { ...made, state: {
            positions: [], phase: "production", sample_qty: 0, done_qty: 0, in_parts_qty: 0, batch_qty: 0,
          } }];
          await LSF_DB.put("items", S.items);
        }
        sync(); // and every other phone learns of it the usual way
        if (logSel._arrival === arrival) rerender();
      } catch (error) {
        const detail = error.body?.detail;
        fail(typeof detail === "string" ? detail
          : Array.isArray(detail) ? rejectReason(error)
          : isNetworkError(error) ? t("No connection — the part was not added.")
          : t("Could not add the part."));
        $partAdd.disabled = false;
      }
    };
  }
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
    const preview = () => {
      const target = document.getElementById("split-preview");
      if (!target) return;
      const n = Number($qty.value) || 0;
      target.textContent = tn(n, "{n} piece becomes {parts}", "{n} pieces become {parts}",
        { parts: parts.map((part) => `${n * part.per_unit} × ${partName(part)}`).join(t(", ")) });
    };
    const warn = () => {
      logSel.qty = $qty.value;
      document.getElementById("qty-warn").hidden = Number($qty.value) <= available;
      preview();
    };
    preview();
    $qty.oninput = warn;
    document.getElementById("qty-minus").onclick = () => { $qty.value = Math.max(1, Number($qty.value) - 1); warn(); };
    document.getElementById("qty-plus").onclick = () => { $qty.value = Number($qty.value) + 1; warn(); };
    document.getElementById("qty-warn").hidden = Number($qty.value) <= available;

    const $go = document.getElementById("log-go");
    $go.onclick = async () => {
      if (!goReady) return;
      // A whole number of pieces, at least one. Anything else used to be
      // queued as typed and only refused by the server at the next sync -- or,
      // for an empty box, quietly logged as 1.
      const qty = Number($qty.value);
      if (!Number.isInteger(qty) || qty < 1) {
        toast(t("Enter a whole quantity of 1 or more"));
        $qty.focus();
        return;
      }
      // One tap, one entry: a second tap landing while the first is still
      // being written would queue the move twice.
      $go.disabled = true;
      const at = new Date().toISOString();
      const station = stage?.requires_station ? logSel.stationId : null;
      const logged = splitting ? await logEvents(((link) => [
        // The pieces leave this item from where they were...
        linkedRow({ item_id: item.id, event_type_id: plainType.id, qty, to_link: true, ...sourceFields(logSel.from) }, link, at),
        // ...and each part arrives, as many per piece as go into one.
        ...parts.map((part) => linkedRow({
          item_id: part.id, event_type_id: plainType.id, qty: qty * part.per_unit, from_link: true,
          stage_id: logSel.to === UNSTARTED ? null : logSel.to, station_id: station,
        }, link, at)),
      ])(newId())) : await logEvent({
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
      if (!logged) { $go.disabled = false; return; }
      // Remember the habit, not rework: sending work back is the exception.
      if (!type?.is_rework && !splitting) localStorage.setItem(`next:${logSel.from}`, logSel.to);
      Object.assign(logSel, { note: "", qty: null, _arrival: null, mode: "move" });
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
        if (photoStatus.busy) { toast(t("A photo is already uploading.")); return; }
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
  wireUpload("snag", "snag", t("Snag photo added"), "photo-note");

  if (parts.length) bindAssemble(item, parts, rerender);
  loadRecent(item);
  loadPhotos(item);
}

/* Where each part can be taken from for an assembly: wherever its units
   rest, finished parts included. */
function partSources(part) {
  const { places, done } = localPlaces(part);
  const out = places.map((p) => ({ key: p.key, label: p.name, qty: p.qty }));
  if (done > 0) out.push({ key: DONE, label: t("Done"), qty: done });
  return out;
}

function assembleCard(item, parts) {
  const sel = assembleSel;
  if (sel.itemId !== item.id) Object.assign(sel, { itemId: item.id, qty: null, from: {}, to: null, stationId: null });
  const rows = parts.map((part) => {
    const options = partSources(part);
    if (!options.some((o) => o.key === sel.from[part.id])) {
      // Wherever most of it is: usually where the parts have gathered.
      sel.from[part.id] = options.slice().sort((a, b) => b.qty - a.qty)[0]?.key || null;
    }
    const picked = options.find((o) => o.key === sel.from[part.id]);
    return { part, options, sets: picked ? Math.floor(picked.qty / part.per_unit) : 0 };
  });
  const most = Math.min(...rows.map((r) => r.sets));
  const targets = activeStages().map((st) => ({ key: st.id, label: localName(st) }));
  if (finishType()) targets.push({ key: DONE, label: t("Done") });
  if (!targets.some((target) => target.key === sel.to)) sel.to = null;
  const stage = sel.to && sel.to !== DONE ? stageById(sel.to) : null;
  const stations = stage ? S.ref.stations.filter((st) => st.stage_id === stage.id && st.is_active) : [];
  if (stage?.requires_station && !stations.some((st) => st.id === sel.stationId)) sel.stationId = usualStation(stage, stations);
  const needsStation = !!stage?.requires_station && stations.length > 0 && !sel.stationId;
  const qty = sel.qty ?? Math.max(most, 1);
  const seg = (id, label, options, picked) => `
      <label id="lbl-${id}">${label}</label>
      <div class="seg" id="seg-${id}" role="group" aria-labelledby="lbl-${id}">
        ${options.map((o) => `<button data-id="${o.key}" class="${o.key === picked ? "on" : ""}" aria-pressed="${o.key === picked}">${esc(o.label)}</button>`).join("")}
      </div>`;
  return `
    <div class="card">
      <h2>${t("Assemble from parts")}</h2>
      <p class="muted">${t("Each piece takes {parts}. Say where each part is taken from.", { parts: parts.map((part) => `${part.per_unit} × ${esc(partName(part))}`).join(t(", ")) })}</p>
      ${rows.map(({ part, options, sets }) => `
      <label for="asm-from-${part.id}">${esc(partName(part))} <span class="muted">(${esc(part.code)})</span></label>
      ${options.length ? `
      <select id="asm-from-${part.id}" data-asm-from="${part.id}">
        ${options.map((o) => `<option value="${o.key}" ${o.key === sel.from[part.id] ? "selected" : ""}>${esc(o.label)} · ${o.qty}</option>`).join("")}
      </select>
      <p class="muted" style="margin:2px 0 0">${tn(sets, "enough for {n} piece", "enough for {n} pieces")}</p>`
      : `<p class="warn-text" style="margin:2px 0 0">${esc(t("None on the floor — split some {code} first.", { code: item.code }))}</p>`}`).join("")}
      ${seg("asm-to", t("The assembled pieces are now at"), targets, sel.to)}
      ${stage?.requires_station ? seg("asm-station", t("Station"), stations.map((st) => ({ key: st.id, label: localName(st) })), sel.stationId) : ""}
      <label for="asm-qty">${t("Pieces assembled")}</label>
      <input id="asm-qty" type="number" inputmode="numeric" min="1" value="${esc(qty)}">
      <p class="muted" style="margin-top:5px">${tn(most, "The parts on hand make {n} piece.", "The parts on hand make {n} pieces.")}</p>
      <p class="warn-text" id="asm-warn" ${Number(qty) > most ? "" : "hidden"}>${t("More than the parts on hand make — the server will refuse it until they are there.")}</p>
      <div style="height:12px" aria-hidden="true"></div>
      <button class="primary" id="asm-go" ${sel.to && !needsStation && rows.every((r) => r.options.length) ? "" : "disabled"}>${!sel.to ? t("Pick where the pieces go") : needsStation ? t("Pick the station") : t("Assemble")}</button>
    </div>`;
}

function bindAssemble(item, parts, rerender) {
  const sel = assembleSel;
  document.querySelectorAll("[data-asm-from]").forEach((select) => {
    select.onchange = (e) => { sel.from[select.dataset.asmFrom] = e.target.value; rerender(); };
  });
  wireSeg("seg-asm-to", (key) => { sel.to = key; sel.stationId = null; rerender(); });
  wireSeg("seg-asm-station", (id) => {
    sel.stationId = id;
    // Remembered per stage, like the log form's.
    if (sel.to && sel.to !== DONE) localStorage.setItem(`station:${sel.to}`, id);
    rerender();
  });
  const $qty = document.getElementById("asm-qty");
  if (!$qty) return;
  $qty.oninput = () => { sel.qty = $qty.value; };
  const go = document.getElementById("asm-go");
  if (!go) return;
  go.onclick = async () => {
    const qty = Number($qty.value);
    if (!Number.isInteger(qty) || qty < 1) { toast(t("Enter a whole quantity of 1 or more")); return; }
    if (!sel.to || parts.some((part) => !sel.from[part.id])) return;
    const plain = (S.ref.event_types || []).find((t) => t.is_active && t.moves_quantity && !t.is_finish && !t.is_rework && !t.is_correction);
    const finish = finishType();
    const toDone = sel.to === DONE;
    const stage = !toDone ? stageById(sel.to) : null;
    const link = newId();
    const at = new Date().toISOString();
    const logged = await logEvents([
      // Each part gives up what goes into this many pieces, from where it is...
      ...parts.map((part) => linkedRow({
        item_id: part.id, event_type_id: plain.id, qty: qty * part.per_unit, to_link: true,
        ...sourceFields(sel.from[part.id]),
      }, link, at)),
      // ...and the pieces appear, assembled.
      linkedRow({
        item_id: item.id, event_type_id: toDone ? finish.id : plain.id, qty, from_link: true,
        stage_id: toDone ? null : sel.to, station_id: stage?.requires_station ? sel.stationId : null,
      }, link, at),
    ]);
    if (!logged) return;
    Object.assign(sel, { itemId: null });
    logScreenCache.fetchedAt = 0;
    navigate("#/items");
  };
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

function photoPicker(key, { camera = t("Take photo"), gallery = t("From gallery"), single = t("Add photo"), offline = t("Photos need a connection") } = {}) {
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
        fail(t("Session expired — sign in again, then add the photo."));
      } else {
        fail(body.detail ? rejectReason({ body }) : t("The server refused it (HTTP {status}).", { status: xhr.status }));
      }
    };
    xhr.onerror = () => fail(t("No connection — the photo was not sent. Try again in range."));
    xhr.ontimeout = () => fail(t("Timed out — the connection dropped mid-upload. Try again."));
    xhr.onabort = () => fail(t("Upload cancelled."));
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
    text.textContent = percent < 100 ? t("Sending photo… {n}%", { n: percent }) : t("Saving on the server…");
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
    text.textContent = reason || t("Upload failed.");
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
                <img src="${img.thumb_url || img.url}" alt="${esc(img.note || img.filename)}" loading="lazy"
                     style="width:100%;aspect-ratio:1;object-fit:cover;border-radius:8px;border:1px solid var(--line)">
                ${img.kind === "icon" ? `<span class="badge" style="position:absolute;top:4px;inset-inline-start:4px;background:rgba(0,0,0,.55)">${t("icon")}</span>` : ""}
              </a>
              <button class="ghost" data-note-for="${img.id}" ${S.online ? "" : "disabled"}
                      style="width:100%;margin-top:4px;padding:6px 4px;min-height:36px;font-size:12px;text-align:start;overflow:hidden;overflow-wrap:anywhere"
                      title="${esc(img.note || "")}">${img.note ? esc(img.note) : t("+ note")}</button>
            </div>`).join("")}
         </div>`
      : `<span class="muted">${t("No photos yet.")}</span>`;

    // The note is written after the shutter: on the floor you photograph the
    // snag first and find the words for it second.
    target.querySelectorAll("[data-note-for]").forEach((button) => {
      const image = images.find((img) => img.id === button.dataset.noteFor);
      button.onclick = async () => {
        const note = prompt(t("What is the snag?"), image.note || "");
        if (note === null) return;
        try {
          await api(`/api/images/${image.id}`, {
            method: "PATCH", body: JSON.stringify({ note: note.trim() }),
          });
          toast(note.trim() ? t("Note saved") : t("Note cleared"));
          logScreenCache.fetchedAt = 0;
          loadPhotos(item);
        } catch (error) {
          alert(error.body?.detail || t("Could not save the note."));
        }
      };
    });
  } catch {
    if (target.isConnected) target.textContent = t("Offline — photos unavailable.");
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
      // A linked entry's other side is another item: its parts, the piece it
      // belongs to, or the same code in another phase.
      const hasParts = partsOf(item).length > 0;
      const to = e.to_link ? (hasParts ? t("into parts") : item.parent_item_id ? t("into assembly") : t("into another phase"))
        : type?.is_finish ? t("Done")
        : e.to_stage_id ? placeName(e.to_stage_id) : e.from_link ? t("Not started") : "";
      const from = e.from_link
        ? (hasParts ? t("from parts") : item.parent_item_id ? t("from split of {code}", { code: pieceOf(item)?.code || t("its piece") }) : t("from another phase"))
        : e.from_unstarted ? t("Not started") : e.from_done ? t("Done")
        : e.from_stage_id ? placeName(e.from_stage_id) : "";
      const label = [
        type?.name || t("event"),
        from && to ? `${from} → ${to}` : to,
        e.qty ? pcs(e.qty) : "",
        e.state_name || "", // rows logged before states were removed
      ].filter(Boolean).join(" · ");
      const voided = !!e.superseded || queuedVoids.has(e.id);
      // One row of a split or an assembly cannot be voided alone: the other
      // items would keep their half. The opposite move undoes it.
      const canVoid = correctionType && type?.moves_quantity && !voided && !e.link_id;
      return `
        <div class="spread" style="padding:6px 0;border-bottom:1px solid var(--line)">
          <span><span${voided ? ` style="text-decoration:line-through;opacity:.6"` : ""}>${esc(label)}</span>${voided ? ` <span class="badge">${t("voided")}</span>` : ""}<br><span class="muted">${fmtWhen(e.occurred_at)}</span>${e.note ? `<br><span class="muted">“${esc(e.note)}”</span>` : ""}</span>
          ${canVoid ? `<button class="ghost" data-void="${e.id}">${t("Void")}</button>` : ""}
        </div>`;
    }).join("") || t("No entries yet.");
    target.querySelectorAll("[data-void]").forEach((button) => {
      button.onclick = async () => {
        if (!confirm(t("Void this entry? A correction event will be logged; history is kept."))) return;
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
    if (target.isConnected) target.textContent = t("Offline — recent entries unavailable.");
  }
}

/* -------------------------------------------------------------- reports -- */

/* Which request for the reports is the latest, and whether the last tap asked
   to be taken to the aging table once it is drawn. */
const reportsView = { asked: 0, showAging: false };

/* An aging row's place: the stage, as the factory named it, or "Not started"
   -- which the server says in English, so it is said here instead. */
const agingWhere = (row) => (row.stage ? localName(row.stage) : t("Not started"));

/* The exceptions view's badges. These are the ledger's own fixed codes, not
   factory vocabulary; one it does not know yet is shown as the code. */
const EXCEPTION_NAMES = {
  orphaned_correction: N_("Orphaned correction"),
  competing_correction: N_("Competing correction"),
  device_clock_ahead: N_("Device clock ahead"),
  late_sync: N_("Late sync"),
  over_advance: N_("Over-advance"),
};
const exceptionName = (code) => (EXCEPTION_NAMES[code] ? t(EXCEPTION_NAMES[code]) : code);

async function viewReports() {
  const projects = (S.ref?.projects || []).filter((p) => p.is_active !== false);
  const stages = (S.ref?.stages || []).filter((s) => s.is_active);
  const fProject = validFilter("r-project", projects);
  const fStage = validFilter("r-stage", stages);
  // The answer is drawn seconds later on a slow link. By then the engineer may
  // be on another screen, or have changed a filter again: only the newest
  // request, on this screen, may draw.
  const asked = ++reportsView.asked;
  const current = () => asked === reportsView.asked && (location.hash || "").startsWith("#/reports");
  const fDays = sessionStorage.getItem("r-days") || "";
  // The reports are computed on the server, which narrows by area and phase
  // before it replays anything. No "none" here: that is a question for the list.
  const areas = labelOptions("areas", "area_id", [], "", fProject);
  const phases = labelOptions("phases", "phase_id", [], "", fProject);
  const fArea = validFilter("r-area", areas);
  const fPhase = validFilter("r-phase", phases);

  $view.innerHTML = `<p class="muted">${t("Loading reports…")}</p>`;
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
    if (current()) $view.innerHTML = `<p class="muted">${t("Reports need a connection — they are computed from the full log on the server.")}</p>`;
    return;
  }
  if (!current()) return;
  const staleNote = cachedFrom
    ? `<p class="warn-text" style="margin-bottom:8px">${cachedFrom !== "1"
      ? t("Offline — showing last-known reports from {when}.", { when: fmtWhen(cachedFrom) })
      : t("Offline — showing last-known reports.")}</p>`
    : "";

  const maxQty = Math.max(1, ...wip.stages.map((s) => s.total_qty));

  $view.innerHTML = `
    ${staleNote}
    <div class="filters">
      <select id="r-project" aria-label="${esc(t("Project"))}">
        <option value="">${t("All projects")}</option>
        ${projects.map((p) => `<option value="${p.id}" ${p.id === fProject ? "selected" : ""}>${esc(p.code)}</option>`).join("")}
      </select>
      <select id="r-stage" aria-label="${esc(t("Stage"))}">
        <option value="">${t("All stages")}</option>
        ${stages.map((s) => `<option value="${s.id}" ${s.id === fStage ? "selected" : ""}>${esc(localName(s))}</option>`).join("")}
      </select>
      ${areas.length ? `
      <select id="r-area" aria-label="${esc(t("Area"))}">
        <option value="">${t("All areas")}</option>
        ${areas.map((a) => `<option value="${a.id}" ${a.id === fArea ? "selected" : ""}>${esc(a.name)}</option>`).join("")}
      </select>` : ""}
      ${phases.length ? `
      <select id="r-phase" aria-label="${esc(t("Phase"))}">
        <option value="">${t("All phases")}</option>
        ${phases.map((ph) => `<option value="${ph.id}" ${ph.id === fPhase ? "selected" : ""}>${esc(ph.name)}</option>`).join("")}
      </select>` : ""}
      <select id="r-days" aria-label="${esc(t("Age"))}">
        <option value="">${t("Any age")}</option>
        ${[1, 2, 3, 5, 7, 14].map((d) => `<option value="${d}" ${String(d) === fDays ? "selected" : ""}>≥ ${t("{n}d", { n: d })}</option>`).join("")}
      </select>
    </div>

    <div class="card">
      <h2>${t("WIP by stage")}</h2>
      <p class="muted" style="margin:-4px 0 10px">${t("Tap a stage to see what is sitting there.")}</p>
      ${wip.stages.map((row) => `
        <button class="wip-row${row.stage.id === fStage ? " on" : ""}" data-wip-stage="${row.stage.id}" aria-pressed="${row.stage.id === fStage}">
          <span class="spread">
            <strong>${esc(localName(row.stage))}</strong>
            <span class="muted">${pcs(row.total_qty)} · ${tn(row.item_count, "{n} item", "{n} items")}${row.sample_qty ? ` · <span class="badge sample">${t("{n} sample", { n: row.sample_qty })}</span>` : ""}${row.reworked_qty ? ` · <span class="badge rework">${t("rework {n}", { n: row.reworked_qty })}</span>` : ""}</span>
          </span>
          <span class="bar-track" style="margin:4px 0"><span class="bar-fill" style="width:${(row.total_qty / maxQty) * 100}%"></span></span>
          ${row.stage.requires_station ? row.stations.map((s) =>
            `<span class="muted">· ${s.station ? `<bdi>${esc(localName(s.station))}</bdi>` : t("No station yet")}: ${s.total_qty}</span>`).join("") : ""}
        </button>`).join("") || `<p class="muted">${t("Nothing in progress.")}</p>`}
      ${wip.unstarted.qty ? `<p class="muted">${t("Not started: {qty} across {items}", { qty: pcs(wip.unstarted.qty), items: tn(wip.unstarted.item_count, "{n} item", "{n} items") })}</p>` : ""}
    </div>

    <div class="card" id="aging-card">
      <h2>${fStage
        ? esc(t("Aging — days at {stage}", { stage: localName(stages.find((s) => s.id === fStage)) || t("current stage") }))
        : t("Aging — days at current stage")}</h2>
      <div class="table-scroll">
      <table>
        <thead><tr><th scope="col">${t("Item")}</th><th scope="col">${t("Where")}</th><th scope="col" class="num">${t("Qty")}</th><th scope="col" class="num">${t("Days")}</th></tr></thead>
        <tbody>
          ${aging.rows.map((row) => `
            <tr>
              <td><a class="cell-link" href="#/items/${row.item.id}">${esc(row.item.code)}${
                row.item.area || row.item.phase ? `<br><span class="muted">${esc([row.item.area, row.item.phase].filter(Boolean).join(" · "))}</span>` : ""}</a></td>
              <td>${esc(agingWhere(row))}${row.sample_qty ? ` <span class="badge sample">${t("sample")}</span>` : ""}${row.reworked_qty ? ` <span class="badge rework" title="${esc(t("Reworked"))}">${t("R")}</span>` : ""}</td>
              <td class="num">${row.qty}</td>
              <td class="num${row.overdue ? " overdue" : ""}">${row.days_in_state.toFixed(1)}${row.overdue ? " ⚠" : ""}</td>
            </tr>`).join("")}
        </tbody>
      </table>
      </div>
      ${aging.rows.length ? "" : `<p class="muted" style="margin-top:8px">${t("Nothing matches.")}</p>`}
    </div>

    ${exceptions.rows.length ? `
    <div class="card">
      <h2>${t("Needs review ({n})", { n: exceptions.rows.length })}</h2>
      ${exceptions.rows.slice(0, 20).map((row) => `
        <div style="padding:6px 0;border-bottom:1px solid var(--line)">
          <span class="badge">${esc(exceptionName(row.code))}</span> <strong>${esc(row.item_code || "")}</strong>${
            row.area || row.phase ? ` <span class="muted">${esc([row.area, row.phase].filter(Boolean).join(" · "))}</span>` : ""}
          <div class="muted" dir="auto">${esc(row.detail)}</div>
        </div>`).join("")}
    </div>` : ""}`;

  for (const id of ["r-project", "r-stage", "r-area", "r-phase", "r-days"]) {
    const select = document.getElementById(id);
    if (select) select.onchange = (e) => { sessionStorage.setItem(id, e.target.value); render(); };
  }
  // A stage's bar is the way into it: tap it and the aging table below is that
  // stage's batches, oldest first. Tap it again for every stage.
  $view.querySelectorAll("[data-wip-stage]").forEach((button) => {
    button.onclick = () => {
      const id = button.dataset.wipStage;
      sessionStorage.setItem("r-stage", id === fStage ? "" : id);
      // The dropdown is carried across a re-render by its value; keep it in step.
      const select = document.getElementById("r-stage");
      if (select) select.value = id === fStage ? "" : id;
      reportsView.showAging = id !== fStage;
      render();
    };
  });
  if (reportsView.showAging) {
    reportsView.showAging = false;
    document.getElementById("aging-card")?.scrollIntoView({ block: "start" });
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
  { key: "import", label: N_("Import") },
  { key: "items", label: N_("Items") },
  { key: "projects", label: N_("Projects") },
  { key: "stages", label: N_("Stages"), admin: true },
  { key: "workorders", label: N_("Work orders") },
  { key: "areas", label: N_("Areas & phases"), admin: true },
  { key: "users", label: N_("Users"), admin: true },
  { key: "status", label: N_("Status"), admin: true },
];

function viewOffice(section, detail) {
  const sections = OFFICE_SECTIONS.filter((s) => !s.admin || S.user?.is_admin);
  // Labels are kept in English above and said in the reader's language here.
  const current = sections.find((s) => s.key === section) || sections[0];

  $view.innerHTML = `
    <nav class="subnav">
      ${sections.map((s) => `
        <a href="#/office/${s.key}"${s.key === current.key ? ` class="on" aria-current="page"` : ""}>${esc(t(s.label))}</a>`).join("")}
    </nav>
    <div id="office-body"></div>`;

  const body = document.getElementById("office-body");
  ({
    import: officeImport,
    items: officeItems,
    projects: officeProjects,
    stages: officeStages,
    workorders: officeWorkOrders,
    areas: officeAreas,
    users: officeUsers,
    status: officeStatus,
  }[current.key])(body, detail);
}

/* A one-line upload status for the editors, same contract as the snag-photo
   bar: percentage while it goes up, the server's own words if it refuses. */
function inlineUpload(el) {
  return {
    set: (fraction) => {
      const percent = Math.round(fraction * 100);
      el.hidden = false;
      el.className = "muted";
      el.textContent = percent < 100 ? t("Uploading… {n}%", { n: percent }) : t("Saving on the server…");
    },
    done: () => { el.hidden = true; },
    fail: (reason) => {
      el.hidden = false;
      el.className = "warn-text";
      el.textContent = reason || t("Upload failed.");
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
      <h2>${t("Import a project")}</h2>
      <p class="muted" style="margin-bottom:8px">${t("One spreadsheet (.xlsx or .csv), one row per item. Every item starts as a sample; when the sample is logged Done, the batch is released. There are no stages to list: an item can be logged at any stage, in any order.")}
      ${t("For a single item, use")} <a href="#/office/items">${t("Items → New item")}</a>.</p>
      <div class="table-scroll">
      <table>
        <thead><tr><th scope="col">${t("Column")}</th><th scope="col">${t("What goes in it")}</th></tr></thead>
        <tbody>
          <!-- The column names are what the file must say, so they stay as
               they are; only what they mean is translated. -->
          <tr><td dir="ltr"><strong>project</strong></td><td>${t("Project code. New codes create the project; existing ones are added to.")}</td></tr>
          <tr><td dir="ltr">project name, client</td><td>${t("Optional. Only used when the project is new.")}</td></tr>
          <tr><td dir="ltr"><strong>item</strong></td><td>${t("Item code. It may repeat on several rows, as long as each has a different area or phase: those are separate batches.")}</td></tr>
          <tr><td dir="ltr">area, phase</td><td>${t("Optional. Which part of the job, and which phase of it, the batch is for. Each project has its own; a name it does not have yet is added to it.")}</td></tr>
          <tr><td dir="ltr">description</td><td>${t("Optional.")}</td></tr>
          <tr><td dir="ltr">dimensions</td><td>${t("Optional, as the drawing gives them. Printed on work orders.")}</td></tr>
          <tr><td dir="ltr"><strong>qty</strong></td><td>${t("The production batch.")}</td></tr>
          <tr><td dir="ltr">sample qty</td><td>${t("Optional, default 1. Built first; 0 for no sample.")}</td></tr>
          <tr><td dir="ltr">revision</td><td>${t("Optional drawing revision, default A.")}</td></tr>
          <tr><td dir="ltr">picture</td><td>${t("Optional, .xlsx only. Put a picture on the item's row (Insert → Pictures → Place over Cells) and it becomes the item's icon.")}</td></tr>
        </tbody>
      </table>
      </div>
      <p style="margin:10px 0"><a href="/api/import/template" download>${t("Download a template")}</a></p>

      <label for="imp-file">${t("File")}</label>
      <input id="imp-file" type="file" accept=".xlsx,.csv,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" ${S.online ? "" : "disabled"}>
      ${file ? `<p class="muted" style="margin-top:4px">${esc(file.name)}</p>` : ""}
      ${S.online ? "" : `<p class="warn-text">${t("Importing needs a connection.")}</p>`}
      ${busy ? `<p class="muted">${t("Reading the file…")}</p>` : ""}
      ${error ? `<p class="warn-text">${esc(error)}</p>` : ""}
    </div>

    ${preview ? `
    <div class="card">
      <h2>${errors.length ? tn(errors.length, "{n} problem — nothing will be imported", "{n} problems — nothing will be imported") : t("Ready to import")}</h2>
      ${errors.length ? `
      <p class="muted" style="margin-bottom:8px">${t("Fix these in the spreadsheet and choose the file again. Row numbers are the spreadsheet's own.")}</p>
      ${errors.map((e) => `
        <div style="padding:4px 0;border-bottom:1px solid var(--line)">
          ${e.row ? `<span class="badge">${t("row {n}", { n: e.row })}</span> ` : ""}${esc(e.message)}
        </div>`).join("")}` : ""}
      ${(preview.notes || []).map((note) => `<p class="muted" style="margin-bottom:6px">${esc(note)}</p>`).join("")}
      ${withPictures ? `<p class="muted" style="margin-bottom:6px">${t("{n} of {total} items have a picture, which will be set as their icon.", { n: withPictures, total: items.length })}</p>` : ""}
      ${(preview.projects || []).map((p) => `
        <p style="margin-top:8px"><strong>${esc(p.code)}</strong> ${p.is_new ? `<span class="badge">${t("new")}</span>` : `<span class="badge">${t("existing")}</span>`}
          <span class="muted"><bdi>${esc(p.name || "")}</bdi>${p.client ? ` · <bdi>${esc(p.client)}</bdi>` : ""} · ${tn(p.item_count, "{n} item", "{n} items")}</span></p>`).join("")}
      ${items.length ? `
      <div class="table-scroll" style="margin-top:8px">
      <table>
        <thead><tr><th scope="col">${t("Item")}</th><th scope="col" class="num">${t("Qty")}</th><th scope="col" class="num">${t("Sample")}</th><th scope="col">${t("Picture")}</th></tr></thead>
        <tbody>
          ${items.map((i) => `
            <tr>
              <td><strong>${esc(i.code)}</strong>${[i.area, i.phase].filter(Boolean).map((tag) => ` <span class="badge tag">${esc(tag)}</span>`).join("")}<br><span class="muted"><bdi>${esc(i.description)}</bdi> · ${esc(revLabel(i.revision))}</span></td>
              <td class="num">${i.qty}</td>
              <td class="num">${i.sample_qty}</td>
              <td>${i.has_picture ? "✓" : `<span class="muted">—</span>`}</td>
            </tr>`).join("")}
        </tbody>
      </table>
      </div>` : ""}
      ${ready ? `
      <div style="height:12px"></div>
      <button class="primary" id="imp-go" ${S.online && !busy ? "" : "disabled"}>${tn(items.length, "Import {n} item", "Import {n} items")}</button>` : ""}
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
        : (detail?.reason || detail || (isNetworkError(error) ? t("No connection.") : t("Could not read the file.")));
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
        toast(pictures
          ? t("Imported {items} with {pictures}", { items: tn(done.items.length, "{n} item", "{n} items"), pictures: tn(pictures, "{n} picture", "{n} pictures") })
          : tn(done.items.length, "Imported {n} item", "Imported {n} items"));
        await sync();
        navigate("#/items");
      } catch (error) {
        const detail = error.body?.detail;
        importState.busy = false;
        // Someone else created one of these codes since the preview: show the
        // fresh list of problems rather than a bare refusal.
        if (detail?.errors) importState.preview = { ...detail, items: detail.items || [] };
        else importState.error = detail?.reason || detail || t("Import failed.");
        rerender();
      }
    };
  }
}

/* ---------------------------------------------------------- office: items -- */

/* The area and phase pickers of the New item form, for one project. */
function newItemLabels(projectId) {
  return `
          <div><label for="ni-area">${t("Area")}</label>${labelSelect("areas", sessionStorage.getItem("ni-area"), `id="ni-area"`, projectId)}</div>`;
}

/* The New item form's phases: one row is one batch. Kept here, not in the
   DOM, so the background re-render redraws added rows instead of losing them. */
let niPhases = [{ phase: sessionStorage.getItem("ni-phase") || "", qty: "" }];

function newItemPhaseRows(projectId) {
  const many = niPhases.length > 1;
  return niPhases.map((row, index) => `
        <div class="field-grid" style="align-items:end">
          <div><label>${index ? t("Another phase") : t("Phase")}</label>${labelSelect("phases", row.phase, `data-ni-phase="${index}"`, projectId)}</div>
          <div><label>${many ? t("Qty in this phase") : t("Batch qty")}</label>
            <div style="display:flex;gap:6px">
              <input data-ni-pqty="${index}" type="number" inputmode="numeric" min="1" value="${esc(row.qty)}">
              ${index ? `<button class="ghost" data-ni-drop="${index}" aria-label="${esc(t("Remove this phase"))}" style="width:auto;padding:8px 12px">✕</button>` : ""}
            </div>
          </div>
        </div>`).join("") + (many ? `
        <p class="muted" id="ni-total" style="margin-top:6px"></p>` : "");
}

/* One item, added by hand: the extra wardrobe the client asked for after the
   import, the piece nobody put on the schedule. Open to every engineer, like
   the import -- adding work to a project is not an admin job; rewriting what
   is already there is. The form is the item's facts and nothing else: it has
   no stages to pick, because an item can be logged at any stage. */
function officeItems(body, itemId) {
  // One item, on a page of its own: #/office/items/<id>.
  if (itemId) {
    body.innerHTML = `
    <div class="card">
      <a class="back-link" href="#/office/items"><span aria-hidden="true">‹</span> ${t("All items")}</a>
      <div id="of-items" class="muted" style="margin-top:8px">${t("Loading…")}</div>
    </div>`;
    loadItemEditors();
    return;
  }
  const projects = (S.ref?.projects || []).filter((p) => p.is_active !== false);
  const remembered = sessionStorage.getItem("ni-project") || "";

  body.innerHTML = `
    <div class="card">
      <details data-panel="new-item">
        <summary style="cursor:pointer"><strong>${t("New item")}</strong>
          <span class="muted">${t("— add one to a project without an import")}</span></summary>
        ${projects.length ? `
        <label for="ni-project" style="margin-top:10px">${t("Project")}</label>
        <select id="ni-project">
          ${projects.map((p) => `<option value="${p.id}" ${p.id === remembered ? "selected" : ""}>${esc(p.code)} — ${esc(p.name)}</option>`).join("")}
        </select>
        <div class="field-grid">
          <div><label for="ni-code">${t("Item code")}</label><input id="ni-code" maxlength="64" autocapitalize="characters" autocomplete="off" dir="ltr"></div>
          <div><label for="ni-rev">${t("Drawing revision")}</label><input id="ni-rev" maxlength="32" autocapitalize="characters" placeholder="A" dir="ltr"></div>
        </div>
        <label for="ni-desc">${t("Description")}</label>
        <input id="ni-desc" maxlength="255" autocomplete="off" dir="auto">
        <label for="ni-dims">${t("Dimensions")}</label>
        <input id="ni-dims" maxlength="255" autocomplete="off" dir="auto" placeholder="${esc(t("e.g. W 600 x D 450 x H 720 mm"))}">
        <div class="field-grid" id="ni-labels">${newItemLabels(projects.some((p) => p.id === remembered) ? remembered : projects[0].id)}</div>
        <div id="ni-phases">${newItemPhaseRows(projects.some((p) => p.id === remembered) ? remembered : projects[0].id)}</div>
        <button class="ghost" id="ni-add-phase" style="width:auto;padding:8px 14px;margin-top:8px">${t("+ Split across another phase")}</button>
        <p class="muted" style="margin-top:6px">${t("Area and phase are optional, from the project's own lists. Each phase is its own batch with its own quantity, under the same code.")}</p>
        <div class="field-grid">
          <div><label for="ni-sample">${t("Sample qty")}</label><input id="ni-sample" type="number" inputmode="numeric" min="0" placeholder="1"></div>
        </div>
        <p class="muted" style="margin-top:6px">${t("The sample is built first; the batch is released when the sample is logged Done. Sample 0 puts the whole batch on the floor at once. Split across phases, one sample serves them all: the first phase builds it, and every phase is released when it is done.")}</p>
        <div style="height:8px"></div>
        <button class="primary" id="ni-go" ${S.online ? "" : "disabled"}>${S.online ? t("Add item") : t("Adding an item needs a connection")}</button>
        <p class="warn-text" id="ni-err" role="alert" hidden></p>`
        : `<p class="muted" style="margin-top:10px">${t("There is no active project to add it to. Projects arrive through")} <a href="#/office/import">${t("Import")}</a>.</p>`}
      </details>
    </div>

    <div class="card">
      <h2>${t("Edit items")}</h2>
      <input id="oi-q" type="search" value="${esc(sessionStorage.getItem("oi-q") || "")}" style="margin-bottom:8px"
             placeholder="${esc(t("Find an item — code, description, project, area or phase"))}">
      <label class="pick-row" style="border:0;min-height:0;padding:0 0 8px">
        <input type="checkbox" id="oi-archived" ${sessionStorage.getItem("oi-archived") ? "checked" : ""}><span>${t("Show archived items too")}</span>
      </label>
      <div id="of-items" class="muted">${t("Loading…")}</div>
    </div>`;

  const go = document.getElementById("ni-go");
  if (go) {
    const value = (id) => document.getElementById(id).value.trim();
    const $phases = document.getElementById("ni-phases");
    const total = () => {
      const line = document.getElementById("ni-total");
      if (!line) return;
      const sum = niPhases.reduce((a, row) => a + (Number(row.qty) || 0), 0);
      line.textContent = t("{qty} across {n} phases", { qty: pcs(sum), n: niPhases.length });
    };
    const paintPhases = () => { $phases.innerHTML = newItemPhaseRows(value("ni-project")); total(); };
    // One listener each for every row: rows come and go, the container stays.
    $phases.onchange = (e) => {
      const index = e.target.dataset?.niPhase;
      if (index === undefined) return;
      niPhases[Number(index)].phase = e.target.value;
      // The next one is usually for the same phase.
      if (index === "0") sessionStorage.setItem("ni-phase", e.target.value);
    };
    $phases.oninput = (e) => {
      const index = e.target.dataset?.niPqty;
      if (index === undefined) return;
      niPhases[Number(index)].qty = e.target.value;
      total();
    };
    $phases.onclick = (e) => {
      const index = e.target.dataset?.niDrop;
      if (index === undefined) return;
      niPhases.splice(Number(index), 1);
      paintPhases();
    };
    document.getElementById("ni-add-phase").onclick = () => {
      niPhases.push({ phase: "", qty: "" });
      paintPhases();
    };
    total();
    // The next one is usually for the same job and area.
    const remember = () => {
      document.getElementById("ni-area").onchange = (e) => { sessionStorage.setItem("ni-area", e.target.value); };
    };
    document.getElementById("ni-project").onchange = (e) => {
      sessionStorage.setItem("ni-project", e.target.value);
      // Another project, other lists: the phases picked no longer exist there.
      document.getElementById("ni-labels").innerHTML = newItemLabels(e.target.value);
      niPhases = niPhases.map((row) => ({ ...row, phase: "" }));
      paintPhases();
      remember();
    };
    remember();
    go.onclick = async () => {
      const err = document.getElementById("ni-err");
      const fail = (message) => { err.textContent = message; err.hidden = false; };
      err.hidden = true;
      const code = value("ni-code");
      const qtys = niPhases.map((row) => Number(row.qty));
      const qty = qtys.reduce((a, b) => a + b, 0);
      const sample = value("ni-sample") === "" ? 1 : Number(value("ni-sample"));
      const split = niPhases.length > 1;
      if (!code) return fail(t("The item needs a code."));
      if (qtys.some((n) => !Number.isInteger(n) || n < 1)) {
        return fail(split ? t("Each phase needs a quantity: a whole number, 1 or more.") : t("Batch qty must be a whole number, 1 or more."));
      }
      if (split && new Set(niPhases.map((row) => row.phase)).size !== niPhases.length) {
        return fail(t("Each phase once: two rows name the same phase."));
      }
      if (!Number.isInteger(sample) || sample < 0) return fail(t("Sample qty must be a whole number, 0 or more."));
      go.disabled = true;
      try {
        const created = await api("/api/items", {
          method: "POST",
          body: JSON.stringify({
            project_id: value("ni-project"),
            code,
            description: value("ni-desc"),
            dimensions: value("ni-dims"),
            area_id: value("ni-area") || null,
            ...(split
              ? { phase_quantities: niPhases.map((row) => ({ phase_id: row.phase || null, qty: Number(row.qty) })) }
              : { phase_id: niPhases[0].phase || null }),
            total_qty: qty,
            sample_qty: sample,
            drawing_revision: value("ni-rev") || "A",
          }),
        });
        toast(split
          ? t("{code} added in {n} phases — they are on the Items list now", { code: created.code, n: created.phases.length })
          : t("{name} added — it is on the Items list now", { name: itemName(created) }));
        // Clear what was typed, but not the project or the first phase: the
        // next one is usually for the same job.
        for (const id of ["ni-code", "ni-desc", "ni-dims", "ni-sample", "ni-rev"]) {
          document.getElementById(id).value = "";
        }
        niPhases = [{ phase: niPhases[0].phase, qty: "" }];
        paintPhases();
        dirtyFields.clear();
        await sync();
        loadItemEditors();
      } catch (error) {
        const detail = error.body?.detail;
        fail(typeof detail === "string" ? detail
          : Array.isArray(detail) ? rejectReason(error)
          : isNetworkError(error) ? t("No connection — the item was not added.")
          : t("Could not add the item."));
      } finally {
        go.disabled = !S.online;
      }
    };
  }

  loadItemEditors();
}

/* Finding the item to edit. Every item in the factory is behind this list, so
   it is a lookup first: type part of a code, a description, a project, an
   area or a phase (commas separate alternatives, words narrow), and open the
   one you want on a page of its own. */
const LOOKUP_SHOWN = 60;
function itemLookup(target, items, projectCode) {
  const rows = () => {
    const matches = filingMatcher(sessionStorage.getItem("oi-q") || "");
    const archivedToo = !!sessionStorage.getItem("oi-archived");
    const pool = items.filter((item) => archivedToo || item.is_active);
    const found = pool.filter(matches);
    const pieceCode = (item) => items.find((i) => i.id === item.parent_item_id)?.code;
    return `
      <p class="muted" style="margin:0 0 4px">${found.length === pool.length ? tn(pool.length, "{n} item", "{n} items") : t("{n} of {total} items", { n: found.length, total: pool.length })}</p>
      ${found.slice(0, LOOKUP_SHOWN).map((item) => `
      <a class="lookup-row" href="#/office/items/${item.id}">
        ${item.icon_url ? `<img class="item-icon" src="${thumbOf(item.icon_url)}" alt="" loading="lazy">` : ""}
        <span style="min-width:0;flex:1">
          <strong>${esc(item.code)}</strong>
          <span class="badge">${esc(projectCode.get(item.project_id) || t("no project"))}</span>
          ${itemTags(item)}
          ${item.parent_item_id ? `<span class="badge tag">${esc(t("part of {code}", { code: pieceCode(item) || "?" }))}</span>` : ""}
          ${item.is_active ? "" : `<span class="badge">${t("archived")}</span>`}
          <br><span class="muted"><bdi>${esc(item.description)}</bdi> · ${esc(batchLabel(item))}${item.sample_qty ? ` + ${t("{n} sample", { n: item.sample_qty })}` : ""} · ${esc(revLabel(item.drawing_revision))}</span>
        </span>
        <span class="chev" aria-hidden="true">›</span>
      </a>`).join("")}
      ${found.length > LOOKUP_SHOWN ? `<p class="muted" style="padding:8px 0">${t("Showing the first {n}. Type more to narrow it down.", { n: LOOKUP_SHOWN })}</p>` : ""}
      ${found.length ? "" : `<p class="muted" style="padding:8px 0">${pool.length ? t("Nothing matches.") : t("No items yet.")}</p>`}`;
  };
  target.classList.remove("muted");
  target.innerHTML = rows();
  // Typing redraws the rows only: the box keeps its focus and the keyboard.
  const search = document.getElementById("oi-q");
  if (search) search.oninput = () => { sessionStorage.setItem("oi-q", search.value); target.innerHTML = rows(); };
  const archived = document.getElementById("oi-archived");
  if (archived) archived.onchange = () => {
    if (archived.checked) sessionStorage.setItem("oi-archived", "1"); else sessionStorage.removeItem("oi-archived");
    target.innerHTML = rows();
  };
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
      if (target.isConnected) target.textContent = t("Photos need a connection.");
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
              <img src="${img.thumb_url || img.url}" alt="${esc(img.note || img.filename)}" loading="lazy">
            </a>
            ${img.kind === "icon" ? `<span class="badge" style="position:absolute;bottom:4px;inset-inline-start:4px;background:rgba(0,0,0,.55)">${t("icon")}</span>` : ""}
            ${canDelete ? `<button class="drop" data-drop="${img.id}" aria-label="${esc(t("Delete this photo"))}"
                    title="${esc(t("Delete this photo"))}" ${S.online ? "" : "disabled"}>✕</button>` : ""}
          </div>`).join("")}
       </div>
       ${canDelete ? "" : `<p class="muted" style="margin-top:6px">${t("Deleting a photo is an admin job.")}</p>`}`
    : t("No photos on this item.");

  target.querySelectorAll("[data-drop]").forEach((button) => {
    const image = images.find((img) => img.id === button.dataset.drop);
    button.onclick = async () => {
      const question = image.kind === "icon" ? t("Delete this icon? The picture goes for good. The item and its logged history are untouched.")
        : image.note ? t("Delete the photo \"{note}\"? The picture goes for good. The item and its logged history are untouched.", { note: image.note })
        : t("Delete this photo? The picture goes for good. The item and its logged history are untouched.");
      if (!confirm(question)) return;
      try {
        await api(`/api/images/${image.id}`, { method: "DELETE" });
        forgetImage(image.url);
        editorPhotos.delete(itemId);
        logScreenCache.fetchedAt = 0; // the log screen's copy is stale too
        toast(t("Photo deleted"));
        // Only an icon changes anything the item list is drawn from.
        if (image.kind === "icon") await sync();
        await loadEditorPhotos(itemId, canDelete);
      } catch (error) {
        alert(error.body?.detail || t("Could not delete the photo."));
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
    if (target.isConnected) target.textContent = t("Editing items needs a connection.");
    return;
  }
  if (!target.isConnected) return;

  const allProjects = S.ref?.projects || [];
  // The badge must name an archived project too, so the lookup keeps every one.
  const projectCode = new Map(allProjects.map((p) => [p.id, p.code]));

  // Which of the two this is: the lookup, or one item's own page.
  const [, , , openId] = (location.hash || "").split("/");
  if (!openId) { itemLookup(target, items, projectCode); return; }
  const open = items.find((i) => i.id === openId);
  if (!open) {
    target.innerHTML = `<p class="muted">${t("That item is not there any more — it may have been deleted.")}</p>`;
    return;
  }
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
        <label>${t("Project")}</label>
        <select data-it-project="${item.id}" ${lock}>
          ${projects.map((p) => option(p, p.id === item.project_id)).join("")}
        </select>`;
    }
    return `
        <label>${t("Project")}</label>
        <p class="muted">${esc(projectCode.get(item.project_id) || t("no project"))} — ${t("archived")}</p>
        <label>${t("Move to a live project")}</label>
        <select data-it-project="${item.id}" ${lock}>
          <option value="" selected>${t("Leave it where it is")}</option>
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
          <div><label>${t("Area")}</label>${labelSelect("areas", own ? item.area_id : null, `data-it-area="${item.id}" ${lock}`, projectId)}</div>
          <div><label>${t("Phase")}</label>${labelSelect("phases", own ? item.phase_id : null, `data-it-phase="${item.id}" ${lock}`, projectId)}</div>`;
  };

  const piece = open.parent_item_id ? items.find((i) => i.id === open.parent_item_id) : null;
  target.innerHTML = [open].map((item) => `
      <div style="display:flex;gap:12px;align-items:center">
        ${item.icon_url ? `<img class="item-icon" src="${thumbOf(item.icon_url)}" alt="">` : ""}
        <div style="flex:1;min-width:0">
          <h2 style="margin:0">${esc(item.code)}</h2>
          <div style="margin:3px 0">
            <span class="badge">${esc(projectCode.get(item.project_id) || t("no project"))}</span>
            ${itemTags(item)}
            ${item.is_active ? "" : `<span class="badge">${t("archived")}</span>`}
          </div>
          <span class="muted"><bdi>${esc(item.description)}</bdi> · ${esc(batchLabel(item))}${item.sample_qty ? ` + ${t("{n} sample", { n: item.sample_qty })}` : ""} · ${esc(revLabel(item.drawing_revision))}</span>
          ${piece ? `<br><span class="muted">${t("A part of")} <a href="#/office/items/${piece.id}">${esc(itemName(piece))}</a></span>` : ""}
        </div>
      </div>
      <div style="padding:8px 0 4px">
        <!-- Readable by anyone, editable by admins: a code or a batch size is
             what the rest of the floor reads the work by. -->
        <div class="field-grid">
          <div><label>${t("Code")}</label><input data-it-code="${item.id}" value="${esc(item.code)}" dir="ltr" ${isAdmin ? "" : "disabled"}></div>
          ${item.parent_item_id
            // A part has no batch of its own: its units come from splitting
            // the piece it belongs to. Its count per piece is under that piece's Parts.
            ? `<div><label>${t("Per piece")}</label><p class="muted" style="margin:10px 0 0">${esc(batchLabel(item))}</p></div>`
            : `<div><label>${t("Batch qty")}</label><input data-it-qty="${item.id}" type="number" inputmode="numeric" min="1" value="${item.total_qty}" ${isAdmin ? "" : "disabled"}></div>`}
        </div>
        <label>${t("Description")}</label>
        <input data-it-desc="${item.id}" value="${esc(item.description)}" dir="auto" ${isAdmin ? "" : "disabled"}>
        <label>${t("Dimensions")}</label>
        <input data-it-dims="${item.id}" maxlength="255" value="${esc(item.dimensions || "")}" dir="auto"
               placeholder="${esc(t("e.g. W 600 x D 450 x H 720 mm"))}" ${isAdmin ? "" : "disabled"}>
        ${projectField(item)}
        <div class="field-grid" data-it-labels="${item.id}">${editorLabels(item, item.project_id)}</div>
        ${isAdmin ? `
        <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
          <button class="ghost" data-it-save="${item.id}" style="width:auto;padding:10px 16px" ${S.online ? "" : "disabled"}>${t("Save")}</button>
        </div>`
        : `<p class="muted" style="margin-top:10px">${t("Changing these is an admin job. Creating items, logging entries and bumping the revision are not.")}</p>`}

        ${item.is_active ? partsEditor(item, items) : ""}
        ${item.is_active && isAdmin ? phaseMover(item, items) : ""}

        <label style="margin-top:12px">${t("Item icon")}</label>
        <div style="display:flex;gap:10px;align-items:center">
          ${item.icon_url ? `
          <div class="photo-tile icon-tile">
            <a href="${item.icon_url}" target="_blank" rel="noopener"><img class="item-icon" src="${thumbOf(item.icon_url)}" alt=""></a>
            ${isAdmin ? `<button class="drop" data-drop-icon="${item.id}" aria-label="${esc(t("Delete the icon"))}"
                    title="${esc(t("Delete the icon"))}" ${S.online ? "" : "disabled"}>✕</button>` : ""}
          </div>` : `<span class="muted">${t("None yet.")}</span>`}
          <div style="flex:1">
            ${photoPicker(`icon:${item.id}`, {
              camera: t("Photograph it"),
              gallery: t("From gallery"),
              single: item.icon_url ? t("Replace icon") : t("Set icon"),
              offline: t("Needs a connection"),
            })}
          </div>
        </div>
        <p data-it-upload="${item.id}" class="muted" style="margin-top:6px" hidden></p>

        <label style="margin-top:12px">${t("Photos")}</label>
        <div data-it-photos="${item.id}" class="muted">${
          editorPhotos.has(item.id) ? "" : t("Loading…")
        }</div>

        <label style="margin-top:12px">${esc(t("Drawing revision — currently {rev}", { rev: item.drawing_revision }))}</label>
        <div class="qty-row">
          <input data-it-rev="${item.id}" placeholder="${esc(t("new rev, e.g. {rev}", { rev: nextRevision(item.drawing_revision) }))}"
                 autocapitalize="characters" maxlength="32" style="flex:1;text-align:start;padding:10px">
          <button class="ghost" data-it-bump="${item.id}" style="width:auto;padding:10px 16px" ${S.online ? "" : "disabled"}>${t("Bump")}</button>
        </div>
        <p class="muted" style="margin-top:4px">${esc(t("{qty} in flight were built to rev {rev}.", { qty: pcs(inFlightQty(item)), rev: item.drawing_revision }))}</p>

        ${isAdmin ? `
        <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap">
          ${item.is_active
            ? `<button class="ghost" data-it-archive="${item.id}" style="width:auto;padding:10px 16px" ${S.online ? "" : "disabled"}>${t("Archive")}</button>`
            : `<button class="ghost" data-it-restore="${item.id}" style="width:auto;padding:10px 16px" ${S.online ? "" : "disabled"}>${t("Reactivate")}</button>`}
          <button class="ghost" data-it-purge="${item.id}" style="width:auto;padding:10px 16px;border-color:var(--warn);color:var(--warn-text)" ${S.online ? "" : "disabled"}>${t("Delete for good")}</button>
        </div>
        <p class="muted" style="margin-top:6px">${t("Archiving hides the item everywhere and keeps its history; an archived item can be reactivated. Deleting for good removes the item and its logged events, and cannot be undone.")}</p>` : ""}
      </div>`).join("");

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
        dimensions: field("data-it-dims", id).value.trim(),
        area_id: field("data-it-area", id).value || null,
        phase_id: field("data-it-phase", id).value || null,
      };
      const qtyField = field("data-it-qty", id);
      if (qtyField) body.total_qty = Number(qtyField.value) || byId.get(id).total_qty;
      // Empty means "leave it where it is": the item's project is archived and
      // nothing was picked to move it to.
      const projectId = field("data-it-project", id).value;
      if (projectId) body.project_id = projectId;
      try {
        await api(`/api/items/${id}`, { method: "PATCH", body: JSON.stringify(body) });
        toast(t("Item updated"));
        await sync();
      } catch (error) {
        alert(error.body?.detail || t("Could not update the item."));
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
        toast(t("Item icon updated"));
        editorPhotos.delete(id); // the item just gained a photo
        await sync();
      } catch (error) {
        status.fail(error.detail);
      } finally {
        clearPicker(`icon:${id}`);
      }
    };
  });

  // The ✕ on the icon. The icon is the newest icon-kind picture, so deleting
  // it brings back the one before, if there was one.
  target.querySelectorAll("[data-drop-icon]").forEach((button) => {
    button.onclick = async () => {
      const item = byId.get(button.dataset.dropIcon);
      if (!item?.icon_url) return;
      if (!confirm(t("Delete the icon of {code}? The picture goes for good; an earlier icon, if there was one, takes its place.", { code: item.code }))) return;
      button.disabled = true;
      try {
        await api(`/api/images/${iconId(item)}`, { method: "DELETE" });
        forgetImage(item.icon_url);
        editorPhotos.delete(item.id);
        logScreenCache.fetchedAt = 0;
        toast(t("Icon deleted"));
        await sync(); // the item list is drawn with the icon
        loadItemEditors();
      } catch (error) {
        button.disabled = !S.online;
        alert(error.body?.detail || t("Could not delete the photo."));
      }
    };
  });

  // The page is one item's, so its photos load with it.
  loadEditorPhotos(open.id, isAdmin);

  bindPartsEditor(target, byId);
  bindPhaseMover(target, byId);

  target.querySelectorAll("[data-it-bump]").forEach((button) => {
    button.onclick = async () => {
      const id = button.dataset.itBump;
      const item = byId.get(id);
      const revision = field("data-it-rev", id).value.trim();
      if (!revision) return;
      const affected = inFlightQty(item);
      const detail = affected
        ? t("{qty} in flight were built to rev {rev}.", { qty: pcs(affected), rev: item.drawing_revision })
        : t("Nothing is in flight yet.");
      if (!confirm(`${t("Bump {code} from rev {from} to {to}?", { code: item.code, from: item.drawing_revision, to: revision })} ${detail}`)) return;
      try {
        await api(`/api/items/${id}/revision`, {
          method: "POST",
          body: JSON.stringify({ drawing_revision: revision }),
        });
        toast(t("{code} bumped to rev {rev}", { code: item.code, rev: revision }));
        await sync();
      } catch (error) {
        alert(error.body?.detail || t("Could not bump the revision."));
      }
    };
  });

  target.querySelectorAll("[data-it-archive]").forEach((button) => {
    button.onclick = async () => {
      const item = byId.get(button.dataset.itArchive);
      if (!confirm(t("Archive {code}? It disappears from lists and reports; its history stays.", { code: item.code }))) return;
      try {
        const result = await api(`/api/items/${item.id}`, { method: "DELETE" });
        toast(result.archived ? t("Item archived (history kept)") : t("Item deleted"));
        await sync();
        if (result.archived) loadItemEditors();
        else navigate("#/office/items");
      } catch (error) {
        alert(error.body?.detail || t("Could not remove the item."));
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
        toast(t("{code} is back on the floor's lists", { code: item.code }));
        await sync();
        loadItemEditors();
      } catch (error) {
        alert(error.body?.detail || t("Could not reactivate the item."));
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
        `${t("Delete {name} for good?", { name: itemName(item) })}\n\n` +
        `${t("This destroys the item and its {n} logged event(s). It cannot be undone — archiving is what you want for anything really built.", { n: events })}\n\n` +
        t("Type {code} to confirm:", { code: item.code })
      );
      if (typed === null) return;
      if (typed.trim() !== item.code) { alert(t("The code did not match; nothing was deleted.")); return; }
      try {
        const result = await api(`/api/items/${item.id}?purge=true`, { method: "DELETE" });
        toast(t("{code} deleted for good ({n} event(s) destroyed)", { code: item.code, n: result.purged_events }));
        await sync();
        navigate("#/office/items");
      } catch (error) {
        alert(error.body?.detail || t("Could not delete the item."));
      }
    };
  });
}

/* Parts, in an item's editor. A part is an item of its own (its code, its
   log) that is split off the piece on the floor and assembled back into it.
   How many go into one piece can only change while no pieces are out in
   parts: they were split by the old count. */
function partsEditor(item, items) {
  const parts = items.filter((i) => i.parent_item_id === item.id && i.is_active);
  const busy = !S.online ? "disabled" : "";
  return `
        <label style="margin-top:12px">${t("Parts")}</label>
        ${parts.length ? parts.map((part) => `
        <div class="spread" style="gap:8px;padding:4px 0">
          <a href="#/office/items/${part.id}" style="min-width:0;color:inherit"><strong>${esc(part.code)}</strong> <span class="muted"><bdi>${esc(part.description)}</bdi></span></a>
          <span style="display:flex;gap:6px;align-items:center;flex:none">
            <input data-pt-per="${part.id}" type="number" inputmode="numeric" min="1" value="${part.per_unit}"
                   aria-label="${esc(t("How many per piece"))}" style="width:70px;padding:8px">
            <span class="muted">${t("per piece")}</span>
            <button class="ghost" data-pt-save="${part.id}" style="width:auto;padding:8px 12px" ${busy}>${t("Save")}</button>
          </span>
        </div>`).join("") : `<p class="muted">${t("None — it goes through the factory as one piece.")}</p>`}
        <div class="field-grid">
          <div><label>${t("New part's code")}</label><input data-pt-code="${item.id}" maxlength="64" dir="ltr" value="${esc(item.code)}-"></div>
          <div><label>${t("Name")}</label><input data-pt-desc="${item.id}" maxlength="255" dir="auto" placeholder="${esc(t("e.g. Headboard"))}"></div>
        </div>
        <div class="field-grid">
          <div><label>${t("How many per piece")}</label><input data-pt-n="${item.id}" type="number" inputmode="numeric" min="1" value="1"></div>
          <div style="align-self:end"><button class="ghost" data-pt-add="${item.id}" style="padding:10px 16px" ${busy}>${t("Add part")}</button></div>
        </div>
        ${item.state?.in_parts_qty ? `<p class="muted">${t("{n} piece(s) are out in parts. Parts cannot be added or recounted until they are assembled back.", { n: item.state.in_parts_qty })}</p>` : ""}`;
}

function bindPartsEditor(target, byId) {
  const value = (attr, id) => target.querySelector(`[${attr}="${id}"]`)?.value ?? "";
  target.querySelectorAll("[data-pt-add]").forEach((button) => {
    button.onclick = async () => {
      const id = button.dataset.ptAdd;
      const code = value("data-pt-code", id).trim();
      const perUnit = Number(value("data-pt-n", id));
      if (!code || code === `${byId.get(id).code}-`) { alert(t("Give the part a code.")); return; }
      if (!Number.isInteger(perUnit) || perUnit < 1) { alert(t("How many per piece: a whole number, 1 or more.")); return; }
      try {
        await api(`/api/items/${id}/parts`, {
          method: "POST",
          body: JSON.stringify({ code, description: value("data-pt-desc", id).trim(), per_unit: perUnit }),
        });
        toast(t("{name} added as a part of {code}", { name: code, code: byId.get(id).code }));
        await sync();
        loadItemEditors();
      } catch (error) {
        alert(error.body?.detail || t("Could not add the part."));
      }
    };
  });
  target.querySelectorAll("[data-pt-save]").forEach((button) => {
    button.onclick = async () => {
      const id = button.dataset.ptSave;
      const perUnit = Number(value("data-pt-per", id));
      if (!Number.isInteger(perUnit) || perUnit < 1) { alert(t("How many per piece: a whole number, 1 or more.")); return; }
      try {
        await api(`/api/items/${id}/part`, { method: "PATCH", body: JSON.stringify({ per_unit: perUnit }) });
        toast(t("Part updated"));
        await sync();
        loadItemEditors();
      } catch (error) {
        alert(error.body?.detail || t("Could not change the part."));
      }
    };
  });
}

/* Some of a batch into another phase of its project. Before the batch is
   released it is planning -- quantity moves from one batch's total to the
   other's, and the other waits on this one's sample. After, it is real units,
   taken from where they are and arriving at the same place. */
function phaseMover(item, items) {
  if (item.parent_item_id || items.some((i) => i.parent_item_id === item.id)) return "";
  const phases = (S.ref?.phases || []).filter((ph) => ph.project_id === item.project_id && ph.is_active && ph.id !== item.phase_id);
  const options = [
    ...phases.map((ph) => `<option value="${ph.id}">${esc(ph.name)}</option>`),
    ...(item.phase_id ? [`<option value="">${t("No phase")}</option>`] : []),
  ];
  if (!options.length) return "";
  const planned = item.state?.phase !== "production";
  const places = planned ? [] : (item.state?.positions || [])
    .map((p) => ({ key: p.is_unstarted ? UNSTARTED : p.stage_id, name: p.is_unstarted ? t("Not started") : localName(stageById(p.stage_id)) || p.stage_name, qty: p.qty - (p.sample_qty || 0) }))
    .filter((p) => p.qty > 0);
  if (!planned && !places.length) return "";
  return `
        <label style="margin-top:12px">${t("Move some into another phase")}</label>
        <select data-ph-to="${item.id}">${options.join("")}</select>
        ${planned ? `
        <label>${t("How many of the planned {n}", { n: item.total_qty })}</label>
        <input data-ph-take="${item.id}" data-ph-key="${UNSTARTED}" type="number" inputmode="numeric" min="0" max="${item.total_qty}" placeholder="0">
        <p class="muted" style="margin-top:4px">${t("Nothing is on the floor yet. The other phase's batch is released by this item's sample.")}</p>`
        : places.map((p) => `
        <div class="spread" style="gap:8px;padding:3px 0">
          <span class="muted"><bdi>${esc(p.name)}</bdi> · ${p.qty}</span>
          <input data-ph-take="${item.id}" data-ph-key="${p.key}" type="number" inputmode="numeric" min="0" max="${p.qty}"
                 placeholder="0" aria-label="${esc(t("How many from {place}", { place: p.name }))}" style="width:90px;padding:8px">
        </div>`).join("")}
        <button class="ghost" data-ph-go="${item.id}" style="width:auto;padding:10px 16px;margin-top:6px" ${S.online ? "" : "disabled"}>${t("Move")}</button>`;
}

function bindPhaseMover(target, byId) {
  target.querySelectorAll("[data-ph-go]").forEach((button) => {
    button.onclick = async () => {
      const id = button.dataset.phGo;
      const item = byId.get(id);
      const take = {};
      for (const input of target.querySelectorAll(`[data-ph-take="${id}"]`)) {
        const qty = Number(input.value || 0);
        if (!Number.isInteger(qty) || qty < 0) { alert(t("Quantities are whole numbers.")); return; }
        if (qty) take[input.dataset.phKey] = qty;
      }
      const total = Object.values(take).reduce((a, b) => a + b, 0);
      if (!total) { alert(t("Say how many to move.")); return; }
      const select = target.querySelector(`[data-ph-to="${id}"]`);
      const phaseName = select.options[select.selectedIndex]?.text || "";
      if (!confirm(t("Move {n} of {code} into {phase}?", { n: total, code: item.code, phase: phaseName }))) return;
      try {
        const moved = await api(`/api/items/${id}/split-phase`, {
          method: "POST", body: JSON.stringify({ phase_id: select.value || null, take }),
        });
        toast(t("{n} moved into {phase} ({name})", { n: total, phase: phaseName, name: itemName(moved.to) }));
        await sync();
        loadItemEditors();
      } catch (error) {
        alert(error.body?.detail || t("Could not move them."));
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
      <h2>${t("Edit projects")}</h2>
      <p class="muted" style="margin-bottom:8px">${t("New projects arrive with their items through")}
      <a href="#/office/import">${t("Import")}</a>.</p>
      ${projects.map((p) => `
        <details data-panel="project:${p.id}" style="padding:4px 0;border-bottom:1px solid var(--line)">
          <summary style="cursor:pointer;padding:6px 0">
            <strong>${esc(p.code)}</strong>${p.is_active ? "" : ` <span class="badge">${t("archived")}</span>`}
            <br><span class="muted"><bdi>${esc(p.name)}</bdi>${p.client ? ` · <bdi>${esc(p.client)}</bdi>` : ""}</span>
          </summary>
          <div style="padding:8px 0 12px">
            <label>${t("Name")}</label><input data-pr-name="${p.id}" value="${esc(p.name)}" dir="auto">
            <label>${t("Client")}</label><input data-pr-client="${p.id}" value="${esc(p.client || "")}" dir="auto">
            ${isAdmin ? `
            <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
              <button class="ghost" data-pr-save="${p.id}" style="width:auto;padding:10px 16px" ${S.online ? "" : "disabled"}>${t("Save")}</button>
              <button class="ghost" data-pr-toggle="${p.id}" style="width:auto;padding:10px 16px" ${S.online ? "" : "disabled"}>${p.is_active ? t("Archive") : t("Reactivate")}</button>
            </div>
            <p class="muted" style="margin-top:6px">${t("Archiving a project archives its items with it. Their history stays, and both can be reactivated. The code is fixed — it appears on drawings and emails that outlive this app.")}</p>`
            : `<p class="muted" style="margin-top:10px">${t("Editing projects is an admin job.")}</p>`}
          </div>
        </details>`).join("") || `<span class="muted">${t("No projects yet.")}</span>`}
    </div>`;

  const patchProject = async (id, patch, doneMessage) => {
    try {
      await api(`/api/projects/${id}`, { method: "PATCH", body: JSON.stringify(patch) });
      toast(doneMessage);
      await sync();
    } catch (error) {
      alert(error.body?.detail || t("Could not update the project."));
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
      }, t("Project updated"));
    };
  });

  body.querySelectorAll("[data-pr-toggle]").forEach((button) => {
    button.onclick = async () => {
      const project = projects.find((p) => p.id === button.dataset.prToggle);
      if (!project.is_active) {
        patchProject(project.id, { is_active: true }, t("Project reactivated"));
        return;
      }
      // S.items is the active list, so this is what will be archived with it.
      const live = S.items.filter((i) => i.project_id === project.id).length;
      const question = live
        ? t("Archive {code} and its {items}? They leave every list and report; their history stays, and they can be reactivated.", { code: project.code, items: tn(live, "{n} active item", "{n} active items") })
        : t("Archive {code}?", { code: project.code });
      if (!confirm(question)) return;
      try {
        const result = await api(`/api/projects/${project.id}?with_items=true`, { method: "DELETE" });
        toast(result.archived_items
          ? t("Project archived with {items}", { items: tn(result.archived_items, "{n} item", "{n} items") })
          : t("Project archived"));
        await sync();
      } catch (error) {
        alert(error.body?.detail || t("Could not archive the project."));
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
      <option value="">${t("— none —")}</option>
      ${rows.map((row) => `<option value="${row.id}" ${row.id === picked ? "selected" : ""}>${esc(row.name)}${row.is_active ? "" : ` ${t("(retired)")}`}</option>`).join("")}
    </select>`;
}

/* ------------------------------------------------- office: areas & phases -- */

/* Two lists of the same shape, edited the same way: which part of a job a
   batch is for, and which phase of it. They are what the Area and Phase
   filters are made of. Renaming one re-labels every item filed under it;
   retiring one takes it off the pickers and leaves its items where they are. */
/* Every sentence the page says about either list is written out whole here:
   "an area" and "a phase" do not come out of one pattern in Arabic, any more
   than they do in English. */
const LABEL_LISTS = [
  { list: "areas", field: "area_id",
    title: N_("Areas of {code}"),
    blurb: N_("The parts of a job: guest rooms, the spa, the all-day dining room."),
    example: N_("e.g. Guest rooms"),
    none: N_("No areas yet."), add: N_("Add an area"),
    updateFailed: N_("Could not update the area."), deleteFailed: N_("Could not delete the area."),
    addFailed: N_("Could not add the area."),
    deleteQuestion: N_("Delete {name}? It is removed from {code}'s areas for good.") },
  { list: "phases", field: "phase_id",
    title: N_("Phases of {code}"),
    blurb: N_("The phases a job is built and delivered in."),
    example: N_("e.g. Phase 1"),
    none: N_("No phases yet."), add: N_("Add a phase"),
    updateFailed: N_("Could not update the phase."), deleteFailed: N_("Could not delete the phase."),
    addFailed: N_("Could not add the phase."),
    deleteQuestion: N_("Delete {name}? It is removed from {code}'s phases for good.") },
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
  // How many of a ticked item go into the phase, as typed; blank is all of it.
  qty: {},
};
// The batch as it stands, units moved between phases included.
const batchOf = (item) => item.state?.batch_qty ?? item.total_qty;
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
    // A part is filed with the piece it belongs to, and follows it.
    && !item.parent_item_id
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
        ${own(list).map((row) => `<option value="${row.id}" ${row.id === picked ? "selected" : ""}>${esc(row.name)}${row.is_active ? "" : ` ${t("(retired)")}`}</option>`).join("")}`;
  const target = (list, picked) => `
        <option value="${KEEP}">${t("— leave as it is —")}</option>
        <option value="${NONE}" ${picked === NONE ? "selected" : ""}>${t("— none —")}</option>
        ${own(list).filter((row) => row.is_active).map((row) => `<option value="${row.id}" ${row.id === picked ? "selected" : ""}>${esc(row.name)}</option>`).join("")}`;
  return `
    <div class="card">
      <h2>${t("File items under an area or phase")}</h2>
      <p class="muted" style="margin-bottom:8px">${t("Search this project's items, tick them, choose where they go, apply. The ticks stay while you search again, so one batch can be gathered from several searches. Each ticked item gets a quantity box for filing into a phase: leave it empty to send the whole batch, or type how many go — the rest stay in the phase they are in.")}</p>
      <input id="fl-q" type="search" value="${esc(filing.q)}" style="margin-bottom:8px"
             placeholder="${esc(t("Search — several at once with commas: bed base, LF-03"))}">
      <div class="filters">
        <select id="fl-in-area" aria-label="${esc(t("Currently in area"))}">${current("areas", filing.inArea, t("In any area"), t("In no area"))}</select>
        <select id="fl-in-phase" aria-label="${esc(t("Currently in phase"))}">${current("phases", filing.inPhase, t("In any phase"), t("In no phase"))}</select>
      </div>
      <div class="spread" style="margin-bottom:6px;flex-wrap:wrap;gap:8px">
        <span class="muted" id="fl-count"></span>
        <span style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="ghost" id="fl-all" style="width:auto;padding:8px 14px">${t("Tick all found")}</button>
          <button class="ghost" id="fl-none" style="width:auto;padding:8px 14px">${t("Untick everything")}</button>
        </span>
      </div>
      <label class="pick-row" style="border:0;min-height:0;padding:2px 0 8px">
        <input type="checkbox" id="fl-only" ${filing.onlyTicked ? "checked" : ""}>
        <span class="muted">${t("Show only what is ticked")}</span>
      </label>
      <div id="fl-results" class="pick-list"></div>
      <div class="field-grid" style="margin-top:12px">
        <div><label for="fl-area">${t("Set area to")}</label><select id="fl-area">${target("areas", filing.area)}</select></div>
        <div><label for="fl-phase">${t("Set phase to")}</label><select id="fl-phase">${target("phases", filing.phase)}</select></div>
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
    go.textContent = !S.online ? t("Filing needs a connection")
      : !n ? t("Tick the items to file")
      : !chosen ? t("Choose an area or a phase to set")
      : tn(n, "File {n} item", "File {n} items");
    go.disabled = !S.online || !n || !chosen;
  };
  const paint = () => {
    const found = filingMatches();
    const shown = found.slice(0, FILING_SHOWN);
    const tickedHere = found.filter((item) => filing.ticked.has(item.id)).length;
    document.getElementById("fl-count").textContent =
      t("{found} found · {ticked} ticked", { found: found.length, ticked: filing.ticked.size })
      + (filing.ticked.size !== tickedHere ? ` ${t("({n} of them here)", { n: tickedHere })}` : "");
    // Every ticked item can be given a quantity: how many of it go into the
    // phase chosen below. Shown on the tick, not once the phase is picked --
    // the picker is under the list, and a box that only appears afterwards is
    // a box nobody finds.
    const asking = true;
    $results.innerHTML = shown.map((item) => `
      <div class="pick-row" style="cursor:default">
        <label style="display:flex;align-items:center;gap:12px;flex:1;min-width:0;margin:0;cursor:pointer">
          <input type="checkbox" data-fl-pick="${item.id}" ${filing.ticked.has(item.id) ? "checked" : ""}>
          <span style="min-width:0">
            <strong>${esc(item.code)}</strong> ${itemTags(item)}
            <br><span class="muted">${esc(projectCode.get(item.project_id) || "")} · ${pcs(batchOf(item))}${item.description ? ` · <bdi>${esc(item.description)}</bdi>` : ""}</span>
          </span>
        </label>
        ${asking && filing.ticked.has(item.id) ? `
        <input data-fl-qty="${item.id}" type="number" inputmode="numeric" min="1" max="${batchOf(item)}"
               value="${esc(filing.qty[item.id] ?? "")}" placeholder="${esc(t("all {n}", { n: batchOf(item) }))}"
               aria-label="${esc(t("How many of {code} go into the phase", { code: item.code }))}" style="width:96px;flex:none;padding:8px">` : ""}
      </div>`).join("")
      + (found.length > shown.length ? `<p class="muted" style="padding:8px 0">${t("Showing the first {n} of {total}. Narrow the search to see the rest — \"Tick all found\" still takes all {total}.", { n: shown.length, total: found.length })}</p>` : "")
      || `<p class="muted" style="padding:8px 0">${filing.onlyTicked ? t("Nothing is ticked.") : t("No items match.")}</p>`;
    label();
  };

  // One listener for the whole list: it is repainted on every keystroke.
  $results.onchange = (e) => {
    const id = e.target?.dataset?.flPick;
    if (!id) return;
    if (e.target.checked) filing.ticked.add(id);
    else { filing.ticked.delete(id); delete filing.qty[id]; }
    // Repainting is cheap and keeps the count, the list and the button honest
    // together; the box that was ticked keeps its place because the order is
    // the same.
    const top = $results.scrollTop;
    paint();
    $results.scrollTop = top;
  };
  // Typing a quantity changes nothing on screen, so nothing is repainted
  // and the box keeps its focus.
  $results.oninput = (e) => {
    const id = e.target?.dataset?.flQty;
    if (id) filing.qty[id] = e.target.value;
  };
  document.getElementById("fl-q").oninput = (e) => { filing.q = e.target.value; paint(); };
  for (const [id, key] of [["fl-in-area", "inArea"], ["fl-in-phase", "inPhase"]]) {
    document.getElementById(id).onchange = (e) => { filing[key] = e.target.value; paint(); };
  }
  document.getElementById("fl-area").onchange = (e) => { filing.area = e.target.value; label(); };
  // Choosing a phase is what brings up the quantity boxes.
  document.getElementById("fl-phase").onchange = (e) => { filing.phase = e.target.value; paint(); };
  document.getElementById("fl-only").onchange = (e) => { filing.onlyTicked = e.target.checked; paint(); };
  document.getElementById("fl-all").onclick = () => {
    for (const item of filingMatches()) filing.ticked.add(item.id);
    paint();
  };
  document.getElementById("fl-none").onclick = () => { filing.ticked.clear(); filing.qty = {}; filing.onlyTicked = false; document.getElementById("fl-only").checked = false; paint(); };

  document.getElementById("fl-go").onclick = async () => {
    const err = document.getElementById("fl-err");
    err.hidden = true;
    const ids = [...filing.ticked];
    if (!ids.length || (filing.area === KEEP && filing.phase === KEEP)) return;
    const payload = { item_ids: ids };
    const where = [];
    for (const [key, list, field, none] of [["area", "areas", "area_id", N_("no area")], ["phase", "phases", "phase_id", N_("no phase")]]) {
      if (filing[key] === KEEP) continue;
      payload[field] = filing[key] === NONE ? null : filing[key];
      where.push(filing[key] === NONE ? t(none) : labelRow(list, filing[key])?.name || "?");
    }
    // Quantities, for the items not going whole. They only mean something
    // when a phase is being set.
    const part = [];
    const fail = (message) => { err.textContent = message; err.hidden = false; };
    if (filing.phase === KEEP && ids.some((id) => String(filing.qty[id] ?? "").trim())) {
      return fail(t("A quantity is how many go into a phase. Choose the phase under “Set phase to”, or clear the quantities."));
    }
    if (filing.phase !== KEEP) {
      for (const id of ids) {
        const typed = String(filing.qty[id] ?? "").trim();
        if (!typed) continue;
        const item = S.items.find((i) => i.id === id);
        const qty = Number(typed);
        if (!Number.isInteger(qty) || qty < 1) return fail(t("{code}: the quantity is a whole number, 1 or more — or leave it empty for all of it.", { code: item?.code || t("An item") }));
        if (item && qty > batchOf(item)) return fail(t("{code} is a batch of {n}; it cannot give {qty}.", { code: item.code, n: batchOf(item), qty }));
        (payload.quantities ||= {})[id] = qty;
        if (item && qty < batchOf(item)) part.push(t("{n} of {code}", { n: qty, code: item.code }));
      }
    }
    const whole = ids.length - part.length;
    const what = [
      whole ? tn(whole, "{n} item", "{n} items") : "",
      part.length ? `${part.slice(0, 6).join(t(", "))}${part.length > 6 ? ` ${t("and {n} more", { n: part.length - 6 })}` : ""}` : "",
    ].filter(Boolean).join(t(", and "));
    const whereText = where.join(t(" and "));
    if (!confirm(`${t("File {what} under {where}?", { what, where: whereText })}${part.length ? `\n\n${t("Where a quantity is given, the rest of that batch stays in the phase it is in.")}` : ""}`)) return;
    const go = document.getElementById("fl-go");
    go.disabled = true;
    try {
      const done = await api("/api/items/assign", { method: "POST", body: JSON.stringify(payload) });
      toast(t("{items} filed under {where}", { items: tn(done.updated, "{n} item", "{n} items"), where: whereText })
        + (done.split ? ` — ${t("{n} in part", { n: done.split })}` : ""));
      // Done with this batch: start the next one clean, but keep the search --
      // the next thing is usually to look at what is left.
      filing.ticked.clear();
      filing.qty = {};
      filing.onlyTicked = false;
      filing.area = KEEP;
      filing.phase = KEEP;
      dirtyFields.clear();
      await sync();
      if (body.isConnected) officeAreas(body);
    } catch (error) {
      const detail = error.body?.detail;
      err.textContent = typeof detail === "string" ? detail
        : isNetworkError(error) ? t("No connection — nothing was changed.")
        : t("Could not file the items.");
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
      <h2>${t("Areas & phases")}</h2>
      <p class="muted">${t("Areas and phases belong to a project, and there is no active project yet. Projects arrive through")} <a href="#/office/import">${t("Import")}</a>.</p>
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
      <label for="lb-project" style="margin-top:0">${t("Project")}</label>
      <select id="lb-project">
        ${projects.map((pr) => `<option value="${pr.id}" ${pr.id === projectId ? "selected" : ""}>${esc(pr.code)} — ${esc(pr.name)}</option>`).join("")}
      </select>
      <p class="muted" style="margin-top:6px">${esc(t("Each project has its own areas and phases. Everything on this page is {code}'s.", { code: project.code }))}</p>
    </div>` + filingCard() + LABEL_LISTS.map(({ list, field, title, blurb, example, none, add }) => {
    const rows = (S.ref?.[list] || []).filter((row) => row.project_id === projectId);
    return `
    <div class="card">
      <h2>${esc(t(title, { code: project.code }))}</h2>
      <p class="muted" style="margin-bottom:8px">${t(blurb)} ${t("Every item of this project can be filed under one, from the import file, its editor or the tool above, and the Items list and Reports can be narrowed to it.")}</p>
      ${rows.map((row) => {
        const count = S.items.filter((item) => item[field] === row.id).length;
        return `
        <details data-panel="${list}:${row.id}" style="padding:4px 0;border-bottom:1px solid var(--line)">
          <summary style="cursor:pointer;padding:6px 0">
            <strong>${esc(row.name)}</strong>${row.is_active ? "" : ` <span class="badge">${t("retired")}</span>`}
            <span class="muted"> · ${tn(count, "{n} item on the floor", "{n} items on the floor")}</span>
          </summary>
          <div style="padding:8px 0 12px">
            <div class="field-grid">
              <div><label>${t("Name")}</label><input data-lb-name="${list}:${row.id}" maxlength="120" value="${esc(row.name)}" dir="auto"></div>
              <div><label>${t("Sort order")}</label><input data-lb-sort="${list}:${row.id}" type="number" inputmode="numeric" value="${row.sort_order}"></div>
            </div>
            <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap">
              <button class="ghost" data-lb-save="${list}:${row.id}" style="width:auto;padding:10px 16px" ${S.online ? "" : "disabled"}>${t("Save")}</button>
              <button class="ghost" data-lb-toggle="${list}:${row.id}" style="width:auto;padding:10px 16px" ${S.online ? "" : "disabled"}>${row.is_active ? t("Retire") : t("Reactivate")}</button>
              ${count ? "" : `<button class="ghost" data-lb-delete="${list}:${row.id}" style="width:auto;padding:10px 16px;border-color:var(--warn);color:var(--warn-text)" ${S.online ? "" : "disabled"}>${t("Delete")}</button>`}
            </div>
            <p class="muted" style="margin-top:6px">${count
              ? t("Retiring takes it off the pickers; the items filed under it stay there.")
              : t("Nothing on the floor is filed under it, so it can be deleted outright.")}</p>
          </div>
        </details>`;
      }).join("") || `<p class="muted">${t(none)}</p>`}
      <label style="margin-top:12px">${t(add)}</label>
      <div class="qty-row">
        <input data-lb-new="${list}" maxlength="120" dir="auto" placeholder="${esc(t(example))}" style="flex:1;text-align:start;padding:10px">
        <button class="ghost" data-lb-add="${list}" style="width:auto;padding:10px 16px" ${S.online ? "" : "disabled"}>${t("Add")}</button>
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
      refuse(error, t(meta(list).updateFailed));
    }
  };

  body.querySelectorAll("[data-lb-save]").forEach((button) => {
    button.onclick = () => {
      const key = button.dataset.lbSave;
      const name = body.querySelector(`[data-lb-name="${key}"]`).value.trim();
      if (!name) return;
      patch(key, { name, sort_order: Number(body.querySelector(`[data-lb-sort="${key}"]`).value) || 0 }, t("Saved"));
    };
  });
  body.querySelectorAll("[data-lb-toggle]").forEach((button) => {
    button.onclick = () => {
      const [list, id] = button.dataset.lbToggle.split(":");
      const row = labelRow(list, id);
      const { field } = meta(list);
      const count = S.items.filter((item) => item[field] === id).length;
      if (row.is_active && count && !confirm(tn(count,
        "Retire {name}? {n} item is filed under it and will stay there; it just cannot be picked for new ones.",
        "Retire {name}? {n} items are filed under it and will stay there; they just cannot be picked for new ones.",
        { name: row.name }))) return;
      patch(button.dataset.lbToggle, { is_active: !row.is_active },
        row.is_active ? t("{name} retired", { name: row.name }) : t("{name} is back on the list", { name: row.name }));
    };
  });
  body.querySelectorAll("[data-lb-delete]").forEach((button) => {
    button.onclick = async () => {
      const [list, id] = button.dataset.lbDelete.split(":");
      const row = labelRow(list, id);
      if (!confirm(t(meta(list).deleteQuestion, { name: row.name, code: project.code }))) return;
      try {
        await api(`/api/${list}/${id}`, { method: "DELETE" });
        toast(t("{name} deleted", { name: row.name }));
        await sync();
        if (body.isConnected) officeAreas(body);
      } catch (error) {
        // The server counts archived items too, which this phone does not hold.
        refuse(error, t(meta(list).deleteFailed));
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
        toast(t("{name} added", { name }));
        await sync();
      } catch (error) {
        err.textContent = typeof error.body?.detail === "string" ? error.body.detail : t(meta(list).addFailed);
        err.hidden = false;
      }
    };
  });
}

/* ---------------------------------------------------- office: work orders -- */

/* A work order is a numbered document releasing a set of one project's items
   to production, printed as a PDF: one table, a row per item. It is paper, not
   state -- issuing one changes nothing about its items.

   The draft being written lives here, not in the DOM: it is a long form, and a
   background sync re-renders the Office view every thirty seconds. Every
   keystroke lands in this object, and the screen is always drawn from it. */
const workOrders = {
  project: "",          // whose work orders the page is showing
  list: null,           // the saved ones for that project, or null while loading
  listFor: null,
  draft: null,          // the one being written, or null for the list
  q: "", inArea: "", inPhase: "",   // the "add items" search
  issued: null,         // the one just saved, to offer its PDF
};
let workOrderKey = 0;

/* The column switches, in the order the PDF prints them. */
const WO_COLUMNS = [
  ["show_picture", N_("Pictures")],
  ["show_description", N_("Description")],
  ["show_dimensions", N_("Dimensions")],
  ["show_location", N_("Area & phase")],
];

/* What the PDF can be printed in. */
const WO_LANGUAGES = [["en", N_("English")], ["ar", N_("Arabic")], ["both", N_("English and Arabic")]];

/* Every text field on a line, by the data attribute of the input that edits
   it. All of them are the work order's own wording. */
const WO_LINE_FIELDS = {
  woCode: "code", woRev: "drawing_revision", woDesc: "description", woDescAr: "description_ar",
  woDims: "dimensions", woQty: "qty", woArea: "area", woPhase: "phase",
};

/* "WO-0003 Rev 2": the number is the document's own and stays as printed. */
const woLabel = (label, revision, gap = " ") => `${label}${gap}${t("Rev {n}", { n: revision })}`;

/* An item's picture, as an image id: its icon URL ends in one. */
const iconId = (item) => (item?.icon_url ? item.icon_url.split("/").pop() : null);

/* A new line starts as a copy of the item. From here on it is the work
   order's own text: changing it does not touch the item, and changing the item
   later does not touch a work order already issued. */
function workOrderLine(item) {
  return {
    key: `new${++workOrderKey}`, id: null, item_id: item.id,
    code: item.code, area: areaName(item), phase: phaseName(item), drawing_revision: item.drawing_revision,
    description: item.description || "", description_ar: "",
    dimensions: item.dimensions || "", qty: item.total_qty,
    image_id: iconId(item), image_url: item.icon_url || null,
  };
}

function workOrderDraft(saved, projectId) {
  if (!saved) {
    // A factory prints in the same language day after day: start from the
    // one the last work order issued on this device used.
    const usual = localStorage.getItem("wo-language");
    return { id: null, label: null, revision: 0, project_id: projectId, title: "", notes: "",
      language: WO_LANGUAGES.some(([code]) => code === usual) ? usual : "en", title_ar: "", notes_ar: "",
      show_picture: true, show_description: true, show_dimensions: true, show_location: true, lines: [] };
  }
  return {
    language: "en", title_ar: "", notes_ar: "", ...saved,
    lines: saved.lines.map((line) => ({ description_ar: "", ...line, key: line.id })),
  };
}

/* The project's items that are not on the draft yet and match the search. */
function workOrderCandidates() {
  const taken = new Set(workOrders.draft.lines.map((line) => line.item_id));
  const matches = filingMatcher(workOrders.q);
  return S.items.filter((item) =>
    item.project_id === workOrders.draft.project_id
    && !taken.has(item.id)
    && matchesLabel(item, "area_id", workOrders.inArea)
    && matchesLabel(item, "phase_id", workOrders.inPhase)
    && matches(item));
}

function officeWorkOrders(body) {
  const projects = (S.ref?.projects || []).filter((pr) => pr.is_active !== false);
  if (!projects.length) {
    body.innerHTML = `
    <div class="card">
      <h2>${t("Work orders")}</h2>
      <p class="muted">${t("A work order releases a project's items to production, and there is no active project yet. Projects arrive through")} <a href="#/office/import">${t("Import")}</a>.</p>
    </div>`;
    return;
  }
  if (workOrders.draft) { workOrderEditor(body, projects); return; }

  const stored = sessionStorage.getItem("wo-project");
  if (!projects.some((pr) => pr.id === workOrders.project)) {
    workOrders.project = projects.some((pr) => pr.id === stored) ? stored : projects[0].id;
  }
  const project = projects.find((pr) => pr.id === workOrders.project);
  const issued = workOrders.issued;

  body.innerHTML = `
    <div class="card">
      <h2>${t("Work orders")}</h2>
      <p class="muted" style="margin-bottom:8px">${t("A numbered document releasing a set of a project's items to production, as a PDF with one row per item. It is a record of what was released: issuing one changes nothing about the items themselves.")}</p>
      <label for="wo-project">${t("Project")}</label>
      <select id="wo-project">
        ${projects.map((pr) => `<option value="${pr.id}" ${pr.id === workOrders.project ? "selected" : ""}>${esc(pr.code)} — ${esc(pr.name)}</option>`).join("")}
      </select>
      <div style="height:10px"></div>
      <button class="primary" id="wo-new" ${S.online ? "" : "disabled"}>${S.online ? esc(t("New work order for {code}", { code: project.code })) : t("Work orders need a connection")}</button>
      ${issued ? `
      <p style="margin-top:10px"><strong>${esc(woLabel(issued.label, issued.revision))}</strong> ${t("is issued.")}
        <a href="${issued.pdf_url}" download>${t("Download the PDF")}</a></p>` : ""}
    </div>
    <div class="card">
      <h2>${esc(t("Issued for {code}", { code: project.code }))}</h2>
      <div id="wo-list" class="muted">${t("Loading…")}</div>
    </div>`;

  document.getElementById("wo-project").onchange = (e) => {
    workOrders.project = e.target.value;
    workOrders.issued = null;
    sessionStorage.setItem("wo-project", e.target.value);
    officeWorkOrders(body);
  };
  document.getElementById("wo-new").onclick = () => {
    Object.assign(workOrders, { draft: workOrderDraft(null, workOrders.project), q: "", inArea: "", inPhase: "", issued: null });
    dirtyFields.clear();
    officeWorkOrders(body);
  };

  const paint = () => {
    const target = document.getElementById("wo-list");
    if (!target || workOrders.listFor !== workOrders.project || !workOrders.list) return;
    target.classList.remove("muted");
    target.innerHTML = workOrders.list.map((row) => `
      <div style="padding:8px 0;border-bottom:1px solid var(--line)">
        <div class="spread">
          <strong>${esc(woLabel(row.label, row.revision, " · "))}</strong>
          <span class="muted">${fmtWhen(row.issued_at)}</span>
        </div>
        ${row.title ? `<div>${esc(row.title)}</div>` : ""}
        <div class="muted">${tn(row.line_count, "{n} item", "{n} items")} · ${pcs(row.total_qty)} · ${esc(t("issued by {name}", { name: row.issued_by || "?" }))}</div>
        <div style="display:flex;gap:8px;margin-top:6px;flex-wrap:wrap">
          <a class="ghost button-link" href="${row.pdf_url}" download>${t("Download PDF")}</a>
          <button class="ghost" data-wo-open="${row.id}" style="width:auto;padding:8px 16px" ${S.online ? "" : "disabled"}>${t("Open")}</button>
        </div>
      </div>`).join("") || `<span class="muted">${t("No work orders issued for this project yet.")}</span>`;
    target.querySelectorAll("[data-wo-open]").forEach((button) => {
      button.onclick = async () => {
        try {
          const saved = await api(`/api/work-orders/${button.dataset.woOpen}`);
          Object.assign(workOrders, { draft: workOrderDraft(saved), q: "", inArea: "", inPhase: "", issued: null });
          dirtyFields.clear();
          if (body.isConnected) officeWorkOrders(body);
        } catch (error) {
          alert(error.body?.detail || t("Could not open the work order."));
        }
      };
    });
  };
  paint(); // what is already known, at once; then the server's word on it
  const asked = workOrders.project;
  api(`/api/work-orders?project_id=${asked}`).then(({ work_orders }) => {
    if (workOrders.project !== asked) return;
    workOrders.list = work_orders;
    workOrders.listFor = asked;
    paint();
  }).catch(() => {
    const target = document.getElementById("wo-list");
    if (target && !(workOrders.listFor === asked && workOrders.list)) {
      target.textContent = t("Work orders need a connection.");
    }
  });
}

function workOrderEditor(body, projects) {
  const draft = workOrders.draft;
  const project = (S.ref?.projects || []).find((pr) => pr.id === draft.project_id);
  const isAdmin = !!S.user?.is_admin;
  const own = (list) => (S.ref?.[list] || []).filter((row) => row.project_id === draft.project_id);
  const inPicker = (list, picked, any, none) => `
        <option value="">${any}</option>
        <option value="${NONE}" ${picked === NONE ? "selected" : ""}>${none}</option>
        ${own(list).map((row) => `<option value="${row.id}" ${row.id === picked ? "selected" : ""}>${esc(row.name)}</option>`).join("")}`;

  body.innerHTML = `
    <div class="card">
      <div class="spread">
        <h2>${draft.id ? esc(woLabel(draft.label, draft.revision, " · ")) : t("New work order")}</h2>
        <span class="badge">${esc(project?.code || "")}</span>
      </div>
      <p class="muted" style="margin-bottom:8px">${draft.id
        ? t("Saving re-issues it as Rev {n}, with today's date and your name.", { n: draft.revision + 1 })
        : t("It gets its number when it is issued.")} ${t("What is written here is the work order's own: it does not change the items, and later changes to the items do not change it.")}</p>
      <label for="wo-title">${t("Title")}</label>
      <input id="wo-title" maxlength="160" dir="ltr" value="${esc(draft.title)}" placeholder="e.g. Guest rooms — first release">
      <label for="wo-notes">${t("Notes")}</label>
      <textarea id="wo-notes" maxlength="4000" rows="3" dir="ltr" placeholder="Finishes, packing, anything production should read first">${esc(draft.notes)}</textarea>
      <label for="wo-lang">${t("Language of the PDF")}</label>
      <select id="wo-lang">
        ${WO_LANGUAGES.map(([code, name]) => `<option value="${code}" ${code === draft.language ? "selected" : ""}>${t(name)}</option>`).join("")}
      </select>
      <div id="wo-ar" ${draft.language === "en" ? "hidden" : ""}>
        <label for="wo-title-ar">${t("Title in Arabic")}</label>
        <input id="wo-title-ar" dir="rtl" lang="ar" maxlength="160" value="${esc(draft.title_ar)}">
        <label for="wo-notes-ar">${t("Notes in Arabic")}</label>
        <textarea id="wo-notes-ar" dir="rtl" lang="ar" maxlength="4000" rows="3">${esc(draft.notes_ar)}</textarea>
        <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:10px">
          <button class="ghost" id="wo-translate" style="width:auto;padding:10px 16px" ${S.online ? "" : "disabled"}>${t("Translate to Arabic")}</button>
          <span class="muted" id="wo-tr-msg" role="status"></span>
        </div>
        <p class="muted" style="margin-top:6px">${t("Fills in the Arabic that is still empty — title, notes and each line's description — as a draft from Claude. Read it and correct it like any other field: what is in the boxes when you issue is what is printed. The text is sent to Anthropic to be translated.")}</p>
      </div>
      <label>${t("Columns on the PDF")}</label>
      <div class="check-row">
        ${WO_COLUMNS.map(([key, name]) => `
        <label class="pick-row" style="border:0;min-height:0;padding-block:4px;padding-inline:0 14px">
          <input type="checkbox" data-wo-col="${key}" ${draft[key] ? "checked" : ""}><span>${t(name)}</span>
        </label>`).join("")}
      </div>
      <p class="muted">${t("Code, drawing revision and quantity are always printed.")}</p>
    </div>

    <div class="card">
      <h2>${t("Items on it")} <span class="muted" id="wo-count"></span></h2>
      <div id="wo-lines"></div>
    </div>

    <div class="card">
      <h2>${t("Add items")}</h2>
      <input id="wo-q" type="search" value="${esc(workOrders.q)}" style="margin-bottom:8px"
             placeholder="${esc(t("Search — several at once with commas: bed base, LF-03"))}">
      <div class="filters">
        <select id="wo-in-area" aria-label="${esc(t("In area"))}">${inPicker("areas", workOrders.inArea, t("In any area"), t("In no area"))}</select>
        <select id="wo-in-phase" aria-label="${esc(t("In phase"))}">${inPicker("phases", workOrders.inPhase, t("In any phase"), t("In no phase"))}</select>
      </div>
      <div class="spread" style="margin-bottom:6px;flex-wrap:wrap;gap:8px">
        <span class="muted" id="wo-found"></span>
        <button class="ghost" id="wo-add-all" style="width:auto;padding:8px 14px">${t("Add all found")}</button>
      </div>
      <div id="wo-results" class="pick-list"></div>
    </div>

    <div class="card">
      <div class="action-sticky">
        <button class="primary" id="wo-save"></button>
      </div>
      <p class="warn-text" id="wo-err" role="alert" hidden></p>
      <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
        <button class="ghost" id="wo-cancel" style="width:auto;padding:10px 16px">${draft.id ? t("Close without saving") : t("Discard")}</button>
        ${draft.id ? `<a class="ghost button-link" href="/api/work-orders/${draft.id}/pdf" download>${t("Download Rev {n} PDF", { n: draft.revision })}</a>` : ""}
        ${draft.id && isAdmin ? `<button class="ghost" id="wo-delete" style="width:auto;padding:10px 16px;border-color:var(--warn);color:var(--warn-text)" ${S.online ? "" : "disabled"}>${t("Delete work order")}</button>` : ""}
      </div>
    </div>`;

  const $lines = document.getElementById("wo-lines");
  const $results = document.getElementById("wo-results");
  const projectCode = project?.code || "";

  const saveLabel = () => {
    const n = draft.lines.length;
    const pieces = draft.lines.reduce((sum, line) => sum + (Number(line.qty) || 0), 0);
    document.getElementById("wo-count").textContent = n ? `(${n} · ${pcs(pieces)})` : "";
    const go = document.getElementById("wo-save");
    go.textContent = !S.online ? t("Issuing needs a connection")
      : !n ? t("Add the items to release")
      : draft.id ? t("Re-issue as Rev {n}", { n: draft.revision + 1 }) : t("Issue work order ({items})", { items: tn(n, "{n} item", "{n} items") });
    go.disabled = !S.online || !n;
  };

  const paintLines = () => {
    $lines.innerHTML = draft.lines.map((line, index) => `
      <div class="wo-line" data-wo-line="${line.key}">
        <div class="wo-pic">
          ${line.image_url ? `<img src="${thumbOf(line.image_url)}" alt="">` : `<span class="muted">${t("No picture")}</span>`}
          ${line.item_id ? `
          <label class="ghost button-link wo-pic-btn">${t("Change")}
            <input type="file" accept="image/*" data-wo-pic="${line.key}" hidden ${S.online ? "" : "disabled"}>
          </label>` : ""}
          ${line.image_id ? `<button class="ghost wo-pic-btn" data-wo-nopic="${line.key}">${t("Remove")}</button>` : ""}
          <span class="muted" data-wo-picmsg="${line.key}" hidden></span>
        </div>
        <div class="wo-fields">
          <div class="spread">
            <span class="muted">${t("Line {n}", { n: index + 1 })}${line.item_id ? "" : ` <span class="badge">${t("item deleted")}</span>`}</span>
            <span style="white-space:nowrap">
              <button class="ghost wo-move" data-wo-up="${line.key}" aria-label="${esc(t("Move up"))}" ${index ? "" : "disabled"}>▲</button>
              <button class="ghost wo-move" data-wo-down="${line.key}" aria-label="${esc(t("Move down"))}" ${index < draft.lines.length - 1 ? "" : "disabled"}>▼</button>
              <button class="ghost wo-move" data-wo-drop="${line.key}" aria-label="${esc(t("Take off the work order"))}">✕</button>
            </span>
          </div>
          <div class="field-grid">
            <div><label>${t("Code")}</label><input data-wo-code="${line.key}" maxlength="64" dir="ltr" value="${esc(line.code)}"></div>
            <div><label>${t("Drawing revision")}</label><input data-wo-rev="${line.key}" maxlength="32" dir="ltr" value="${esc(line.drawing_revision)}"></div>
          </div>
          <label>${t("Description")}</label>
          <textarea data-wo-desc="${line.key}" maxlength="2000" rows="2" dir="ltr">${esc(line.description)}</textarea>
          ${draft.language === "en" ? "" : `
          <div class="spread" style="align-items:flex-end">
            <label>${t("Description in Arabic")}</label>
            <button class="ghost wo-tr" data-wo-tr="${line.key}" ${S.online ? "" : "disabled"}>${t("Translate")}</button>
          </div>
          <textarea data-wo-desc-ar="${line.key}" dir="rtl" lang="ar" maxlength="2000" rows="2">${esc(line.description_ar)}</textarea>
          <p class="warn-text" data-wo-stale="${line.key}" style="margin:4px 0 0" ${line.ar_stale ? "" : "hidden"}>${t("The English was changed after this was translated. Check the Arabic, or press Translate.")}</p>`}
          <div class="field-grid">
            <div><label>${t("Dimensions")}</label><input data-wo-dims="${line.key}" maxlength="255" dir="auto" value="${esc(line.dimensions)}"></div>
            <div><label>${t("Quantity")}</label><input data-wo-qty="${line.key}" type="number" inputmode="numeric" min="1" value="${esc(line.qty)}"></div>
          </div>
          <div class="field-grid">
            <div><label>${t("Area")}</label><input data-wo-area="${line.key}" maxlength="120" dir="auto" value="${esc(line.area)}"></div>
            <div><label>${t("Phase")}</label><input data-wo-phase="${line.key}" maxlength="120" dir="auto" value="${esc(line.phase)}"></div>
          </div>
        </div>
      </div>`).join("") || `<p class="muted">${t("Nothing on it yet. Find the items below and add them.")}</p>`;
    saveLabel();
  };

  const paintResults = () => {
    const found = workOrderCandidates();
    const shown = found.slice(0, 100);
    document.getElementById("wo-found").textContent = t("{n} found", { n: found.length });
    document.getElementById("wo-add-all").disabled = !found.length;
    $results.innerHTML = shown.map((item) => `
      <div class="pick-row" style="cursor:default">
        ${item.icon_url ? `<img class="item-icon" src="${thumbOf(item.icon_url)}" alt="" loading="lazy">` : ""}
        <span style="min-width:0;flex:1">
          <strong>${esc(item.code)}</strong> ${itemTags(item)}
          <br><span class="muted">${pcs(item.total_qty)}${item.description ? ` · <bdi>${esc(item.description)}</bdi>` : ""}</span>
        </span>
        <button class="ghost" data-wo-add="${item.id}" style="width:auto;padding:8px 14px;flex:none">${t("Add")}</button>
      </div>`).join("")
      + (found.length > shown.length ? `<p class="muted" style="padding:8px 0">${t("Showing the first {n} of {total}. \"Add all found\" takes all {total}.", { n: shown.length, total: found.length })}</p>` : "")
      || `<p class="muted" style="padding:8px 0">${S.items.some((i) => i.project_id === draft.project_id) ? t("Nothing more to add matches.") : esc(t("{code} has no items yet.", { code: projectCode }))}</p>`;
  };

  const line = (key) => draft.lines.find((entry) => entry.key === key);

  // One listener each for the whole list of lines: it is repainted whenever
  // its shape changes, and typing must never cause that.
  const stale = (target, is) => {
    target.ar_stale = is;
    const hint = $lines.querySelector(`[data-wo-stale="${target.key}"]`);
    if (hint) hint.hidden = !is;
  };
  $lines.oninput = (e) => {
    const data = e.target.dataset || {};
    const attribute = Object.keys(WO_LINE_FIELDS).find((name) => data[name]);
    if (!attribute) return;
    const target = line(data[attribute]);
    const field = WO_LINE_FIELDS[attribute];
    target[field] = e.target.value;
    if (field === "qty") saveLabel();
    // An Arabic description stops matching the moment the English is edited.
    // Say so on the line; nothing is retranslated behind anyone's back.
    else if (field === "description") stale(target, !!target.description_ar.trim());
    else if (field === "description_ar") stale(target, false);
  };

  /* Ask for Arabic and hand each answer to `put`. The answers are a draft:
     they go into the fields, where they are read and corrected. */
  const $trMsg = document.getElementById("wo-tr-msg");
  const sayTr = (text, warn) => { $trMsg.className = warn ? "warn-text" : "muted"; $trMsg.textContent = text; };
  let translating = false;
  const translate = async (jobs) => {
    if (translating) return;
    translating = true;
    sayTr(tn(jobs.length, "Translating {n} text…", "Translating {n} texts…"));
    try {
      const { translations } = await api("/api/work-orders/translate", {
        method: "POST", body: JSON.stringify({ texts: jobs.map((job) => job.text) }),
      });
      jobs.forEach((job, index) => job.put(translations[index] || ""));
      if (workOrders.draft !== draft || !body.isConnected) return; // closed while waiting
      document.getElementById("wo-title-ar").value = draft.title_ar;
      document.getElementById("wo-notes-ar").value = draft.notes_ar;
      paintLines();
      sayTr(t("Translated {n}. Read it through before issuing.", { n: jobs.length }));
    } catch (error) {
      const detail = error.body?.detail;
      sayTr(typeof detail === "string" ? detail
        : isNetworkError(error) ? t("No connection — nothing was translated.")
        : t("Could not translate."), true);
    } finally {
      translating = false;
    }
  };
  document.getElementById("wo-translate").onclick = () => {
    // Only what is still empty: a translation someone has corrected is theirs.
    const jobs = [];
    if (draft.title.trim() && !draft.title_ar.trim()) jobs.push({ text: draft.title, put: (ar) => { if (!draft.title_ar.trim()) draft.title_ar = ar; } });
    if (draft.notes.trim() && !draft.notes_ar.trim()) jobs.push({ text: draft.notes, put: (ar) => { if (!draft.notes_ar.trim()) draft.notes_ar = ar; } });
    for (const entry of draft.lines) {
      if (entry.description.trim() && !entry.description_ar.trim()) {
        jobs.push({ text: entry.description, put: (ar) => { if (!entry.description_ar.trim()) { entry.description_ar = ar; entry.ar_stale = false; } } });
      }
    }
    if (!jobs.length) {
      sayTr(t("Nothing to translate: every Arabic box that has English beside it is already filled. Clear a box to have it translated again."));
      return;
    }
    translate(jobs);
  };

  $lines.onclick = (e) => {
    const data = e.target.dataset || {};
    if (data.woTr) {
      // One line, on request: this replaces what is there.
      const entry = line(data.woTr);
      if (!entry.description.trim()) { sayTr(t("That line has no English description to translate."), true); return; }
      translate([{ text: entry.description, put: (ar) => { entry.description_ar = ar; entry.ar_stale = false; } }]);
      return;
    }
    const key = data.woUp || data.woDown || data.woDrop || data.woNopic;
    if (!key) return;
    const at = draft.lines.findIndex((entry) => entry.key === key);
    if (data.woNopic) Object.assign(draft.lines[at], { image_id: null, image_url: null });
    else if (data.woDrop) draft.lines.splice(at, 1);
    else {
      const to = at + (data.woUp ? -1 : 1);
      if (to < 0 || to >= draft.lines.length) return;
      [draft.lines[at], draft.lines[to]] = [draft.lines[to], draft.lines[at]];
    }
    paintLines();
    if (data.woDrop) paintResults(); // it is available to add again
  };
  // A picture chosen here is for the document: stored with the item, but not
  // as its icon, so the item list keeps the picture it had.
  $lines.onchange = async (e) => {
    const key = e.target.dataset?.woPic;
    const file = e.target.files?.[0];
    if (!key || !file) return;
    const target = line(key);
    const message = $lines.querySelector(`[data-wo-picmsg="${key}"]`);
    const say = (text, warn) => { if (message) { message.hidden = false; message.className = warn ? "warn-text" : "muted"; message.textContent = text; } };
    say(t("Uploading…"));
    try {
      const image = await uploadImage(target.item_id, file, {
        kind: "document",
        onProgress: (fraction) => say(t("Uploading… {n}%", { n: Math.round(fraction * 100) })),
      });
      Object.assign(target, { image_id: image.id, image_url: image.url });
      paintLines();
    } catch (error) {
      say(error.detail || t("Upload failed."), true);
    }
  };

  $results.onclick = (e) => {
    const id = e.target.dataset?.woAdd;
    const item = id && S.items.find((entry) => entry.id === id);
    if (!item) return;
    draft.lines.push(workOrderLine(item));
    paintLines();
    paintResults();
  };
  document.getElementById("wo-add-all").onclick = () => {
    for (const item of workOrderCandidates()) draft.lines.push(workOrderLine(item));
    paintLines();
    paintResults();
  };
  document.getElementById("wo-q").oninput = (e) => { workOrders.q = e.target.value; paintResults(); };
  document.getElementById("wo-in-area").onchange = (e) => { workOrders.inArea = e.target.value; paintResults(); };
  document.getElementById("wo-in-phase").onchange = (e) => { workOrders.inPhase = e.target.value; paintResults(); };
  document.getElementById("wo-title").oninput = (e) => { draft.title = e.target.value; };
  document.getElementById("wo-notes").oninput = (e) => { draft.notes = e.target.value; };
  document.getElementById("wo-title-ar").oninput = (e) => { draft.title_ar = e.target.value; };
  document.getElementById("wo-notes-ar").oninput = (e) => { draft.notes_ar = e.target.value; };
  document.getElementById("wo-lang").onchange = (e) => {
    draft.language = e.target.value;
    // The Arabic boxes are only on screen when the paper will carry Arabic.
    // What is in them is kept either way.
    document.getElementById("wo-ar").hidden = draft.language === "en";
    paintLines();
  };
  body.querySelectorAll("[data-wo-col]").forEach((box) => {
    box.onchange = () => { draft[box.dataset.woCol] = box.checked; };
  });

  const leave = () => {
    workOrders.draft = null;
    workOrders.list = null; // ask again: it has just changed
    dirtyFields.clear();
    if (body.isConnected) officeWorkOrders(body);
  };
  document.getElementById("wo-cancel").onclick = () => {
    const touched = draft.lines.length || draft.title || draft.notes;
    if (touched && !confirm(draft.id ? t("Close without saving your changes?") : t("Discard this work order?"))) return;
    leave();
  };
  const remove = document.getElementById("wo-delete");
  if (remove) remove.onclick = async () => {
    if (!confirm(t("Delete {label}? The record goes for good. Its items are not touched.", { label: draft.label }))) return;
    try {
      await api(`/api/work-orders/${draft.id}`, { method: "DELETE" });
      toast(t("{name} deleted", { name: draft.label }));
      leave();
    } catch (error) {
      alert(error.body?.detail || t("Could not delete the work order."));
    }
  };

  document.getElementById("wo-save").onclick = async () => {
    const err = document.getElementById("wo-err");
    const fail = (text) => { err.textContent = text; err.hidden = false; };
    err.hidden = true;
    if (!draft.lines.length) return;
    const bad = draft.lines.find((entry) => !Number.isInteger(Number(entry.qty)) || Number(entry.qty) < 1 || String(entry.qty).trim() === "");
    if (bad) return fail(t("{code}: the quantity must be a whole number, 1 or more.", { code: bad.code || t("A line") }));
    const uncoded = draft.lines.findIndex((entry) => !String(entry.code).trim());
    if (uncoded >= 0) return fail(t("Line {n} has no code.", { n: uncoded + 1 }));
    const payload = {
      project_id: draft.project_id,
      title: draft.title.trim(),
      notes: draft.notes.trim(),
      language: draft.language,
      title_ar: draft.title_ar.trim(),
      notes_ar: draft.notes_ar.trim(),
      ...Object.fromEntries(WO_COLUMNS.map(([key]) => [key, !!draft[key]])),
      lines: draft.lines.map((entry) => ({
        ...(entry.id ? { id: entry.id } : {}),
        item_id: entry.item_id,
        image_id: entry.image_id,
        code: String(entry.code).trim(),
        drawing_revision: String(entry.drawing_revision ?? "").trim(),
        description: entry.description.trim(),
        description_ar: (entry.description_ar || "").trim(),
        dimensions: entry.dimensions.trim(),
        qty: Number(entry.qty),
        area: String(entry.area ?? "").trim(),
        phase: String(entry.phase ?? "").trim(),
      })),
    };
    const go = document.getElementById("wo-save");
    go.disabled = true;
    try {
      const saved = await api(draft.id ? `/api/work-orders/${draft.id}` : "/api/work-orders", {
        method: draft.id ? "PUT" : "POST", body: JSON.stringify(payload),
      });
      toast(t("{label} issued", { label: woLabel(saved.label, saved.revision) }));
      localStorage.setItem("wo-language", saved.language);
      workOrders.issued = saved;
      workOrders.project = saved.project_id;
      leave();
    } catch (error) {
      const detail = error.body?.detail;
      fail(typeof detail === "string" ? detail
        : Array.isArray(detail) ? rejectReason(error)
        : isNetworkError(error) ? t("No connection — the work order was not issued.")
        : t("Could not issue the work order."));
      saveLabel();
    }
  };

  paintLines();
  paintResults();
}

/* --------------------------------------------------------- office: stages -- */

function officeStages(body) {
  body.innerHTML = `
    <div class="card">
      <h2>${t("Stages & stations")}</h2>
      <p class="muted" style="margin-bottom:8px">${t("Add a stage here and every item can be logged at it straight away, on each phone's next sync — there is nothing else to set up. Add stations if the stage has more than one booth or bench. Sort order is only the order stages are listed in: units move between them freely.")}</p>
      <div id="mg-stages" class="muted">${t("Loading…")}</div>
      <details data-panel="new-stage" style="margin-top:12px">
        <summary class="muted" style="cursor:pointer">${t("New stage")}</summary>
        <div class="field-grid" style="margin-top:8px">
          <div><label for="ns-name">${t("Name")}</label><input id="ns-name" dir="auto" placeholder="${esc(t("Glass shop"))}"></div>
          <div><label for="ns-name-ar">${t("Name in Arabic")}</label><input id="ns-name-ar" dir="rtl" lang="ar" maxlength="120" placeholder="${esc(t("optional"))}"></div>
        </div>
        <label for="ns-sort">${t("Sort order")}</label>
        <input id="ns-sort" type="number" inputmode="numeric">
        <label for="ns-days">${t("Flag as aging after (days — empty for never)")}</label>
        <input id="ns-days" type="number" inputmode="numeric" min="1" max="365" value="3">
        <div id="ns-flags"></div>
        <div style="height:8px"></div>
        <button class="primary" id="ns-go" ${S.online ? "" : "disabled"}>${t("Add stage")}</button>
        <p class="warn-text" id="ns-err" hidden></p>
      </details>
    </div>`;
  renderStageAdmin();
}

/* ---------------------------------------------------------- office: users -- */

function officeUsers(body) {
  body.innerHTML = `
    <div class="card">
      <h2>${t("Users")}</h2>
      <div id="mg-users" class="muted">${t("Loading…")}</div>
      <details data-panel="new-user" style="margin-top:12px">
        <summary class="muted" style="cursor:pointer">${t("New user")}</summary>
        <div class="field-grid" style="margin-top:8px">
          <div><label for="nu-username">${t("Username")}</label><input id="nu-username" autocapitalize="none" autocomplete="off" dir="ltr"></div>
          <div><label for="nu-display">${t("Display name")}</label><input id="nu-display" autocomplete="off" dir="auto"></div>
        </div>
        <label for="nu-pass">${t("Password (min 8 characters)")}</label>
        <input id="nu-pass" type="password" autocomplete="new-password">
        <label style="display:flex;align-items:center;gap:10px;margin-top:10px;font-size:14px;color:var(--text)">
          <input type="checkbox" id="nu-admin" style="width:22px;height:22px;flex:none">
          ${t("Admin — can manage stages, users, and removals")}
        </label>
        <div style="height:8px"></div>
        <button class="primary" id="nu-go" ${S.online ? "" : "disabled"}>${t("Create user")}</button>
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
      <h2>${t("Floor banner")}</h2>
      <p class="muted" style="margin-bottom:10px">${t("Shown at the top of the app to everyone who is not an admin, offline included — the phones keep showing the last banner they received. Neutral with no message shows nothing at all.")}</p>
      <div id="st-live" class="muted"></div>
      <label>${t("Colour")}</label>
      <div class="seg" id="st-colors">
        ${BANNER_COLORS.map((c) => `
          <button data-id="${c}" class="${c === picked ? "on" : ""}" aria-pressed="${c === picked}">
            ${esc(bannerColorName(c))}
          </button>`).join("")}
      </div>
      <label for="st-msg">${t("Message (optional, 200 characters)")}</label>
      <input id="st-msg" maxlength="200" dir="auto" placeholder="${esc(t("e.g. Paint booth 2 down until Thursday"))}"
             value="${esc(bannerMsg ?? live.message ?? "")}">
      <div style="height:10px"></div>
      <button class="primary" id="st-go" ${S.online ? "" : "disabled"}>
        ${S.online ? t("Publish banner") : t("Publishing needs a connection")}</button>
      <p class="warn-text" id="st-err" hidden></p>
    </div>

    <div class="card">
      <h2>${t("Accounts")}</h2>
      <p class="muted" style="margin-bottom:10px">${t("Entries are counted against the person they were logged for, and timed by when the work happened on the floor.")}</p>
      <div id="st-users" class="muted">${t("Loading…")}</div>
    </div>

    <div class="card">
      <h2>${t("Database backups")}</h2>
      <div id="st-backup" class="muted">${t("Loading…")}</div>
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
      toast(t("Banner published to the floor"));
      sync(); // the banner reaches other phones through the reference payload
    } catch (error) {
      err.textContent = error.body?.detail || t("Could not publish the banner.");
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
    ? `<span class="badge">${t("live")}</span> <strong>${esc(bannerColorName(live.color))}</strong>${live.message ? ` — <span dir="auto">${esc(live.message)}</span>` : ""}
       <br>${esc(t("set by {name}", { name: live.set_by || t("someone") }))} ${live.set_at ? timeAgo(Date.parse(live.set_at)) : ""}`
    : `<span class="badge">${t("live")}</span> ${t("Nothing showing on the floor.")}`;
}

/* "never" beats a blank cell: an account that has not signed in since this
   shipped is a fact worth reading as one, not an empty space that looks like a
   rendering bug. Relative time on top because that is the question being asked;
   the wall clock underneath because "3d ago" is not something to put in an
   email about it. */
function whenCell(iso) {
  if (!iso) return `<td class="muted">${t("never")}</td>`;
  const when = new Date(iso);
  return `<td>${timeAgo(when.getTime())}<br><span class="muted">${fmtWhen(when)}</span></td>`;
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
    if (target.isConnected) target.textContent = t("Backup status needs a connection.");
    return;
  }
  if (!target.isConnected) return;

  if (!status.available) {
    target.className = "warn-text";
    target.textContent = t("Could not read the backup storage — check the server log.");
  } else if (!status.count) {
    target.className = "warn-text";
    target.textContent = t("No backups in storage yet. The first is written about a minute after the stack starts.");
  } else if (!status.last_at) {
    target.className = "muted";
    target.textContent = t("{n} kept. Newest: {key}", { n: status.count, key: status.last_key });
  } else {
    const when = new Date(status.last_at);
    const stale = Date.now() - when.getTime() > BACKUP_STALE_MS;
    target.className = stale ? "warn-text" : "";
    target.textContent = t("Last backup {ago} ({when}) · {n} kept", { ago: timeAgo(when.getTime()), when: fmtWhen(when), n: status.count })
      + (stale ? ` — ${t("the nightly backup has not run; check the backup container's log.")}` : "");
  }
}

async function loadUserStatus() {
  const target = document.getElementById("st-users");
  if (!target) return;

  let users;
  try {
    ({ users } = await api("/api/status/users"));
  } catch {
    target.textContent = t("Account status needs a connection — it is counted from the log on the server.");
    return;
  }
  if (!target.isConnected) return;

  target.classList.remove("muted");
  target.innerHTML = `
    <div class="table-scroll">
    <table>
      <thead><tr>
        <th scope="col">${t("Person")}</th>
        <th scope="col">${t("Last sign-in")}</th>
        <th scope="col">${t("Last entry")}</th>
        <th scope="col" class="num">${t("Entries")}</th>
      </tr></thead>
      <tbody>
        ${users.map((u) => `
          <tr>
            <td>
              <strong>${esc(u.display_name)}</strong>
              ${u.is_admin ? ` <span class="badge">${t("admin")}</span>` : ""}${u.is_active ? "" : ` <span class="badge">${t("off")}</span>`}
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
/* [flag, what the checkbox says, what the stage's summary line says] */
const STAGE_FLAGS = [
  ["requires_station", N_("Has physical stations"), N_("Has physical stations")],
  ["requires_external_po", N_("External supplier (lead time, PO)"), N_("External supplier")],
  ["allows_partial_qty", N_("Allows partial quantities (off: a move in takes the whole lot)"), N_("Allows partial quantities")],
];

function flagCheckboxes(prefix, values) {
  return STAGE_FLAGS.map(([key, label]) => `
    <label style="display:flex;align-items:center;gap:10px;font-size:14px;color:var(--text);margin:6px 0">
      <input type="checkbox" data-flag="${prefix}:${key}" ${values[key] ? "checked" : ""}
             style="width:22px;height:22px;flex:none">
      ${t(label)}
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
      ...STAGE_FLAGS.filter(([k]) => s[k]).map(([, , short]) => t(short)),
      s.max_days_in_state != null ? t("ages at {n}d", { n: s.max_days_in_state }) : "",
    ].filter(Boolean).join(" · ");
    return `
      <details data-panel="stage:${s.id}" style="padding:4px 0;border-bottom:1px solid var(--line)">
        <summary style="cursor:pointer;padding:6px 0">
          <strong>${esc(localName(s))}</strong>${s.is_active ? "" : ` <span class="badge">${t("inactive")}</span>`}
          <br><span class="muted">${esc(flagSummary || t("no flags"))}${stations.length ? ` · ${t("stations:")} ${stations.map((st) => `<bdi>${esc(localName(st))}</bdi>` + (st.is_active ? "" : ` ${t("(off)")}`)).join(t(", "))}` : ""}</span>
        </summary>
        <div style="padding:8px 0 12px">
          <div class="field-grid">
            <div><label>${t("Name")}</label><input data-stage-name="${s.id}" value="${esc(s.name)}" dir="auto"></div>
            <div><label>${t("Name in Arabic")}</label><input data-stage-name-ar="${s.id}" value="${esc(s.name_ar || "")}" dir="rtl" lang="ar" maxlength="120" placeholder="${esc(t("optional"))}"></div>
          </div>
          <label>${t("Sort order")}</label>
          <input data-stage-sort="${s.id}" type="number" inputmode="numeric" value="${s.sort_order}">
          <label>${t("Flag as aging after (days — empty for never, e.g. outsourced)")}</label>
          <input data-stage-days="${s.id}" type="number" inputmode="numeric" min="1" max="365"
                 placeholder="${esc(t("no threshold"))}" value="${s.max_days_in_state ?? ""}">
          ${flagCheckboxes(s.id, s)}
          <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap">
            <button class="ghost" data-stage-save="${s.id}" style="width:auto;padding:10px 16px" ${S.online ? "" : "disabled"}>${t("Save")}</button>
            <button class="ghost" data-stage-toggle="${s.id}" style="width:auto;padding:10px 16px" ${S.online ? "" : "disabled"}>${s.is_active ? t("Deactivate") : t("Reactivate")}</button>
          </div>
          ${stations.map((st) => `
          <div style="border:1px solid var(--line);border-radius:var(--radius);padding:8px 10px;margin-top:10px">
            <div class="field-grid">
              <div><label>${t("Station")}${st.is_active ? "" : ` <span class="badge">${t("inactive")}</span>`}</label><input data-st-name="${st.id}" value="${esc(st.name)}" dir="auto"></div>
              <div><label>${t("Name in Arabic")}</label><input data-st-name-ar="${st.id}" value="${esc(st.name_ar || "")}" dir="rtl" lang="ar" maxlength="120" placeholder="${esc(t("optional"))}"></div>
            </div>
            <label>${t("Sort order")}</label>
            <input data-st-sort="${st.id}" type="number" inputmode="numeric" value="${st.sort_order}">
            <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap">
              <button class="ghost" data-st-save="${st.id}" style="width:auto;padding:10px 16px" ${S.online ? "" : "disabled"}>${t("Save")}</button>
              <button class="ghost" data-st-toggle="${st.id}" style="width:auto;padding:10px 16px" ${S.online ? "" : "disabled"}>${st.is_active ? t("Deactivate") : t("Reactivate")}</button>
            </div>
          </div>`).join("")}
          <label style="margin-top:10px">${t("Add station")}</label>
          <div class="qty-row">
            <input data-station-name="${s.id}" dir="auto" placeholder="${esc(t("e.g. {name}", { name: `${localName(s)} ${stations.length + 1}` }))}" style="flex:1;text-align:start;padding:10px">
            <button class="ghost" data-station-add="${s.id}" style="width:auto;padding:10px 16px" ${S.online ? "" : "disabled"}>${t("Add")}</button>
          </div>
        </div>
      </details>`;
  }).join("") || `<span class="muted">${t("No stages yet.")}</span>`;

  const patchStage = async (stageId, body, doneMessage) => {
    try {
      await api(`/api/stages/${stageId}`, { method: "PATCH", body: JSON.stringify(body) });
      toast(doneMessage);
      await sync(); // refreshes S.ref, re-renders the office view
    } catch (error) {
      alert(error.body?.detail || t("Could not update the stage."));
    }
  };

  target.querySelectorAll("[data-stage-save]").forEach((button) => {
    button.onclick = () => {
      const id = button.dataset.stageSave;
      const days = document.querySelector(`[data-stage-days="${id}"]`).value.trim();
      patchStage(id, {
        name: document.querySelector(`[data-stage-name="${id}"]`).value.trim(),
        // Empty clears it: the stage is then shown by its name in Arabic too.
        name_ar: document.querySelector(`[data-stage-name-ar="${id}"]`).value.trim(),
        sort_order: Number(document.querySelector(`[data-stage-sort="${id}"]`).value) || 0,
        max_days_in_state: days ? Number(days) : null,
        ...readFlags(id),
      }, t("Stage updated"));
    };
  });
  target.querySelectorAll("[data-stage-toggle]").forEach((button) => {
    button.onclick = () => {
      const stage = S.ref.stages.find((s) => s.id === button.dataset.stageToggle);
      patchStage(stage.id, { is_active: !stage.is_active },
        stage.is_active ? t("Stage deactivated") : t("Stage reactivated"));
    };
  });
  const patchStation = async (stationId, body, doneMessage) => {
    try {
      await api(`/api/stations/${stationId}`, { method: "PATCH", body: JSON.stringify(body) });
      toast(doneMessage);
      await sync();
    } catch (error) {
      alert(error.body?.detail || t("Could not update the station."));
    }
  };

  target.querySelectorAll("[data-st-save]").forEach((button) => {
    button.onclick = () => {
      const id = button.dataset.stSave;
      const name = document.querySelector(`[data-st-name="${id}"]`).value.trim();
      if (!name) return;
      patchStation(id, {
        name,
        name_ar: document.querySelector(`[data-st-name-ar="${id}"]`).value.trim(),
        sort_order: Number(document.querySelector(`[data-st-sort="${id}"]`).value) || 0,
      }, t("Station updated"));
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
      if (lastActive && !confirm(t("{station} is the last active station at {stage}. Entries logged there will carry no station until one is added back. Deactivate anyway?", { station: localName(station), stage: localName(stage) }))) return;
      patchStation(station.id, { is_active: !station.is_active },
        station.is_active ? t("Station deactivated") : t("Station reactivated"));
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
        toast(t("Station added"));
        await sync();
      } catch (error) {
        alert(error.body?.detail || t("Could not add the station."));
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
    if (!name) { err.textContent = t("The stage needs a name."); err.hidden = false; return; }
    try {
      const days = document.getElementById("ns-days").value.trim();
      await api("/api/stages", {
        method: "POST",
        body: JSON.stringify({
          code: slugify(name),
          name,
          name_ar: document.getElementById("ns-name-ar").value.trim(),
          sort_order: Number(document.getElementById("ns-sort").value) || 0,
          max_days_in_state: days ? Number(days) : null,
          ...readFlags("new"),
        }),
      });
      toast(t("Stage {name} added — items can be logged there now", { name }));
      await sync();
    } catch (error) {
      err.textContent = error.body?.detail || t("Could not add the stage.");
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
    target.textContent = t("User management needs a connection.");
    return;
  }
  if (!target.isConnected) return;

  target.classList.remove("muted");
  target.innerHTML = users.map((u) => `
    <details data-panel="user:${u.id}" style="padding:4px 0;border-bottom:1px solid var(--line)">
      <summary style="cursor:pointer;padding:6px 0">
        <strong>${esc(u.display_name)}</strong> <span class="muted">${esc(u.username)}</span>
        ${u.is_admin ? `<span class="badge">${t("admin")}</span>` : ""}${u.is_active ? "" : ` <span class="badge">${t("deactivated")}</span>`}
        ${u.id === S.user?.id ? ` <span class="badge qty">${t("you")}</span>` : ""}
      </summary>
      <div style="padding:8px 0 12px">
        <label>${t("Display name")}</label>
        <input data-user-display="${u.id}" value="${esc(u.display_name)}" dir="auto">
        <label>${t("Reset password (signs them out everywhere)")}</label>
        <div class="qty-row">
          <input data-user-pass="${u.id}" type="password" autocomplete="new-password"
                 placeholder="${esc(t("min 8 characters"))}" style="flex:1;text-align:start;padding:10px">
          <button class="ghost" data-user-reset="${u.id}" style="width:auto;padding:10px 16px">${t("Reset")}</button>
        </div>
        <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
          <button class="ghost" data-user-save="${u.id}" style="width:auto;padding:10px 16px">${t("Save name")}</button>
          ${u.id === S.user?.id ? "" : `
          <button class="ghost" data-user-admin="${u.id}" style="width:auto;padding:10px 16px">${u.is_admin ? t("Remove admin") : t("Make admin")}</button>
          <button class="ghost" data-user-active="${u.id}" style="width:auto;padding:10px 16px">${u.is_active ? t("Deactivate") : t("Reactivate")}</button>`}
        </div>
      </div>
    </details>`).join("");

  const patchUser = async (id, body, doneMessage) => {
    try {
      await api(`/api/users/${id}`, { method: "PATCH", body: JSON.stringify(body) });
      toast(doneMessage);
      loadUsers();
    } catch (error) {
      alert(error.body?.detail || t("Could not update the user."));
    }
  };

  // This list lands after render() has already restored the view, so it has to
  // put the user's open panels and half-typed names back itself.
  restoreView(lastSnapshot);

  target.querySelectorAll("[data-user-save]").forEach((b) => {
    b.onclick = () => patchUser(b.dataset.userSave, {
      display_name: target.querySelector(`[data-user-display="${b.dataset.userSave}"]`).value.trim(),
    }, t("Name updated"));
  });
  target.querySelectorAll("[data-user-reset]").forEach((b) => {
    b.onclick = () => {
      const field = target.querySelector(`[data-user-pass="${b.dataset.userReset}"]`);
      if (field.value.length < 8) { alert(t("Password needs at least 8 characters.")); return; }
      patchUser(b.dataset.userReset, { password: field.value }, t("Password reset — their old sessions are signed out"));
    };
  });
  target.querySelectorAll("[data-user-admin]").forEach((b) => {
    const user = users.find((u) => u.id === b.dataset.userAdmin);
    b.onclick = () => patchUser(user.id, { is_admin: !user.is_admin },
      user.is_admin ? t("Admin removed") : t("Now an admin"));
  });
  target.querySelectorAll("[data-user-active]").forEach((b) => {
    const user = users.find((u) => u.id === b.dataset.userActive);
    b.onclick = () => {
      if (user.is_active && !confirm(t("Deactivate {name}? They are signed out on their next request; their logged events stay.", { name: user.display_name }))) return;
      patchUser(user.id, { is_active: !user.is_active },
        user.is_active ? t("User deactivated") : t("User reactivated"));
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
      toast(t("User {name} created", { name: created.username }));
      loadUsers();
    } catch (error) {
      const detail = error.body?.detail;
      err.textContent = typeof detail === "string" ? detail
        : Array.isArray(detail) && detail.length ? `${(detail[0].loc || []).slice(1).join(".")}: ${detail[0].msg}`
        : t("Could not create the user.");
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
        <h2>${t("Storage unavailable")}</h2>
        <p class="muted">${t("This browser blocked the on-device database the app needs for offline logging (private browsing does this). Entries cannot be queued safely — close this tab and open the app in a normal browser window.")}</p>
      </div>`;
    return;
  }
  renderPill();
  render();
  sync();
}

boot();
