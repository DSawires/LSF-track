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

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ------------------------------------------------------------------ api -- */

async function api(path, options = {}) {
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
  return response.json();
}

/* ----------------------------------------------------------------- sync -- */

let syncRunning = false;

async function sync() {
  if (syncRunning) return;
  syncRunning = true;
  try {
    const queue = (await LSF_DB.queueAll()).filter((q) => q._status === "pending");
    if (queue.length) {
      const events = queue.map(({ _queued_at, _status, _reason, ...event }) => event);
      const { results } = await api("/api/events/batch", {
        method: "POST",
        body: JSON.stringify({ events }),
      });
      for (const result of results) {
        if (result.status === "stored" || result.status === "duplicate") {
          await LSF_DB.ack(result.id);
        } else {
          await LSF_DB.markRejected(result.id, result.reason || "rejected");
        }
      }
    }
    // Queue drained (or empty): refresh the offline caches.
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
    if (error.auth) {
      // Never touch the queue on a 401. The entries outlive the session.
      renderPill();
      if (!location.hash.startsWith("#/login")) navigate("#/login");
      syncRunning = false;
      return;
    }
    S.online = false; // network failed; queue stays, we try again later
  } finally {
    syncRunning = false;
  }
  S.pending = await LSF_DB.queueAll();
  renderPill();
  render(true); // background refresh; defers if the user is mid-form
}

function renderPill() {
  const pending = S.pending.filter((q) => q._status === "pending").length;
  const rejected = S.pending.filter((q) => q._status === "rejected").length;
  $pill.classList.toggle("offline", !S.online || S.needsLogin);
  $pill.classList.toggle("pending", pending > 0);
  let text;
  if (pending) text = `${pending} pending`;
  else if (S.lastSync) text = `synced ${timeAgo(S.lastSync)}`;
  else text = S.online ? "synced" : "offline";
  if (!S.online) text = `offline · ${text}`;
  if (S.needsLogin) text = "sign-in needed";
  if (rejected) text += ` · ${rejected} rejected`;
  $pillText.textContent = text;
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
setInterval(sync, 30000);
setInterval(renderPill, 60000);

/* -------------------------------------------------------------- logging -- */

async function logEvent(event) {
  await LSF_DB.enqueue(event);
  S.pending = await LSF_DB.queueAll();
  renderPill();
  toast("Logged ✓");
  sync(); // fire and forget; the queue survives if this fails
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

function render(background = false) {
  const hash = location.hash || "#/items";
  document.getElementById("topbar").hidden = false;
  document.getElementById("tabs").hidden = S.needsLogin && !S.user;

  if (S.needsLogin || (!S.user && !S.ref)) {
    if (hash !== "#/login") { navigate("#/login"); return; }
  }

  const [, route, arg] = hash.split("/");
  document.querySelectorAll("#tabs a").forEach((a) =>
    a.classList.toggle("active", a.dataset.tab === route));

  // A background sync re-renders whatever view is open. Don't let that eat a
  // half-typed form: skip the refresh entirely while a field is focused (the
  // next sync catches up), and carry non-empty field values over regardless.
  const typing = $view.contains(document.activeElement) &&
    /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName);
  if (background && typing) return;
  const saved = {};
  $view.querySelectorAll("input[id], select[id]").forEach((el) => {
    if (el.value) saved[el.id] = el.value;
  });

  if (route === "login") viewLogin();
  else if (route === "items" && arg) viewLogScreen(arg);
  else if (route === "reports") viewReports();
  else if (route === "office") viewOffice();
  else viewItems();

  for (const [id, value] of Object.entries(saved)) {
    const el = document.getElementById(id);
    if (el && !el.value) el.value = value;
  }
}

/* ---------------------------------------------------------------- login -- */

function viewLogin() {
  document.getElementById("tabs").hidden = true;
  $view.innerHTML = `
    <div class="login-wrap">
      <h1>LSF Track</h1>
      <div class="card">
        <label>Username</label>
        <input id="login-user" autocapitalize="none" autocomplete="username">
        <label>Password</label>
        <input id="login-pass" type="password" autocomplete="current-password">
        <div style="height:14px"></div>
        <button class="primary" id="login-go">Sign in</button>
        <p id="login-err" class="warn-text" hidden>Wrong username or password.</p>
      </div>
    </div>`;
  document.getElementById("login-go").onclick = async () => {
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
    } catch {
      document.getElementById("login-err").hidden = false;
    }
  };
}

/* ---------------------------------------------------------------- items -- */

function viewItems() {
  const projects = S.ref?.projects || [];
  const stages = (S.ref?.stages || []).filter((s) => s.is_active);
  const filterProject = sessionStorage.getItem("f-project") || "";
  const filterStage = sessionStorage.getItem("f-stage") || "";

  const rows = S.items.filter((item) => {
    if (!item.is_released) return false;
    if (filterProject && item.project_id !== filterProject) return false;
    if (filterStage) {
      const at = (item.state?.positions || []).some((p) => p.stage_id === filterStage);
      if (!at) return false;
    }
    return true;
  });

  $view.innerHTML = `
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
}

function itemRow(item) {
  const positions = (item.state?.positions || []).filter((p) => !p.is_unstarted);
  const worst = positions.reduce((max, p) => Math.max(max, p.days_in_state), 0);
  const pending = pendingCountFor(item.id);
  const badges = positions.slice(0, 4).map((p) =>
    `<span class="badge qty">${esc(p.stage_name)} ${p.state_code ? esc(stateName(p.state_id)) : ""}: ${p.qty}</span>`).join(" ");
  return `
    <a href="#/items/${item.id}">
      <div class="spread">
        <strong>${esc(item.code)}</strong>
        <span class="muted">${item.total_qty} pcs · rev ${esc(item.drawing_revision)}</span>
      </div>
      <div class="muted" style="margin:2px 0 6px">${esc(item.description)}</div>
      <div>
        ${badges || `<span class="badge">not started</span>`}
        ${worst >= 3 ? `<span class="badge age-hot">${worst.toFixed(1)}d</span>` : ""}
        ${pending ? `<span class="badge pending">${pending} pending</span>` : ""}
      </div>
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
  if (!steps.some((s) => s.id === logSel.stepId)) {
    const target = defaultTarget(item, steps, states);
    logSel.stepId = target.stepId;
    logSel.stateId = target.stateId;
  }
  if (!states.some((s) => s.id === logSel.stateId)) logSel.stateId = states[0]?.id;

  const type = movableTypes.find((t) => t.id === logSel.typeId);
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

  $view.innerHTML = `
    <div class="card">
      <div class="spread">
        <h2>${esc(item.code)}</h2>
        <span class="muted">${item.total_qty} pcs · rev ${esc(item.drawing_revision)}</span>
      </div>
      <div class="muted">${esc(item.description)}</div>
    </div>

    <div class="card">
      ${movableTypes.length > 1 ? `
      <label>Entry type</label>
      <div class="seg" id="seg-type">
        ${movableTypes.map((t) => `<button data-id="${t.id}" class="${t.id === logSel.typeId ? "on" : ""}">${esc(t.name)}</button>`).join("")}
      </div>` : ""}

      <label>Stage</label>
      <div class="seg" id="seg-step">
        ${steps.map((s) => {
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
      <p class="warn-text" id="qty-warn" hidden>More than is available upstream — it will be stored and flagged for review.</p>

      <div style="height:12px"></div>
      <button class="primary" id="log-go">Log entry</button>
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

  const $qty = document.getElementById("qty");
  const warn = () => {
    document.getElementById("qty-warn").hidden = Number($qty.value) <= available;
  };
  $qty.oninput = warn;
  document.getElementById("qty-minus").onclick = () => { $qty.value = Math.max(1, Number($qty.value) - 1); warn(); };
  document.getElementById("qty-plus").onclick = () => { $qty.value = Number($qty.value) + 1; warn(); };

  document.getElementById("log-go").onclick = async () => {
    const reasonSelect = document.getElementById("sel-reason");
    await logEvent({
      id: crypto.randomUUID(),
      item_id: item.id,
      item_step_id: logSel.stepId,
      station_id: stage?.requires_station ? logSel.stationId : null,
      event_type_id: logSel.typeId,
      state_id: logSel.stateId,
      qty: Number($qty.value) || 1,
      reason_code_id: reasonSelect ? reasonSelect.value : null,
      occurred_at: new Date().toISOString(),
      note: null,
      supersedes_event_id: null,
      user_id: S.user?.id || null,
    });
    rerender();
  };

  loadRecent(item);
}

function wireSeg(id, onPick) {
  const seg = document.getElementById(id);
  if (!seg) return;
  seg.querySelectorAll("button").forEach((b) => {
    b.onclick = () => onPick(b.dataset.id);
  });
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
          id: crypto.randomUUID(),
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
  $view.innerHTML = `<p class="muted">Loading reports…</p>`;
  let wip, aging, exceptions;
  try {
    [wip, aging, exceptions] = await Promise.all([
      api("/api/reports/wip"),
      api("/api/reports/aging?limit=40"),
      api("/api/reports/exceptions"),
    ]);
  } catch {
    $view.innerHTML = `<p class="muted">Reports need a connection — they are computed from the full log on the server.</p>`;
    return;
  }

  const maxQty = Math.max(1, ...wip.stages.map((s) => s.total_qty));
  const stateCodes = wip.states.map((s) => s.code);

  $view.innerHTML = `
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
          <select id="ni-project">${projects.map((p) => `<option value="${p.id}">${esc(p.code)}</option>`).join("")}</select>
        </div>
      </div>
      <label>Description</label><input id="ni-desc">
      <div class="field-grid">
        <div><label>Total qty</label><input id="ni-qty" type="number" inputmode="numeric" min="1"></div>
        <div><label>Drawing rev</label><input id="ni-rev" value="A"></div>
      </div>
      <label>Target release date</label><input id="ni-date" type="date">
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
        </div>`).join("") || `<p class="muted">Nothing waiting.</p>`}
    </div>`;

  document.getElementById("ni-go").onclick = async () => {
    const err = document.getElementById("ni-err");
    err.hidden = true;
    try {
      await api("/api/items", {
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
      toast("Item created");
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

  $view.querySelectorAll("[data-release]").forEach((button) => {
    button.onclick = async () => {
      const itemId = button.dataset.release;
      const select = $view.querySelector(`[data-route-for="${itemId}"]`);
      const template = templates.find((t) => t.id === select.value);
      if (!confirm(`Release against ${template.name} v${template.version}? The route is frozen from here.`)) return;
      try {
        await api(`/api/items/${itemId}/release`, {
          method: "POST",
          body: JSON.stringify({ route_template_id: select.value }),
        });
        toast("Released to production");
        await sync();
      } catch (error) {
        alert(error.body?.detail || "Release failed.");
      }
    };
  });
}

/* ----------------------------------------------------------------- boot -- */

async function boot() {
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
  // Come up from cache first so the app is usable before any network round trip.
  S.ref = (await LSF_DB.get("reference")) || null;
  S.items = (await LSF_DB.get("items")) || [];
  S.user = (await LSF_DB.get("user")) || null;
  S.lastSync = (await LSF_DB.get("lastSync")) || null;
  S.pending = await LSF_DB.queueAll();
  renderPill();
  render();
  sync();
}

boot();
