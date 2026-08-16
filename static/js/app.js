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
      if (error.auth || isNetworkError(error)) throw error;
      // The server answered but refused the whole batch — one malformed entry
      // fails request validation for all of them. Post one at a time so a
      // single poison entry quarantines alone instead of wedging the queue.
      results = await drainOneByOne(events);
    }
    let divergent = 0;
    for (const result of results) {
      if (result.status === "stored" || result.status === "duplicate") {
        await LSF_DB.ack(result.id);
        if (result.divergent) divergent += 1;
      } else {
        await LSF_DB.markRejected(result.id, result.reason || "rejected");
      }
    }
    if (divergent) {
      toast(`${divergent} ${divergent === 1 ? "entry" : "entries"} already existed with different details`);
    }
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
      if (error.auth || isNetworkError(error)) throw error;
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
      } else {
        S.online = false; // network failed; queue stays, we try again later
      }
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
  render(true); // background refresh; defers if the user is mid-form
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

/* -------------------------------------------------------------- routing -- */

function navigate(hash) { location.hash = hash; }

// Wrapped so the Event object is not mistaken for the `background` flag.
window.addEventListener("hashchange", () => render());

/* Fields the user has actually typed into since the last deliberate render.
   A background sync must not clobber them with freshly-rendered defaults. */
const dirtyFields = new Set();
$view.addEventListener("input", (e) => { if (e.target.id) dirtyFields.add(e.target.id); });

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

  const [, route, arg] = hash.split("/");
  document.querySelectorAll("#tabs a").forEach((a) =>
    a.classList.toggle("active", a.dataset.tab === route));

  // A background sync re-renders whatever view is open. Don't let that eat a
  // half-typed form: skip the refresh entirely while a field is focused (the
  // next sync catches up), and carry user-typed values over regardless.
  const typing = $view.contains(document.activeElement) &&
    /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName);
  if (background && typing) return;
  if (!background) dirtyFields.clear();
  const saved = {};
  $view.querySelectorAll("input[id], select[id]").forEach((el) => {
    if (el.value) saved[el.id] = el.value;
  });

  if (route === "login") viewLogin();
  else if (route === "sync") viewSyncStatus();
  else if (route === "items" && arg) viewLogScreen(arg);
  else if (route === "reports") viewReports();
  else if (route === "office") viewOffice();
  else viewItems();

  for (const [id, value] of Object.entries(saved)) {
    const el = document.getElementById(id);
    // Restore into empty fields (text inputs with no default), and into any
    // field the user had edited — even if the fresh render gave it a default,
    // like the qty box does.
    if (el && (!el.value || dirtyFields.has(id))) el.value = value;
  }
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
  // One line per sub-batch, mirroring the aging report: a split batch reads as
  // its positions, not as one blob with badges.
  const positions = item.state?.positions || [];
  const completed = item.state?.completed_qty || 0;
  const pending = pendingCountFor(item.id);

  const lines = positions.map((p) => `
    <div class="spread" style="padding:3px 0 3px 10px;border-left:2px solid var(--line)">
      <span class="muted">${p.is_unstarted ? "Not started" : `${esc(p.stage_name)} · ${esc(stateName(p.state_id))}`}${p.reworked_qty ? ` <span class="badge rework">R</span>` : ""}</span>
      <span style="white-space:nowrap">
        <span class="badge qty">${p.qty}</span>${p.is_unstarted ? "" : `
        <span class="badge${p.days_in_state >= 3 ? " age-hot" : ""}">${p.days_in_state.toFixed(1)}d</span>`}
      </span>
    </div>`).join("");

  return `
    <a href="#/items/${item.id}">
      <div style="display:flex;gap:10px;align-items:flex-start">
        ${item.icon_url ? `<img src="${item.icon_url}" alt="" loading="lazy"
          style="width:44px;height:44px;object-fit:cover;border-radius:8px;border:1px solid var(--line);flex:none">` : ""}
        <div style="flex:1;min-width:0">
          <div class="spread">
            <strong>${esc(item.code)}</strong>
            <span class="muted">${item.total_qty} pcs · rev ${esc(item.drawing_revision)}</span>
          </div>
          <div class="muted" style="margin:2px 0 6px">${esc(item.description)}</div>
        </div>
      </div>
      ${lines || `<span class="badge">not started</span>`}
      ${completed ? `
      <div class="spread" style="padding:3px 0 3px 10px;border-left:2px solid var(--good)">
        <span class="muted">Completed</span>
        <span class="badge qty" style="color:var(--good)">${completed}</span>
      </div>` : ""}
      ${pending ? `<div style="margin-top:4px"><span class="badge pending">${pending} pending</span></div>` : ""}
    </a>`;
}

function stateName(stateId) {
  return S.ref?.states.find((s) => s.id === stateId)?.name || "";
}

/* ----------------------------------------------------------- log screen -- */

const logSel = { stepId: null, stateId: null, typeId: null, stationId: null, reasonId: null };

function viewLogScreen(itemId) {
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

  // The track constrains the picker: only steps where units rest, plus the
  // immediate next step, are offered. Rework may target any step at or before
  // the furthest units. Free jumps down the route would negate the route.
  const allowed = allowedStepIds(item, steps, states, !!type?.is_rework);
  const shownSteps = steps.filter((s) => allowed.has(s.id));
  if (!shownSteps.some((s) => s.id === logSel.stepId)) {
    const target = defaultTarget(item, steps, states);
    logSel.stepId = allowed.has(target.stepId) ? target.stepId : shownSteps[0]?.id;
    logSel.stateId = target.stateId;
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
        ${item.icon_url ? `<img src="${item.icon_url}" alt=""
          style="width:52px;height:52px;object-fit:cover;border-radius:10px;border:1px solid var(--line);flex:none">` : ""}
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
      ${movableTypes.length > 1 ? `
      <label>Entry type</label>
      <div class="seg" id="seg-type">
        ${movableTypes.map((t) => `<button data-id="${t.id}" class="${t.id === logSel.typeId ? "on" : ""}">${esc(t.name)}</button>`).join("")}
      </div>` : ""}

      <label>Stage</label>
      <div class="seg" id="seg-step">
        ${shownSteps.map((s) => {
          const st = stageById(s.stage_id);
          return `<button data-id="${s.id}" class="${s.id === logSel.stepId ? "on" : ""}">${esc(st?.name || "?")}</button>`;
        }).join("")}
      </div>

      <label>State</label>
      <div class="seg" id="seg-state">
        ${states.map((s) => `<button data-id="${s.id}" class="${s.id === logSel.stateId ? "on" : ""}">${esc(s.name)}</button>`).join("")}
      </div>

      ${stage?.requires_station ? `
      <label>Station</label>
      <div class="seg" id="seg-station">
        ${stations.map((st) => `<button data-id="${st.id}" class="${st.id === logSel.stationId ? "on" : ""}">${esc(st.name)}</button>`).join("")}
      </div>` : ""}

      ${type?.requires_reason_code ? `
      <label>Reason</label>
      <select id="sel-reason">
        ${reasons.map((r) => `<option value="${r.id}" ${r.id === logSel.reasonId ? "selected" : ""}>${esc(r.name)}</option>`).join("")}
      </select>` : ""}

      <label>Quantity</label>
      <div class="qty-row">
        <button id="qty-minus">−</button>
        <input id="qty" type="number" inputmode="numeric" min="1" value="${available || 1}">
        <button id="qty-plus">+</button>
      </div>
      <p class="muted" style="margin-top:5px">${available} available at the previous step</p>
      <p class="warn-text" id="qty-warn" hidden>More than is available upstream — the server will reject this entry until the earlier steps are logged. It will wait under the sync pill with a Retry button.</p>

      ${offerAutoQueue ? `
      <label style="display:flex;align-items:center;gap:10px;margin-top:14px;font-size:15px;color:var(--text)">
        <input type="checkbox" id="auto-queue" checked style="width:22px;height:22px;flex:none">
        Also queue at ${esc(nextStage?.name || "next stage")}
      </label>` : ""}

      <div style="height:12px"></div>
      <button class="primary" id="log-go">Log entry</button>
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

  const rerender = () => viewLogScreen(itemId);
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
      note: null,
      supersedes_event_id: null,
      user_id: S.user?.id || null,
    });
    if (!logged) return;
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
        state_id: states[0].id,
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

async function loadPhotos(item) {
  const target = document.getElementById("photos");
  if (!target) return;
  try {
    const { images } = await api(`/api/items/${item.id}/images`);
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
    const { events } = await api(`/api/items/${item.id}/events?limit=8`);
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
          <span>${esc(label)}<br><span class="muted">${new Date(e.occurred_at).toLocaleString()}</span></span>
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
      <table>
        <thead><tr><th>Item</th><th>Where</th><th class="num">Qty</th><th class="num">Days</th></tr></thead>
        <tbody>
          ${aging.rows.map((row) => `
            <tr>
              <td><a href="#/items/${row.item.id}" style="color:inherit">${esc(row.item.code)}</a></td>
              <td>${esc(row.label)}${row.reworked_qty ? ` <span class="badge rework">R</span>` : ""}</td>
              <td class="num">${row.qty}</td>
              <td class="num" style="${row.days_in_state >= 3 ? "color:var(--warn)" : ""}">${row.days_in_state.toFixed(1)}</td>
            </tr>`).join("")}
        </tbody>
      </table>
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

function viewOffice() {
  const projects = S.ref?.projects || [];
  const templates = (S.ref?.route_templates || []).filter((t) => t.is_published);
  const stages = (S.ref?.stages || []).filter((s) => s.is_active);
  const unreleased = S.items.filter((i) => !i.is_released);

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
      <label>Sequence${routeDraft.length ? " — tap a step to remove it" : ""}</label>
      <div id="nr-seq">
        ${routeDraft.map((stageId, i) =>
          `<span class="badge qty" data-remove-step="${i}" style="margin:0 6px 6px 0;padding:6px 12px">${i + 1}. ${esc(stageById(stageId)?.name || "?")}</span>`
        ).join("") || `<span class="muted">Empty — a route needs at least one stage.</span>`}
      </div>
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
          <details style="margin-top:8px">
            <summary class="muted" style="cursor:pointer">Already mid-production? Distribute the ${item.total_qty} pcs</summary>
            <div data-dist-for="${item.id}" style="margin-top:6px"></div>
          </details>
        </div>`).join("") || `<p class="muted">Nothing waiting.</p>`}
    </div>

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
    document.getElementById("nr-seq").innerHTML = routeDraft.map((stageId, i) =>
      `<span class="badge qty" data-remove-step="${i}" style="margin:0 6px 6px 0;padding:6px 12px">${i + 1}. ${esc(stageById(stageId)?.name || "?")}</span>`
    ).join("") || `<span class="muted">Empty — a route needs at least one stage.</span>`;
    document.getElementById("nr-go").disabled = !S.online || !routeDraft.length;
    document.querySelectorAll("[data-remove-step]").forEach((chip) => {
      chip.onclick = () => { routeDraft.splice(Number(chip.dataset.removeStep), 1); renderSeq(); };
    });
  };
  renderSeq();

  document.querySelectorAll("[data-add-stage]").forEach((button) => {
    button.onclick = () => { routeDraft.push(button.dataset.addStage); renderSeq(); };
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

  if (S.user?.is_admin) loadManage();

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
