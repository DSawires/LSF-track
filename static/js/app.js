/* LSF Track — the whole client.

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

async function drainQueue() {
  // Loop, because entries can be enqueued *while* a drain is in flight (the
  // auto-queue second event, or a fast pair of taps) and the syncRunning
  // guard swallows the sync() call they trigger. Bounded so a server that
  // rejects nothing but stores nothing can't spin us.
  for (let round = 0; round < 10; round++) {
    const queue = (await LSF_DB.queueAll()).filter((q) => q._status === "pending");
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
      S.ref = await api("/api/reference");
      await LSF_DB.put("reference", S.ref);
      const itemsPayload = await api("/api/items");
      S.items = itemsPayload.items;
      await LSF_DB.put("items", S.items);
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
function stateOrder() { return [...(S.ref?.states || [])].sort((a, b) => a.sort_order - b.sort_order); }

/* Position order for an item: unstarted, then (step seq asc, state sort asc).
   Used to compute how many units are upstream of a logging target, including
   queued entries the server has not seen yet. */
function positionIndex(item, stepId, stateId) {
  const states = stateOrder();
  const steps = [...item.steps].sort((a, b) => a.seq - b.seq);
  if (stepId === null) return 0;
  const stepIdx = steps.findIndex((s) => s.id === stepId);
  const stateIdx = states.findIndex((s) => s.id === stateId);
  return 1 + stepIdx * states.length + stateIdx;
}

function availableUpstream(item, stepId, stateId, isRework) {
  if (!item.state) return item.total_qty;
  const target = positionIndex(item, stepId, stateId);
  let total = 0;
  for (const position of item.state.positions) {
    const index = position.is_unstarted
      ? 0
      : positionIndex(item, position.item_step_id, position.state_id);
    if (isRework ? index > target : index < target) total += position.qty;
  }
  // Overlay queued-but-unsynced events so back-to-back logging offline
  // decrements what the next entry offers. A queued move whose target sits at or
  // beyond ours pulled units out of our upstream window; one that moved units
  // around *within* the window is net zero.
  for (const queued of S.pending) {
    if (queued._status !== "pending" || queued.item_id !== item.id) continue;
    const type = S.ref?.event_types.find((t) => t.id === queued.event_type_id);
    if (!type || !type.moves_quantity) continue;
    const queuedIndex = positionIndex(item, queued.item_step_id, queued.state_id);
    const drains = isRework ? queuedIndex <= target : queuedIndex >= target;
    if (drains) total -= queued.qty;
  }
  return Math.max(total, 0);
}

function pendingCountFor(itemId) {
  return S.pending.filter((q) => q._status === "pending" && q.item_id === itemId).length;
}

/* The position one past (stepId, stateId) on this item's chain — where a batch
   resting there naturally advances to. (null, null) means unstarted; returns
   null past the end of the route. */
function positionAfter(item, stepId, stateId) {
  const states = stateOrder();
  const steps = [...item.steps].sort((a, b) => a.seq - b.seq);
  const next = positionIndex(item, stepId, stateId) + 1;
  if (!steps.length || !states.length || next > steps.length * states.length) return null;
  return {
    stepId: steps[Math.floor((next - 1) / states.length)].id,
    stateId: states[(next - 1) % states.length].id,
  };
}

/* Stable key for one sub-batch position, used in the URL so a tapped batch
   survives the navigation: #/items/{id}/{stepId}.{stateId} */
function positionKey(p) {
  return p.is_unstarted ? "unstarted" : `${p.item_step_id}.${p.state_id}`;
}

function findPosition(item, key) {
  const positions = item.state?.positions || [];
  if (key === "unstarted") return positions.find((p) => p.is_unstarted) || null;
  const [stepId, stateId] = key.split(".");
  return positions.find((p) => p.item_step_id === stepId && p.state_id === stateId) || null;
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
  else if (route === "office") viewOffice();
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
      <h2 style="text-align:center;font-size:22px;margin-bottom:12px">LSF Track</h2>
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
   path: upstream events may have landed since) or discard. */
function viewSyncStatus() {
  const pending = S.pending.filter((q) => q._status === "pending");
  const rejected = S.pending.filter((q) => q._status === "rejected");

  const describe = (q) => {
    const item = S.items.find((i) => i.id === q.item_id);
    const step = item?.steps?.find((s) => s.id === q.item_step_id);
    const stage = step ? stageById(step.stage_id) : null;
    const type = S.ref?.event_types.find((t) => t.id === q.event_type_id);
    const what = [
      type?.name || "entry",
      stage?.name,
      q.state_id ? stateName(q.state_id) : "",
      q.qty ? `${q.qty} pcs` : "",
    ].filter(Boolean).join(" · ");
    return { code: item?.code || "unknown item", what, when: new Date(q._queued_at || Date.now()).toLocaleString() };
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
      <p class="muted" style="margin-bottom:6px">These did not go in. Retry if the situation has changed (e.g. the missing upstream entry has since synced); discard if the entry was a mistake.</p>
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
  // Back on the list: the next sub-batch tap is a fresh arrival, even if it
  // is the same batch as last time.
  logSel._fromKey = null;
  const projects = S.ref?.projects || [];
  const stages = (S.ref?.stages || []).filter((s) => s.is_active);
  const filterProject = sessionStorage.getItem("f-project") || "";
  const filterStage = sessionStorage.getItem("f-stage") || "";
  const filterText = (sessionStorage.getItem("f-q") || "").toLowerCase();

  const rows = S.items.filter((item) => {
    if (!item.is_released) return false;
    if (filterProject && item.project_id !== filterProject) return false;
    if (filterStage) {
      const at = (item.state?.positions || []).some((p) => p.stage_id === filterStage);
      if (!at) return false;
    }
    if (filterText) {
      const haystack = `${item.code} ${item.description}`.toLowerCase();
      if (!haystack.includes(filterText)) return false;
    }
    return true;
  });

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
    </div>
    <div class="row-list">
      ${rows.map(itemRow).join("") || `<p class="muted">No items match.</p>`}
    </div>`;

  document.getElementById("f-project").onchange = (e) => {
    sessionStorage.setItem("f-project", e.target.value); render();
  };
  document.getElementById("f-stage").onchange = (e) => {
    sessionStorage.setItem("f-stage", e.target.value); render();
  };
  // Filter the list live but don't re-render around the keyboard; the input
  // keeps focus and the rows below it re-draw.
  document.getElementById("f-q").oninput = (e) => {
    sessionStorage.setItem("f-q", e.target.value);
    const list = document.querySelector(".row-list");
    if (list) {
      const needle = e.target.value.toLowerCase();
      const filtered = S.items.filter((item) => {
        if (!item.is_released) return false;
        if (filterProject && item.project_id !== filterProject) return false;
        if (filterStage && !(item.state?.positions || []).some((p) => p.stage_id === filterStage)) return false;
        return !needle || `${item.code} ${item.description}`.toLowerCase().includes(needle);
      });
      list.innerHTML = filtered.map(itemRow).join("") || `<p class="muted">No items match.</p>`;
    }
  };
}

function itemRow(item) {
  // One line per sub-batch, mirroring the aging report — and each line is its
  // own tap target: tapping the 40 pcs at carpentry opens the log screen
  // aimed at moving THOSE 40, independent of the 50 sitting at paint. This is
  // the whole workflow: create the item at its project total, then advance
  // batches through production one position at a time.
  const positions = item.state?.positions || [];
  const completed = item.state?.completed_qty || 0;
  const pending = pendingCountFor(item.id);

  const where = (p) => p.is_unstarted
    ? "Not started"
    : `${p.stage_name} · ${stateName(p.state_id)}`;

  const lines = positions.map((p) => `
    <a class="pos-row" href="#/items/${item.id}/${positionKey(p)}"
       aria-label="Advance ${p.qty} pcs from ${esc(where(p))}">
      <span class="muted" style="min-width:0">${esc(where(p))}${p.reworked_qty ? ` <span class="badge rework">R</span>` : ""}</span>
      <span style="white-space:nowrap;display:flex;align-items:center;gap:6px">
        <span class="badge qty">${p.qty}</span>${p.is_unstarted ? "" : `
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
            <div class="muted" style="margin:2px 0 6px">${esc(item.description)}</div>
          </div>
        </div>
      </a>
      ${lines || `<a class="item-head" href="#/items/${item.id}"><span class="badge">not started</span></a>`}
      ${completed ? `
      <div class="spread" style="padding:3px 0 3px 10px;border-left:2px solid var(--good)">
        <span class="muted">Completed</span>
        <span class="badge qty" style="color:var(--good)">${completed}</span>
      </div>` : ""}
      ${pending ? `<div style="margin-top:4px"><span class="badge pending">${pending} pending</span></div>` : ""}
    </div>`;
}

function stateName(stateId) {
  return S.ref?.states.find((s) => s.id === stateId)?.name || "";
}

/* ----------------------------------------------------------- log screen -- */

const logSel = {
  stepId: null, stateId: null, typeId: null, stationId: null, reasonId: null,
  note: "", qtyDefault: null, _fromKey: null,
};

function viewLogScreen(itemId, fromKey) {
  const item = S.items.find((i) => i.id === itemId);
  if (!item) { navigate("#/items"); return; }

  const states = stateOrder();
  const steps = [...item.steps].sort((a, b) => a.seq - b.seq);
  const movableTypes = (S.ref.event_types || []).filter(
    (t) => t.is_active && t.moves_quantity);
  const defaultType = movableTypes.find((t) => !t.is_rework) || movableTypes[0];

  if (!logSel.typeId || !movableTypes.some((t) => t.id === logSel.typeId)) {
    logSel.typeId = defaultType?.id;
  }
  const type = movableTypes.find((t) => t.id === logSel.typeId);

  // Arrived by tapping a specific sub-batch on the item card: aim the form at
  // moving THAT batch — target its next position, default the quantity to its
  // size. Applied once per arrival (the guard), so segment taps and background
  // re-renders don't fight the engineer's own adjustments afterwards.
  const fromPos = fromKey ? findPosition(item, fromKey) : null;
  if (fromPos && logSel._fromKey !== `${itemId}/${fromKey}`) {
    logSel._fromKey = `${itemId}/${fromKey}`;
    const target = positionAfter(
      item,
      fromPos.is_unstarted ? null : fromPos.item_step_id,
      fromPos.is_unstarted ? null : fromPos.state_id,
    );
    if (target) {
      logSel.typeId = defaultType?.id || logSel.typeId;
      logSel.stepId = target.stepId;
      logSel.stateId = target.stateId;
      logSel.stationId = null;
      logSel.qtyDefault = fromPos.qty;
    }
  }

  // The track constrains the picker: only steps where units rest, plus the
  // immediate next step, are offered. Rework may target any step at or before
  // the furthest units. Free jumps down the route would negate the route.
  const allowed = allowedStepIds(item, steps, states, !!type?.is_rework);
  const shownSteps = steps.filter((s) => allowed.has(s.id));
  if (!shownSteps.some((s) => s.id === logSel.stepId)) {
    const target = defaultTarget(item, steps, states);
    logSel.stepId = allowed.has(target.stepId) ? target.stepId : shownSteps[0]?.id;
    logSel.stateId = target.stateId;
    logSel.qtyDefault = null; // the batch context can't survive a target reset
  }
  if (!states.some((s) => s.id === logSel.stateId)) logSel.stateId = states[0]?.id;

  const step = steps.find((s) => s.id === logSel.stepId);
  const stage = step ? stageById(step.stage_id) : null;
  const stations = stage
    ? S.ref.stations.filter((st) => st.stage_id === stage.id && st.is_active)
    : [];
  if (stage?.requires_station && !stations.some((st) => st.id === logSel.stationId)) {
    logSel.stationId = localStorage.getItem(`station:${stage.id}`) || stations[0]?.id || null;
  }
  const available = step
    ? availableUpstream(item, step.id, logSel.stateId, !!type?.is_rework)
    : 0;
  const reasons = (S.ref.reason_codes || []).filter((r) => r.is_active);
  // Keep the tracked selection valid: a re-render must not silently reset the
  // reason to the first option, and an empty reason list must never turn into
  // an empty-string id in the payload.
  if (type?.requires_reason_code && !reasons.some((r) => r.id === logSel.reasonId)) {
    logSel.reasonId = reasons[0]?.id || null;
  }

  // Completing a non-final step usually means the units move straight to the
  // next stage's queue; offer to log both in one tap.
  const selectedState = states.find((s) => s.id === logSel.stateId);
  const stepIndex = steps.findIndex((s) => s.id === logSel.stepId);
  const nextStep = stepIndex >= 0 ? steps[stepIndex + 1] : null;
  const nextStage = nextStep ? stageById(nextStep.stage_id) : null;
  const offerAutoQueue = !!(
    selectedState?.is_complete && nextStep && type && !type.is_rework
  );

  $view.innerHTML = `
    <div class="card">
      <div style="display:flex;gap:12px;align-items:center">
        ${item.icon_url ? `<img class="item-icon" src="${item.icon_url}" alt="">` : ""}
        <div style="flex:1;min-width:0">
          <div class="spread">
            <h2>${esc(item.code)}</h2>
            <span class="muted">${item.total_qty} pcs · rev ${esc(item.drawing_revision)}</span>
          </div>
          <div class="muted">${esc(item.description)}</div>
        </div>
      </div>
    </div>

    <div class="card">
      ${fromPos && logSel.qtyDefault != null ? `
      <div class="batch-context">
        Moving the batch of <strong>${fromPos.qty}</strong> from
        <strong>${esc(fromPos.is_unstarted ? "Not started" : `${fromPos.stage_name} · ${stateName(fromPos.state_id)}`)}</strong>
        — adjust anything below before logging.
      </div>` : ""}
      ${movableTypes.length > 1 ? `
      <label id="lbl-type">Entry type</label>
      <div class="seg" id="seg-type" role="group" aria-labelledby="lbl-type">
        ${movableTypes.map((t) => `<button data-id="${t.id}" class="${t.id === logSel.typeId ? "on" : ""}" aria-pressed="${t.id === logSel.typeId}">${esc(t.name)}</button>`).join("")}
      </div>` : ""}

      <label id="lbl-step">Stage</label>
      <div class="seg" id="seg-step" role="group" aria-labelledby="lbl-step">
        ${shownSteps.map((s) => {
          const st = stageById(s.stage_id);
          return `<button data-id="${s.id}" class="${s.id === logSel.stepId ? "on" : ""}" aria-pressed="${s.id === logSel.stepId}">${esc(st?.name || "?")}</button>`;
        }).join("")}
      </div>

      <label id="lbl-state">State</label>
      <div class="seg" id="seg-state" role="group" aria-labelledby="lbl-state">
        ${states.filter((s) => s.is_active !== false).map((s) => `<button data-id="${s.id}" class="${s.id === logSel.stateId ? "on" : ""}" aria-pressed="${s.id === logSel.stateId}">${esc(s.name)}</button>`).join("")}
      </div>

      ${stage?.requires_station ? `
      <label id="lbl-station">Station</label>
      <div class="seg" id="seg-station" role="group" aria-labelledby="lbl-station">
        ${stations.map((st) => `<button data-id="${st.id}" class="${st.id === logSel.stationId ? "on" : ""}" aria-pressed="${st.id === logSel.stationId}">${esc(st.name)}</button>`).join("")}
      </div>` : ""}

      ${type?.requires_reason_code ? `
      <label for="sel-reason">Reason</label>
      <select id="sel-reason">
        ${reasons.map((r) => `<option value="${r.id}" ${r.id === logSel.reasonId ? "selected" : ""}>${esc(r.name)}</option>`).join("")}
      </select>` : ""}

      <label for="qty">Quantity</label>
      <div class="qty-row">
        <button id="qty-minus" aria-label="One fewer">−</button>
        <input id="qty" type="number" inputmode="numeric" min="1"
               value="${logSel.qtyDefault != null ? Math.min(logSel.qtyDefault, available || logSel.qtyDefault) : (available || 1)}">
        <button id="qty-plus" aria-label="One more">+</button>
      </div>
      <p class="muted" style="margin-top:5px">${available} available at the previous step</p>
      ${stage?.requires_external_po ? `<p class="muted">External supplier stage — time in state is supplier lead time.</p>` : ""}
      ${stage && !stage.allows_partial_qty ? `<p class="muted">Whole-batch stage — a move that leaves units behind is rejected.</p>` : ""}
      <p class="warn-text" id="qty-warn" hidden>More than is available upstream — the server will reject this entry until the earlier steps are logged. It will wait under the sync pill with a Retry button.</p>

      ${offerAutoQueue ? `
      <label style="display:flex;align-items:center;gap:10px;margin-top:14px;font-size:15px;color:var(--text)">
        <input type="checkbox" id="auto-queue" checked style="width:22px;height:22px;flex:none">
        Also queue at ${esc(nextStage?.name || "next stage")}
      </label>` : ""}

      <details data-panel="log-note" ${logSel.note ? "open" : ""} style="margin-top:12px">
        <summary class="muted" style="cursor:pointer">Add note${logSel.note ? " ·" : ""}</summary>
        <input id="log-note" maxlength="2000" placeholder="e.g. rack 3, waiting on fittings"
               value="${esc(logSel.note)}" style="margin-top:6px">
      </details>

      <div style="height:12px" aria-hidden="true"></div>
      <div class="action-sticky">
        <button class="primary" id="log-go">Log entry</button>
      </div>
    </div>

    <div class="card">
      <h2>Photos <span class="muted" id="photo-count"></span></h2>
      <div id="photos" class="muted">Loading…</div>
      <div style="height:8px"></div>
      <label class="ghost" style="display:block;text-align:center;padding:12px;border:1px dashed var(--line);border-radius:var(--radius);cursor:pointer${S.online ? "" : ";opacity:.5"}">
        ${S.online ? "Add snag photo" : "Photos need a connection"}
        <input type="file" id="photo-file" accept="image/*" capture="environment" hidden ${S.online ? "" : "disabled"}>
      </label>
    </div>

    <div class="card">
      <h2>Recent entries</h2>
      <div id="recent" class="muted">Loading…</div>
    </div>`;

  const rerender = () => viewLogScreen(itemId, fromKey);
  wireSeg("seg-type", (id) => { logSel.typeId = id; rerender(); });
  wireSeg("seg-step", (id) => { logSel.stepId = id; logSel.stationId = null; rerender(); });
  wireSeg("seg-state", (id) => { logSel.stateId = id; rerender(); });
  wireSeg("seg-station", (id) => {
    logSel.stationId = id;
    if (stage) localStorage.setItem(`station:${stage.id}`, id);
    rerender();
  });
  const $reason = document.getElementById("sel-reason");
  if ($reason) $reason.onchange = (e) => { logSel.reasonId = e.target.value || null; };
  // Tracked in logSel like every other selection, so a segment tap's re-render
  // cannot eat a half-typed note.
  const $note = document.getElementById("log-note");
  if ($note) $note.oninput = (e) => { logSel.note = e.target.value; };

  const $qty = document.getElementById("qty");
  const warn = () => {
    document.getElementById("qty-warn").hidden = Number($qty.value) <= available;
  };
  $qty.oninput = warn;
  document.getElementById("qty-minus").onclick = () => { $qty.value = Math.max(1, Number($qty.value) - 1); warn(); };
  document.getElementById("qty-plus").onclick = () => { $qty.value = Number($qty.value) + 1; warn(); };

  document.getElementById("log-go").onclick = async () => {
    const qty = Number($qty.value) || 1;
    const logged = await logEvent({
      id: newId(),
      item_id: item.id,
      item_step_id: logSel.stepId,
      station_id: stage?.requires_station ? logSel.stationId : null,
      event_type_id: logSel.typeId,
      state_id: logSel.stateId,
      qty,
      reason_code_id: type?.requires_reason_code ? logSel.reasonId : null,
      occurred_at: new Date().toISOString(),
      note: logSel.note.trim() || null,
      supersedes_event_id: null,
      user_id: S.user?.id || null,
    });
    if (!logged) return;
    logSel.note = "";
    logSel.qtyDefault = null;
    logSel._fromKey = null;
    if (fromKey) {
      // The batch has moved on; drop its key from the URL without a reload so
      // the next render defaults normally.
      history.replaceState(null, "", `#/items/${item.id}`);
      fromKey = undefined;
    }
    // One tap, two facts: done here, queued there. The +1ms keeps the replay
    // order deterministic so the queue event always pulls the units the
    // completion just produced.
    if (offerAutoQueue && document.getElementById("auto-queue")?.checked) {
      await logEvent({
        id: newId(),
        item_id: item.id,
        item_step_id: nextStep.id,
        station_id: null,
        event_type_id: logSel.typeId,
        // The queue state is flagged, not positional: a state added at a lower
        // sort_order must not change what "queue at next stage" writes.
        state_id: (states.find((s) => s.is_initial) || states[0]).id,
        qty,
        reason_code_id: null,
        occurred_at: new Date(Date.now() + 1).toISOString(),
        note: null,
        supersedes_event_id: null,
        user_id: S.user?.id || null,
      });
    }
    dirtyFields.clear(); // the form resets to fresh defaults after a log
    rerender();
  };

  const wireUpload = (inputId, kind, doneMessage, refreshList) => {
    const input = document.getElementById(inputId);
    if (!input) return;
    input.onchange = async () => {
      const file = input.files[0];
      if (!file) return;
      const form = new FormData();
      form.append("file", file);
      form.append("kind", kind);
      try {
        const response = await fetch(`/api/items/${item.id}/images`, {
          method: "POST", body: form, credentials: "same-origin",
        });
        if (!response.ok) throw new Error();
        toast(doneMessage);
        logScreenCache.fetchedAt = 0; // the photo list just changed
        if (refreshList) {
          await sync(); // pulls the new icon_url into S.items, re-renders
        } else {
          loadPhotos(item);
        }
      } catch {
        toast("Upload failed");
      } finally {
        input.value = "";
      }
    };
  };
  wireUpload("photo-file", "snag", "Snag photo added", false);

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

/* Which steps the picker may offer, derived from where quantity actually is.
   A normal move targets an occupied step or the one right after it; rework may
   return to any step at or before the furthest units. */
function allowedStepIds(item, steps, states, isRework) {
  const occupied = new Set();
  let furthest = -1;
  let hasUnstarted = false;
  for (const p of item.state?.positions || []) {
    if (p.qty <= 0) continue;
    if (p.is_unstarted) { hasUnstarted = true; continue; }
    const index = steps.findIndex((s) => s.id === p.item_step_id);
    if (index < 0) continue;
    occupied.add(index);
    furthest = Math.max(furthest, index);
  }
  const allowed = new Set();
  if (isRework) {
    for (let i = 0; i <= furthest; i++) allowed.add(i);
  } else {
    if (hasUnstarted) allowed.add(0);
    for (const index of occupied) {
      allowed.add(index);
      if (index + 1 < steps.length) allowed.add(index + 1);
    }
  }
  if (!allowed.size && steps.length) allowed.add(0);
  return new Set([...allowed].map((i) => steps[i].id));
}

/* The natural next entry: one position past wherever the oldest quantity is
   resting. Units sitting at carpentry/completed default the form to
   veneer/queued — the move the engineer is most likely about to log. */
function defaultTarget(item, steps, states) {
  const fallback = { stepId: steps[0]?.id || null, stateId: states[0]?.id || null };
  const occupied = (item.state?.positions || [])
    .filter((p) => p.qty > 0)
    .map((p) => (p.is_unstarted ? 0 : positionIndex(item, p.item_step_id, p.state_id)))
    .sort((a, b) => a - b);
  const last = steps.length * states.length;
  for (const index of occupied) {
    const next = index + 1;
    if (next > last) continue; // fully finished; try the next bucket up
    const stepIdx = Math.floor((next - 1) / states.length);
    return { stepId: steps[stepIdx].id, stateId: states[(next - 1) % states.length].id };
  }
  return fallback;
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
            <a href="${img.url}" target="_blank" rel="noopener" style="position:relative;display:block">
              <img src="${img.url}" alt="${esc(img.note || img.filename)}" loading="lazy"
                   style="width:100%;aspect-ratio:1;object-fit:cover;border-radius:8px;border:1px solid var(--line)">
              ${img.kind === "icon" ? `<span class="badge" style="position:absolute;top:4px;left:4px;background:rgba(0,0,0,.55)">icon</span>` : ""}
            </a>`).join("")}
         </div>`
      : `<span class="muted">No photos yet.</span>`;
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
    target.innerHTML = events.map((e) => {
      const type = S.ref.event_types.find((t) => t.id === e.event_type_id);
      const label = [
        type?.name || "event",
        e.qty ? `${e.qty} pcs` : "",
        e.state_id ? stateName(e.state_id) : "",
      ].filter(Boolean).join(" · ");
      const canVoid = correctionType && type?.moves_quantity && !e.superseded;
      return `
        <div class="spread" style="padding:6px 0;border-bottom:1px solid var(--line)">
          <span>${esc(label)}<br><span class="muted">${new Date(e.occurred_at).toLocaleString()}</span>${e.note ? `<br><span class="muted">“${esc(e.note)}”</span>` : ""}</span>
          ${canVoid ? `<button class="ghost" data-void="${e.id}" data-step="${e.item_step_id || ""}" data-state="${e.state_id || ""}">Void</button>` : ""}
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
          state_id: null,
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
  const projects = S.ref?.projects || [];
  const stages = (S.ref?.stages || []).filter((s) => s.is_active);
  const fProject = sessionStorage.getItem("r-project") || "";
  const fStage = sessionStorage.getItem("r-stage") || "";
  const fDays = sessionStorage.getItem("r-days") || "";

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
    const wipQuery = fProject ? `?project_id=${fProject}` : "";
    const agingParams = new URLSearchParams({ limit: "40" });
    if (fProject) agingParams.set("project_id", fProject);
    if (fStage) agingParams.set("stage_id", fStage);
    if (fDays) agingParams.set("min_days", fDays);
    [wip, aging, exceptions] = await Promise.all([
      fetchReport(`/api/reports/wip${wipQuery}`),
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
  const stateCodes = wip.states.map((s) => s.code);

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
            <span class="muted">${row.total_qty} pcs · queue ${row.queue_qty}${row.reworked_qty ? ` · <span class="badge rework">rework ${row.reworked_qty}</span>` : ""}</span>
          </div>
          <div class="bar-track" style="margin:4px 0"><div class="bar-fill" style="width:${(row.total_qty / maxQty) * 100}%"></div></div>
          <div class="muted">
            ${stateCodes.map((code) => `${esc(code)} ${row.by_state[code] || 0}`).join(" · ")}
          </div>
          ${row.stations.filter((s) => s.station).map((s) =>
            `<div class="muted">· ${esc(s.station.name)}: ${s.total_qty}</div>`).join("")}
        </div>`).join("") || `<p class="muted">Nothing in progress.</p>`}
      ${wip.unstarted.qty ? `<p class="muted">Not started: ${wip.unstarted.qty} pcs across ${wip.unstarted.item_count} items</p>` : ""}
    </div>

    <div class="card">
      <h2>Aging — days in current state</h2>
      <div class="table-scroll">
      <table>
        <thead><tr><th scope="col">Item</th><th scope="col">Where</th><th scope="col" class="num">Qty</th><th scope="col" class="num">Days</th></tr></thead>
        <tbody>
          ${aging.rows.map((row) => `
            <tr>
              <td><a href="#/items/${row.item.id}" style="color:inherit">${esc(row.item.code)}</a></td>
              <td>${esc(row.label)}${row.reworked_qty ? ` <span class="badge rework">R</span>` : ""}</td>
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
          <span class="badge">${esc(row.code)}</span> <strong>${esc(row.item_code || "")}</strong>
          <div class="muted">${esc(row.detail)}</div>
        </div>`).join("")}
    </div>` : ""}`;

  for (const [id, key] of [["r-project", "r-project"], ["r-stage", "r-stage"], ["r-days", "r-days"]]) {
    document.getElementById(id).onchange = (e) => {
      sessionStorage.setItem(key, e.target.value);
      render();
    };
  }
}

/* --------------------------------------------------------------- office -- */

/* Stage sequence being assembled in the "New route" card. Survives re-renders
   of the Office view within a session. */
let routeDraft = [];
/* Where the next tapped stage lands in the draft: an index, or null to append.
   This is how a stage gets slotted BETWEEN two existing steps — the versioned
   template then carries the insertion with seq gaps of 10. */
let routeInsertAt = null;

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

function viewOffice() {
  const projects = S.ref?.projects || [];
  const templates = (S.ref?.route_templates || []).filter((t) => t.is_published);
  const stages = (S.ref?.stages || []).filter((s) => s.is_active);
  const unreleased = S.items.filter((i) => !i.is_released);
  const released = S.items.filter((i) => i.is_released);
  const inFlight = (item) =>
    (item.state?.positions || [])
      .filter((p) => !p.is_unstarted)
      .reduce((sum, p) => sum + p.qty, 0);

  $view.innerHTML = `
    <div class="card">
      <h2>New item</h2>
      <div class="field-grid">
        <div><label>Code</label><input id="ni-code" autocapitalize="characters"></div>
        <div><label>Project</label>
          <select id="ni-project">${projects.filter((p) => p.is_active !== false).map((p) => `<option value="${p.id}">${esc(p.code)}</option>`).join("")}</select>
        </div>
      </div>
      <label>Description</label><input id="ni-desc">
      <div class="field-grid">
        <div><label>Total qty</label><input id="ni-qty" type="number" inputmode="numeric" min="1"></div>
        <div><label>Drawing rev</label><input id="ni-rev" value="A"></div>
      </div>
      <div class="field-grid">
        <div><label>Target release date</label><input id="ni-date" type="date"></div>
        <div><label>Item icon (photo)</label><input id="ni-icon" type="file" accept="image/*" style="padding:10px"></div>
      </div>
      <div style="height:12px"></div>
      <button class="primary" id="ni-go" ${S.online ? "" : "disabled"}>Create item</button>
      ${S.online ? "" : `<p class="warn-text">Office tasks need a connection.</p>`}
      <p class="warn-text" id="ni-err" hidden></p>
    </div>

    <div class="card">
      <h2>New project</h2>
      <div class="field-grid">
        <div><label>Code</label><input id="np-code" autocapitalize="characters" placeholder="HOTEL-B"></div>
        <div><label>Client</label><input id="np-client"></div>
      </div>
      <label>Name</label><input id="np-name">
      <div style="height:12px"></div>
      <button class="primary" id="np-go" ${S.online ? "" : "disabled"}>Create project</button>
      <p class="warn-text" id="np-err" hidden></p>
    </div>

    <div class="card">
      <h2>New route</h2>
      <div class="field-grid">
        <div><label>Code</label><input id="nr-code" autocapitalize="none" placeholder="casegoods_standard"></div>
        <div><label>Name</label><input id="nr-name"></div>
      </div>
      <label>Tap stages in production order</label>
      <div class="seg" id="nr-stages">
        ${stages.map((s) => `<button data-add-stage="${s.id}">${esc(s.name)}</button>`).join("")}
      </div>
      <label>Sequence${routeDraft.length ? " — tap a step to remove it, tap a ＋ to insert there" : ""}</label>
      <div id="nr-seq"></div>
      <p class="muted" id="nr-version-hint" style="margin-top:6px"></p>
      <div style="height:12px"></div>
      <button class="primary" id="nr-go" ${S.online && routeDraft.length ? "" : "disabled"}>Create route</button>
      <p class="warn-text" id="nr-err" hidden></p>
    </div>

    <div class="card">
      <h2>Awaiting release (${unreleased.length})</h2>
      ${unreleased.map((item) => `
        <div style="padding:10px 0;border-bottom:1px solid var(--line)">
          <div class="spread">
            <strong>${esc(item.code)}</strong>
            <span class="muted">${item.total_qty} pcs · rev ${esc(item.drawing_revision)}</span>
          </div>
          <div class="muted" style="margin-bottom:6px">${esc(item.description)}</div>
          <div class="qty-row">
            <select data-route-for="${item.id}" style="flex:1">
              ${templates.map((t) => `<option value="${t.id}">${esc(t.name)} v${t.version} (${t.steps.length} steps)</option>`).join("")}
            </select>
            <button class="ghost" data-release="${item.id}" ${S.online ? "" : "disabled"} style="width:auto;padding:8px 16px">Release</button>
          </div>
          <details data-panel="dist:${item.id}" style="margin-top:8px">
            <summary class="muted" style="cursor:pointer">Already mid-production? Distribute the ${item.total_qty} pcs</summary>
            <div data-dist-for="${item.id}" style="margin-top:6px"></div>
          </details>
        </div>`).join("") || `<p class="muted">Nothing waiting.</p>`}
    </div>

    ${released.length ? `
    <div class="card">
      <h2>Drawing revisions</h2>
      <p class="muted" style="margin-bottom:8px">A bump records that a new drawing was issued
      after release: everything already in production was built to the old revision, and the
      quantity affected is derived from the log, never typed in.</p>
      ${released.map((item) => `
        <div style="padding:10px 0;border-bottom:1px solid var(--line)">
          <div class="spread">
            <strong>${esc(item.code)}</strong>
            <span class="muted">rev ${esc(item.drawing_revision)} · ${inFlight(item)} pcs in flight</span>
          </div>
          <div class="qty-row" style="margin-top:6px">
            <input data-rev-for="${item.id}" placeholder="new rev, e.g. ${esc(nextRevision(item.drawing_revision))}"
                   autocapitalize="characters" maxlength="32" style="flex:1;text-align:left;padding:10px">
            <button class="ghost" data-bump="${item.id}" ${S.online ? "" : "disabled"}
                    style="width:auto;padding:8px 16px">Bump</button>
          </div>
        </div>`).join("")}
    </div>` : ""}

    ${S.user?.is_admin ? `
    <div class="card">
      <h2>Manage</h2>
      <p class="muted" style="margin-bottom:8px">Removal keeps history: an item with logged events is archived (hidden everywhere), never destroyed. Projects archive once their items are gone; routes are unpublished, leaving released items untouched.</p>
      <label>Items</label>
      <div id="mg-items" class="muted">Loading…</div>
      <label>Projects</label>
      <div id="mg-projects"></div>
      <label>Route versions</label>
      <div id="mg-routes"></div>
    </div>

    <div class="card">
      <h2>Stages &amp; stations</h2>
      <p class="muted" style="margin-bottom:8px">Adding a stage needs no deploy and no code
      change: add it here with its behaviour flags, add stations if it has physical
      instances, then create a new route version with the stage slotted in. Items already
      in production keep the route they were released against.</p>
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
    </div>

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
    </div>` : ""}`;

  document.getElementById("ni-go").onclick = async () => {
    const err = document.getElementById("ni-err");
    err.hidden = true;
    try {
      const created = await api("/api/items", {
        method: "POST",
        body: JSON.stringify({
          code: document.getElementById("ni-code").value.trim(),
          project_id: document.getElementById("ni-project").value,
          description: document.getElementById("ni-desc").value.trim(),
          total_qty: Number(document.getElementById("ni-qty").value),
          drawing_revision: document.getElementById("ni-rev").value.trim() || "A",
          target_release_date: document.getElementById("ni-date").value || null,
        }),
      });
      const iconFile = document.getElementById("ni-icon").files[0];
      if (iconFile) {
        const form = new FormData();
        form.append("file", iconFile);
        form.append("kind", "icon");
        const uploaded = await fetch(`/api/items/${created.id}/images`, {
          method: "POST", body: form, credentials: "same-origin",
        });
        // The item exists either way; a failed icon shouldn't look like a
        // failed creation.
        toast(uploaded.ok ? "Item created with icon" : "Item created — icon upload failed");
      } else {
        toast("Item created");
      }
      await sync();
    } catch (error) {
      err.textContent = error.body?.detail?.reason || error.body?.detail || "Could not create the item.";
      err.hidden = false;
    }
  };

  document.getElementById("np-go").onclick = async () => {
    const err = document.getElementById("np-err");
    err.hidden = true;
    try {
      const created = await api("/api/projects", {
        method: "POST",
        body: JSON.stringify({
          code: document.getElementById("np-code").value.trim(),
          name: document.getElementById("np-name").value.trim(),
          client: document.getElementById("np-client").value.trim() || null,
        }),
      });
      toast(`Project ${created.code} created`);
      await sync(); // refreshes the reference cache; re-renders with it in the pickers
    } catch (error) {
      err.textContent = error.body?.detail || "Could not create the project.";
      err.hidden = false;
    }
  };

  // ---- route builder: updates in place so typed inputs survive ----
  const renderSeq = () => {
    const chip = (stageId, i) =>
      `<span class="badge qty" data-remove-step="${i}" style="margin:0 4px 6px 0;padding:8px 12px">${i + 1}. ${esc(stageById(stageId)?.name || "?")}</span>`;
    const slot = (i) =>
      `<button data-insert-at="${i}" class="insert-slot${routeInsertAt === i ? " on" : ""}" title="Insert here">＋</button>`;
    document.getElementById("nr-seq").innerHTML = routeDraft.length
      ? routeDraft.map((stageId, i) => slot(i) + chip(stageId, i)).join("") + slot(routeDraft.length)
      : `<span class="muted">Empty — a route needs at least one stage.</span>`;
    document.getElementById("nr-go").disabled = !S.online || !routeDraft.length;
    document.querySelectorAll("[data-remove-step]").forEach((el) => {
      el.onclick = () => {
        routeDraft.splice(Number(el.dataset.removeStep), 1);
        routeInsertAt = null;
        renderSeq();
      };
    });
    document.querySelectorAll("[data-insert-at]").forEach((el) => {
      el.onclick = () => {
        const at = Number(el.dataset.insertAt);
        routeInsertAt = routeInsertAt === at ? null : at;
        renderSeq();
      };
    });
  };
  renderSeq();

  document.querySelectorAll("[data-add-stage]").forEach((button) => {
    button.onclick = () => {
      if (routeInsertAt === null || routeInsertAt >= routeDraft.length) {
        routeDraft.push(button.dataset.addStage);
        routeInsertAt = null;
      } else {
        routeDraft.splice(routeInsertAt, 0, button.dataset.addStage);
        routeInsertAt += 1; // consecutive taps keep inserting in order
      }
      renderSeq();
    };
  });

  const versionHint = () => {
    const code = document.getElementById("nr-code").value.trim().toLowerCase().replace(/ /g, "_");
    const versions = templates.filter((t) => t.code === code).map((t) => t.version);
    document.getElementById("nr-version-hint").textContent = versions.length
      ? `${code} exists — this will create v${Math.max(...versions) + 1}; items already released keep their old route.`
      : "";
  };
  document.getElementById("nr-code").oninput = versionHint;
  versionHint();

  document.getElementById("nr-go").onclick = async () => {
    const err = document.getElementById("nr-err");
    err.hidden = true;
    try {
      const created = await api("/api/routes", {
        method: "POST",
        body: JSON.stringify({
          code: document.getElementById("nr-code").value.trim(),
          name: document.getElementById("nr-name").value.trim(),
          stage_ids: routeDraft,
        }),
      });
      routeDraft = [];
      routeInsertAt = null;
      toast(`Route ${created.code} v${created.version} created`);
      await sync();
    } catch (error) {
      err.textContent = error.body?.detail || "Could not create the route.";
      err.hidden = false;
    }
  };

  // Optional initial distribution: qty inputs per step of the selected route,
  // for items entering the system already mid-production.
  const renderDistribution = (itemId) => {
    const select = $view.querySelector(`[data-route-for="${itemId}"]`);
    const container = $view.querySelector(`[data-dist-for="${itemId}"]`);
    if (!select || !container) return;
    const template = templates.find((t) => t.id === select.value);
    container.innerHTML = (template?.steps || []).map((step) => `
      <div class="spread" style="padding:4px 0">
        <span class="muted">${step.seq} · ${esc(stageById(step.stage_id)?.name || "?")}</span>
        <input type="number" inputmode="numeric" min="0" placeholder="0"
               data-dist-seq="${step.seq}" style="width:90px;padding:8px;text-align:center">
      </div>`).join("");
  };

  $view.querySelectorAll("[data-route-for]").forEach((select) => {
    const itemId = select.dataset.routeFor;
    renderDistribution(itemId);
    select.addEventListener("change", () => renderDistribution(itemId));
  });

  $view.querySelectorAll("[data-bump]").forEach((button) => {
    button.onclick = async () => {
      const item = S.items.find((i) => i.id === button.dataset.bump);
      const revision = $view.querySelector(`[data-rev-for="${item.id}"]`).value.trim();
      if (!revision) return;
      const affected = inFlight(item);
      const detail = affected
        ? `${affected} pcs in flight were built to rev ${item.drawing_revision}.`
        : "Nothing is in flight yet.";
      if (!confirm(`Bump ${item.code} from rev ${item.drawing_revision} to ${revision}? ${detail}`)) return;
      try {
        await api(`/api/items/${item.id}/revision`, {
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

  if (S.user?.is_admin) {
    loadManage();
    renderStageAdmin();
    loadUsers();
  }

  $view.querySelectorAll("[data-release]").forEach((button) => {
    button.onclick = async () => {
      const itemId = button.dataset.release;
      const select = $view.querySelector(`[data-route-for="${itemId}"]`);
      const template = templates.find((t) => t.id === select.value);

      const distribution = {};
      $view.querySelectorAll(`[data-dist-for="${itemId}"] [data-dist-seq]`).forEach((input) => {
        const qty = Number(input.value);
        if (qty > 0) distribution[input.dataset.distSeq] = qty;
      });
      const distributed = Object.values(distribution).reduce((a, b) => a + b, 0);
      const summary = distributed
        ? ` ${distributed} pcs start mid-route; the rest start unstarted.`
        : "";
      if (!confirm(`Release against ${template.name} v${template.version}? The route is frozen from here.${summary}`)) return;
      try {
        await api(`/api/items/${itemId}/release`, {
          method: "POST",
          body: JSON.stringify({
            route_template_id: select.value,
            initial_quantities: distribution,
          }),
        });
        toast("Released to production");
        await sync();
      } catch (error) {
        alert(error.body?.detail || "Release failed.");
      }
    };
  });
}

/* ----------------------------------------------- stage admin (runtime) -- */

/* The behaviour flags a stage can carry. Labels only — nothing here branches
   on what any particular stage means. */
const STAGE_FLAGS = [
  ["requires_station", "Has physical stations"],
  ["requires_external_po", "External supplier (lead time, PO)"],
  ["allows_partial_qty", "Allows partial quantities"],
  ["is_terminal", "Terminal — completes the item"],
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
      toast(`Stage ${name} added — slot it into a route version to use it`);
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

/* Admin management lists: items (incl. archived), projects, route versions,
   each with a Remove action. The server decides archive vs delete. */
async function loadManage() {
  const $items = document.getElementById("mg-items");
  if (!$items) return;

  const row = (label, sub, attr, id, action = "Remove") => `
    <div class="spread" style="padding:6px 0;border-bottom:1px solid var(--line)">
      <span style="min-width:0">${label}${sub ? `<br><span class="muted">${sub}</span>` : ""}</span>
      <button class="ghost" ${attr}="${id}" style="flex:none">${action}</button>
    </div>`;

  try {
    const { items } = await api("/api/items?include_archived=true");
    if (!$items.isConnected) return;
    $items.innerHTML = items.map((item) => row(
      esc(item.code),
      `${esc(item.description)} · ${!item.is_active ? "archived" : item.is_released ? "released" : "not released"}`,
      "data-rm-item", item.id, item.is_active ? "Remove" : "Archived",
    )).join("") || `<span class="muted">No items.</span>`;
  } catch {
    $items.textContent = "Management needs a connection.";
    return;
  }

  const projects = S.ref?.projects || [];
  document.getElementById("mg-projects").innerHTML = projects.map((p) => row(
    esc(p.code), p.is_active ? esc(p.name) : `${esc(p.name)} · archived`,
    "data-rm-project", p.id, p.is_active ? "Remove" : "Archived",
  )).join("") || `<span class="muted">No projects.</span>`;

  const routes = (S.ref?.route_templates || []).filter((t) => t.is_published);
  document.getElementById("mg-routes").innerHTML = routes.map((t) => row(
    `${esc(t.name)} v${t.version}`, `${t.steps.length} steps`,
    "data-rm-route", t.id, "Unpublish",
  )).join("") || `<span class="muted">No published routes.</span>`;

  const wire = (attr, confirmText, request, done) => {
    document.querySelectorAll(`[${attr}]`).forEach((button) => {
      if (button.textContent === "Archived") { button.disabled = true; return; }
      button.onclick = async () => {
        if (!confirm(confirmText)) return;
        try {
          const result = await request(button.getAttribute(attr));
          toast(done(result));
          await sync();
        } catch (error) {
          alert(error.body?.detail || "Could not remove it.");
        }
      };
    });
  };
  wire("data-rm-item",
    "Remove this item? If it has logged events it is archived with its history; otherwise it is deleted.",
    (id) => api(`/api/items/${id}`, { method: "DELETE" }),
    (r) => (r.archived ? "Item archived (history kept)" : "Item deleted"));
  wire("data-rm-project",
    "Archive this project? Its items must be removed or archived first.",
    (id) => api(`/api/projects/${id}`, { method: "DELETE" }),
    () => "Project archived");
  wire("data-rm-route",
    "Unpublish this route version? Items already released keep their route.",
    (id) => api(`/api/routes/${id}`, { method: "DELETE" }),
    () => "Route version unpublished");
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
  }
  // Unsynced events must not sit in storage the browser considers evictable.
  if (navigator.storage?.persist) navigator.storage.persist().catch(() => {});

  try {
    // Come up from cache first so the app is usable before any network round trip.
    S.ref = (await LSF_DB.get("reference")) || null;
    S.items = (await LSF_DB.get("items")) || [];
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
