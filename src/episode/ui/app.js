import { API, api, apiBlob } from "./api.js?v=2";
import { notify } from "./dialogs.js?v=1";
import { $, $$, escHtml } from "./dom.js";
import { activateEpisodeWorkspace, renderEpisodeWorkspace } from "./episode-view.js?v=5";
import { fmt, fmtDuration, fmtShort, plural, titleCase, trunc } from "./format.js?v=3";
import { confirmAreaDelete, confirmDeviceDelete, openAreaEditor, openDeviceEditor } from "./inventory.js?v=3";

const LS_THEME = "episode-theme";
const PAGE_SIZES = Object.freeze({ episodes: 48, activity: 100, evidence: 60 });

function showLoading() {
  $("#view-loading").classList.remove("hidden");
  $("#view-error").classList.add("hidden");
  $("#view-content").innerHTML = "";
}
function showError(msg) {
  $("#view-loading").classList.add("hidden");
  const el = $("#view-error");
  el.classList.remove("hidden");
  el.textContent = msg;
}
function showContent(html) {
  $("#view-loading").classList.add("hidden");
  $("#view-error").classList.add("hidden");
  $("#view-content").innerHTML = html;
}

function stateBadge(s) {
  return `<span class="badge badge-${s.toLowerCase()}">${s}</span>`;
}
function eventBadge(t) {
  const cls = (t || "").toLowerCase();
  return `<span class="badge badge-${cls}">${t}</span>`;
}
function episodeTriggerBadge(triggerType) {
  if (triggerType === "doorbell") {
    return '<span class="badge badge-doorbell episode-trigger" title="Triggered by a Doorbell Event">Doorbell</span>';
  }
  if (triggerType === "motion") {
    return '<span class="badge badge-motion episode-trigger" title="Triggered by a motion Event">Motion</span>';
  }
  if (triggerType === "manual") {
    return '<span class="badge badge-manual episode-trigger" title="Triggered by a manual Event">Manual</span>';
  }
  return "";
}
function sourceBadges(sources) {
  if (!Array.isArray(sources)) return sources || "";
  return sources.map(s => `<span class="label">${s}</span>`).join(" ");
}
function toggleCollapse(el) {
  const body = el.nextElementSibling;
  el.classList.toggle("collapsed");
  body.classList.toggle("collapsed");
}

function payloadFieldLabel(key) {
  return String(key)
    .replace(/_/g, " ")
    .replace(/\b\w/g, c => c.toUpperCase());
}

function payloadFieldValue(key, value) {
  if (value === null || value === undefined || value === "") return "-";
  if (key === "sdk_command" && Number.isInteger(value)) {
    return `${value} (0x${value.toString(16).toUpperCase()})`;
  }
  if (Array.isArray(value)) return value.join(", ");
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}

function renderPayloadInterpretation(metadata) {
  const entries = Object.entries(metadata || {});
  if (!entries.length) return "";
  return `
    <div class="payload-interpretation">
      <div class="payload-interpretation-title">Interpreted payload</div>
      <div class="meta" style="margin-bottom:0.75rem">Fields decoded by the source integration. The original evidence remains unchanged.</div>
      <dl class="payload-fields">
        ${entries.map(([key, value]) => `
          <dt>${escHtml(payloadFieldLabel(key))}</dt>
          <dd>${escHtml(payloadFieldValue(key, value))}</dd>
        `).join("")}
      </dl>
    </div>`;
}

function hasEmbeddedEventPicture(metadata) {
  const descriptor = metadata?.embedded_picture;
  if (descriptor && Number.isInteger(descriptor.byte_size)) {
    return descriptor.byte_size > 0;
  }
  return metadata?.picture_transport === "binary"
    && Number.isInteger(metadata?.picture_byte_size)
    && metadata.picture_byte_size > 0;
}

function hexDump(buffer, limit = 2048) {
  const bytes = new Uint8Array(buffer);
  const shown = bytes.subarray(0, Math.min(bytes.length, limit));
  const lines = [];
  for (let offset = 0; offset < shown.length; offset += 16) {
    const row = shown.subarray(offset, Math.min(offset + 16, shown.length));
    const hex = Array.from(row, byte => byte.toString(16).padStart(2, "0")).join(" ").padEnd(47, " ");
    const ascii = Array.from(row, byte => byte >= 32 && byte <= 126 ? String.fromCharCode(byte) : ".").join("");
    lines.push(`${offset.toString(16).padStart(8, "0")}  ${hex}  |${ascii}|`);
  }
  return {
    text: lines.join("\n"),
    truncated: bytes.length > shown.length,
    shown: shown.length,
  };
}

/* ─── Theme ─── */

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  localStorage.setItem(LS_THEME, theme);
  $("#theme-toggle").textContent = theme === "light" ? "\u263e Dark" : "\u2600 Light";
}
function toggleTheme() {
  applyTheme(document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light");
}
const saved = localStorage.getItem(LS_THEME) || "dark";
applyTheme(saved);
window.toggleTheme = toggleTheme;
window.toggleCollapse = toggleCollapse;

/* ─── Router ─── */

function navigate() {
  const rawHash = location.hash.slice(1) || "episodes";
  const [hash, query = ""] = rawHash.split("?", 2);
  const params = new URLSearchParams(query);
  const requestedPage = Number.parseInt(params.get("page") || "1", 10);
  const page = Number.isFinite(requestedPage) && requestedPage > 0 ? requestedPage : 1;
  const segs = hash.split("/");
  const view = segs[0];
  const args = segs.slice(1);

  const navigationView = view === "device" ? "devices" : view;
  $$("nav a").forEach(a =>
    a.classList.toggle("active", a.getAttribute("href") === "#" + navigationView)
  );
  // Close sidebar on mobile after navigation
  const aside = document.querySelector("aside");
  if (aside.classList.contains("open")) {
    aside.classList.remove("open");
    document.getElementById("sidebar-overlay").classList.add("hidden");
    document.body.style.overflow = "";
  }

  const routes = {
    episodes, activity: events, evidence, devices, areas,
    episode, event, device: deviceView, system: systemStatus,
  };
  if (view === "evidence" && args.length && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(args[0])) {
    return evidenceDetail(args[0]);
  }
  if (view === "episode" && args.length > 1) {
    return episode(args[0]);
  }
  if (view === "episodes") return episodes(page);
  if (view === "activity") return events(args[0], page);
  if (view === "evidence") return evidence(args[0], page);
  (routes[view] || episodes)(...args);
}
window.addEventListener("hashchange", navigate);

/* ─── Views ─── */

function pageControls(base, page, itemCount, hasNext) {
  if (page === 1 && !hasNext) return "";
  const separator = base.includes("?") ? "&" : "?";
  const href = target => `${base}${separator}page=${target}`;
  return `<div class="pagination" aria-label="Page navigation">
    ${page > 1
      ? `<a class="button button-ghost" href="${href(page - 1)}">\u2190 Newer</a>`
      : '<span class="button button-ghost pagination-disabled">\u2190 Newer</span>'}
    <span class="pagination-summary">Page ${page} \u00b7 ${plural(itemCount, "item")}</span>
    ${hasNext
      ? `<a class="button button-ghost" href="${href(page + 1)}">Older \u2192</a>`
      : '<span class="button button-ghost pagination-disabled">Older \u2192</span>'}
  </div>`;
}

async function episodes(page = 1) {
  showLoading();
  try {
    const pageSize = PAGE_SIZES.episodes;
    const offset = (page - 1) * pageSize;
    const result = await api(`/episodes?limit=${pageSize + 1}&offset=${offset}`);
    const hasNext = result.length > pageSize;
    const list = result.slice(0, pageSize);
    const epIds = list.map(e => e.id).join(",");
    const covers = epIds ? await api("/covers?ids=" + encodeURIComponent(epIds)) : {};

    showContent(`
      <div class="page-header"><h2>Episodes</h2></div>
      ${list.length === 0 ? '<div class="empty">No episodes yet</div>' : `
      <div class="card-grid">
        ${list.map(e => `
          <a href="#episode/${e.id}" class="card episode-card" style="text-decoration:none;color:inherit;display:block">
            ${covers[e.id]
              ? `<div class="episode-cover"><img src="${API}/evidence/${covers[e.id]}/file" loading="lazy" alt=""></div>`
              : `<div class="episode-cover episode-cover-placeholder"><img src="/logo.svg" alt=""><span>No snapshot captured</span></div>`}
            <div class="episode-card-body">
              <div class="episode-card-heading">
                <h3>${trunc(e.primary_area_id || "?", 24)}</h3>
                <div class="episode-card-badges">
                  ${episodeTriggerBadge(e.trigger_type)}
                  ${stateBadge(e.state)}
                </div>
              </div>
              <div class="meta">
                <span>${plural(e.event_count, "event")}</span>
                <span>${plural(e.evidence_count, "evidence")}</span>
                <span>${fmtDuration(e.start_time, e.end_time || e.last_event_time)}</span><br>
                <span>${fmtShort(e.start_time)}</span>
                ${e.last_event_time ? `\u2192 ${fmtShort(e.last_event_time)}` : ""}
              </div>
            </div>
          </a>
        `).join("")}
      </div>`}
      ${pageControls("#episodes", page, list.length, hasNext)}
    `);
    updateRecentEpisodes(page === 1 ? list : null);
  } catch (e) { showError(e.message); }
}

async function episode(id) {
  showLoading();
  try {
    const ep = await api("/episodes/" + id);
    const [events, evidence] = await Promise.all([
      api("/episodes/" + id + "/events"),
      api("/episodes/" + id + "/evidence"),
    ]);
    const dur = fmtDuration(ep.start_time, ep.end_time || ep.last_event_time);
    const timelapseDevices = ep.state === "closed"
      ? [...new Set(evidence
        .filter(item => item.evidence_type === "snapshot"
          && item.metadata?.timelapse_eligible !== false)
        .map(item => item.device_id)
        .filter(Boolean))]
      : [];
    const workspace = renderEpisodeWorkspace(ep, events, evidence, timelapseDevices);
    const chronologicalEvents = [...events]
      .sort((left, right) => new Date(left.timestamp) - new Date(right.timestamp));

    showContent(`
      <div class="breadcrumbs"><a href="#episodes">Episodes</a> <span class="sep">›</span> <span>${trunc(ep.primary_area_id || ep.id, 40)}</span></div>
      <div class="detail-header episode-detail-header">
        <div class="eyebrow">Episode</div>
        <h2>${trunc(ep.primary_area_id || "?", 40)} ${stateBadge(ep.state)} ${dur ? `<span class="badge badge-duration">${dur}</span>` : ""}</h2>
        <div class="subtitle">
          ${ep.id} &middot; ${plural(ep.event_count, "event")}, ${plural(ep.evidence_count, "evidence")}
          &middot; ${fmt(ep.start_time)} ${ep.last_event_time ? `\u2192 ${fmt(ep.last_event_time)}` : ""}
        </div>
      </div>

      ${workspace.html}

      <div class="section episode-secondary">
        <div class="collapse-header collapsed" onclick="toggleCollapse(this)">
          <h3>All evidence (${evidence.length})</h3>
        </div>
        <div class="collapse-body collapsed">
          ${renderEpisodeEvidence(evidence)}
        </div>
      </div>

      <div class="section episode-secondary">
        <div class="collapse-header collapsed" onclick="toggleCollapse(this)">
          <h3>Raw activity (${events.length})</h3>
        </div>
        <div class="collapse-body collapsed">
          ${events.length === 0 ? '<div class="empty">No activity</div>' : `
          <div class="table-wrap"><table>
            <thead><tr><th>Type</th><th>Device</th><th>State</th><th>Time</th><th>Source</th></tr></thead>
            <tbody>
              ${chronologicalEvents.map(event => `
              <tr class="clickable" onclick="location='#event/${event.id}'">
                <td>${eventBadge(event.event_type)}</td>
                <td>${trunc(event.device_id, 20)}</td>
                <td>${stateBadge(event.event_state)}</td>
                <td>${fmtShort(event.timestamp)}</td>
                <td>${sourceBadges(event.sources)}</td>
              </tr>
              `).join("")}
            </tbody>
          </table></div>`}
        </div>
      </div>
    `);
    activateEpisodeWorkspace(workspace.model, ep);
  } catch (e) { showError(e.message); }
}

async function events(device_id, page = 1) {
  showLoading();
  try {
    const devices = await api("/devices");
    const pageSize = PAGE_SIZES.activity;
    const offset = (page - 1) * pageSize;
    const query = new URLSearchParams({ limit: pageSize + 1, offset });
    if (device_id) query.set("device_id", device_id);
    const result = await api(`/events?${query}`);
    const hasNext = result.length > pageSize;
    const list = result.slice(0, pageSize);
    const base = `#activity${device_id ? `/${encodeURIComponent(device_id)}` : ""}`;
    showContent(`
      <div class="page-header">
        <h2>Activity</h2>
        <div class="filters">
          <select onchange="navigateEventFilter(this.value)">
            <option value="">All devices</option>
            ${devices.map(s => `<option value="${s.id}" ${s.id === device_id ? "selected" : ""}>${s.name || s.id}</option>`).join("")}
          </select>
        </div>
      </div>
      ${list.length === 0 ? '<div class="empty">No activity</div>' : `
      <table>
        <thead><tr><th>Type</th><th>Device</th><th>Area</th><th>State</th><th>Source</th><th>Time</th><th>Episode</th></tr></thead>
        <tbody>
          ${list.map(e => `
            <tr class="clickable" onclick="location='#event/${e.id}'">
              <td>${eventBadge(e.event_type)}</td>
              <td>${trunc(e.device_id, 16)}</td>
              <td>${trunc(e.area_id, 16)}</td>
              <td>${stateBadge(e.event_state)}</td>
              <td>${sourceBadges(e.sources)}</td>
              <td>${fmtShort(e.timestamp)}</td>
              <td>${e.episode_id ? `<a href="#episode/${e.episode_id}" onclick="event.stopPropagation()">${trunc(e.episode_id, 12)}</a>` : "-"}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>`}
      ${pageControls(base, page, list.length, hasNext)}
    `);
  } catch (e) { showError(e.message); }
}
window.navigateEventFilter = v => { location = "#activity" + (v ? "/" + v : ""); };

async function event(id) {
  showLoading();
  try {
    const ev = await api("/events/" + id);
    const [nearbyEvidence, closestData] = await Promise.all([
      ev.episode_id ? api("/episodes/" + ev.episode_id + "/evidence") : Promise.resolve([]),
      ev.episode_id ? api("/events/" + id + "/closest-snapshot").catch(() => null) : Promise.resolve(null),
    ]);
    const related = nearbyEvidence.filter(e =>
      e.device_id === ev.device_id && e.evidence_type !== "payload"
    ).sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));

    let snapHtml = "";
    let targetBadge = "";
    if (closestData && closestData.snapshot && closestData.bounding_box) {
      const snap = closestData.snapshot;
      const bb = closestData.bounding_box;
      const tt = closestData.target_type;
      if (tt) targetBadge = `<span class="badge" style="background:var(--surface-elevated);color:var(--text-secondary);text-transform:none;margin-left:0.35rem">${tt}</span>`;
      snapHtml = `
      <div class="section">
        <h3>Closest Snapshot <label style="font-weight:400;font-size:0.85rem;margin-left:0.5rem"><input type="checkbox" checked onchange="document.getElementById('bbox-overlay').style.display=this.checked?'block':'none'"> Bounding box</label></h3>
        <div style="position:relative;display:inline-block;max-width:100%">
          <img src="${API}/evidence/${snap.id}/file" style="max-width:100%;max-height:60vh;border-radius:var(--radius);display:block">
          <svg id="bbox-overlay" style="position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none" viewBox="0 0 1 1" preserveAspectRatio="none">
            <rect x="${bb.x}" y="${bb.y}" width="${bb.width}" height="${bb.height}"
                  fill="none" stroke="#00C2C7" stroke-width="0.008" stroke-linecap="round">
              <animate attributeName="stroke-opacity" values="1;0.4;1" dur="2s" repeatCount="indefinite"/>
            </rect>
          </svg>
        </div>
        <div class="meta" style="margin-top:0.5rem">${fmtShort(snap.timestamp)} &middot; <a href="#evidence/${snap.id}" style="color:var(--accent)">View evidence</a></div>
      </div>`;
    }

    const lockName = String(ev.metadata?.lock_name || "").trim();
    const eventPictureHtml = ev.has_raw_payload && hasEmbeddedEventPicture(ev.metadata) ? `
      <div class="section">
        <h3>Event picture</h3>
        <div class="event-embedded-picture">
          <img src="${API}/events/${encodeURIComponent(ev.id)}/picture"
               alt="${escHtml(lockName ? `Unlock record for ${lockName}` : "Door unlock record")}">
        </div>
        <div class="meta" style="margin-top:0.5rem">
          Image embedded in the original vendor callback. The raw payload remains unchanged.
        </div>
      </div>` : "";

    showContent(`
      <div class="breadcrumbs"><a href="#activity">Activity</a> <span class="sep">›</span> <span>${eventBadge(ev.event_type)}</span></div>
      <div class="detail-header">
        <h2>${eventBadge(ev.event_type)} ${stateBadge(ev.event_state)}${targetBadge}</h2>
        <div class="subtitle">
          ${ev.id} &middot; ${sourceBadges(ev.sources)}
          &middot; ${fmt(ev.timestamp)}
          ${ev.episode_id ? `&middot; <a href="#episode/${ev.episode_id}" style="color:var(--accent)">Episode ${trunc(ev.episode_id, 12)}</a>` : ""}
        </div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.75rem;margin-bottom:1.5rem">
        <div class="card"><div class="meta"><strong>Device</strong><br>${ev.device_id}</div></div>
        <div class="card"><div class="meta"><strong>Area</strong><br>${ev.area_id}</div></div>
      </div>
      ${eventPictureHtml}
      ${snapHtml}
      ${ev.has_raw_payload ? `
        <div class="section">
          <h3>Raw Payload</h3>
          <div class="meta" style="margin-bottom:0.5rem">Original vendor payload preserved by Episode.</div>
          ${renderPayloadInterpretation(ev.metadata)}
          <button class="btn-back" onclick="loadPayload('${ev.id}', this)" style="font-size:0.85rem">\u25b6 Show payload content</button>
          <div id="payload-${ev.id}" class="hidden"></div>
        </div>` : ""}
      <div class="section">
        <h3>Related Evidence (${related.length})</h3>
        <div class="meta" style="margin-bottom:0.5rem">Evidence from device <strong>${ev.device_id}</strong> in the same episode</div>
        ${related.length === 0 ? '<div class="empty">No evidence linked to this event yet</div>' : (_carouselItems=related, renderEvidenceGrid(related))}
      </div>
    `);
  } catch (e) { showError(e.message); }
}

window.loadPayload = async (eventId, btn) => {
  btn.style.display = "none";
  const el = $("#payload-" + eventId);
  el.classList.remove("hidden");
  el.innerHTML = '<div class="meta">Loading...</div>';
  try {
    const r = await fetch(API + "/events/" + eventId + "/payload");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const contentType = (r.headers.get("content-type") || "").split(";", 1)[0];
    if (contentType === "application/octet-stream") {
      const blob = await r.blob();
      const dump = hexDump(await blob.arrayBuffer());
      el.innerHTML = `
        <div class="meta" style="margin-bottom:0.5rem">
          Binary vendor callback · ${blob.size} bytes · ${dump.shown} bytes shown${dump.truncated ? " (preview truncated)" : ""}
          · <a href="${API}/events/${eventId}/payload" download style="color:var(--accent)">Download original</a>
        </div>
        <pre class="payload-xml payload-binary">${escHtml(dump.text)}</pre>`;
      return;
    }
    const text = await r.text();
    let displayed = text;
    if (contentType === "application/json") {
      try { displayed = JSON.stringify(JSON.parse(text), null, 2); } catch (_) { /* Preserve invalid JSON verbatim. */ }
    }
    el.innerHTML = `
      <div class="meta" style="margin-bottom:0.5rem"><a href="${API}/events/${eventId}/payload" download style="color:var(--accent)">Download original</a></div>
      <pre class="payload-xml">${escHtml(displayed)}</pre>`;
  } catch (e) {
    el.innerHTML = `<div class="meta" style="color:var(--danger)">Failed to load: ${e.message}</div>`;
  }
};

async function evidence(device_id, page = 1) {
  showLoading();
  try {
    const pageSize = PAGE_SIZES.evidence;
    const offset = (page - 1) * pageSize;
    const query = new URLSearchParams({ limit: pageSize + 1, offset });
    if (device_id) query.set("device_id", device_id);
    const result = await api(`/evidence?${query}`);
    const hasNext = result.length > pageSize;
    const list = result.slice(0, pageSize);
    const base = `#evidence${device_id ? `/${encodeURIComponent(device_id)}` : ""}`;
    showContent(`
      <div class="page-header"><h2>Evidence</h2></div>
      ${list.length === 0 ? '<div class="empty">No evidence</div>' : (_carouselItems=list, renderEvidenceGrid(list))}
      ${pageControls(base, page, list.length, hasNext)}
    `);
  } catch (e) { showError(e.message); }
}

async function evidenceDetail(id) {
  showLoading();
  try {
    const ev = await api("/evidence/" + id);
    const isVideo = ev.mime_type?.startsWith("video/");
    const isImage = ev.mime_type?.startsWith("image/");
    const isText = ev.mime_type?.startsWith("text/") || ev.mime_type === "application/xml";

    let mediaHtml = "";
    if (isVideo) {
      mediaHtml = `<video src="${API}/evidence/${ev.id}/file" controls style="width:100%;max-height:70vh;border-radius:var(--radius);background:#000"></video>`;
    } else if (isImage) {
      mediaHtml = `<img src="${API}/evidence/${ev.id}/file" style="width:100%;max-height:70vh;object-fit:contain;border-radius:var(--radius);background:#000">`;
    } else if (isText) {
      try {
        const blob = await apiBlob("/evidence/" + ev.id + "/file");
        const text = await blob.text();
        mediaHtml = `<pre class="payload-xml">${escHtml(text)}</pre>`;
      } catch { }
    }

    // Find closest event with bounding box
    let bboxHtml = "";
    if (isImage && ev.episode_id) {
      try {
        const closeData = await api("/evidence/" + id + "/closest-event");
        if (closeData && closeData.bounding_box) {
          const bb = closeData.bounding_box;
          const tt = closeData.target_type;
          const ce = closeData.event;
          const diffMs = new Date(ev.timestamp) - new Date(ce.timestamp);
          const diffStr = diffMs >= 0 ? `+${(diffMs / 1000).toFixed(1)}s` : "";
          bboxHtml = `
          <div class="section">
            <h3>Linked event ${tt ? `<span class="badge" style="background:var(--surface-elevated);color:var(--text-secondary);text-transform:none">${tt}</span>` : ""} <label style="font-weight:400;font-size:0.85rem;margin-left:0.5rem"><input type="checkbox" checked onchange="document.getElementById('ev-bbox-overlay').style.display=this.checked?'block':'none'"> Bounding box</label></h3>
            <div style="position:relative;display:inline-block;max-width:100%">
              <img src="${API}/evidence/${ev.id}/file" style="max-width:100%;max-height:60vh;border-radius:var(--radius);display:block">
              <svg id="ev-bbox-overlay" style="position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none" viewBox="0 0 1 1" preserveAspectRatio="none">
                <rect x="${bb.x}" y="${bb.y}" width="${bb.width}" height="${bb.height}"
                      fill="none" stroke="#00C2C7" stroke-width="0.008" stroke-linecap="round">
                  <animate attributeName="stroke-opacity" values="1;0.4;1" dur="2s" repeatCount="indefinite"/>
                </rect>
              </svg>
            </div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.75rem;margin-top:0.75rem">
              <div class="card"><div class="meta"><strong>Type</strong><br>${eventBadge(ce.event_type)} ${stateBadge(ce.event_state)}</div></div>
              <div class="card"><div class="meta"><strong>Source</strong><br>${sourceBadges(ce.sources)}</div></div>
              <div class="card"><div class="meta"><strong>Device</strong><br>${ce.device_id}</div></div>
              <div class="card"><div class="meta"><strong>Timestamp</strong><br>${fmt(ce.timestamp)} <span class="label" style="color:var(--accent)">${diffStr}</span></div></div>
            </div>
            ${ce.raw_payload_path ? `
            <button class="btn-back" onclick="loadPayload('${ce.id}', this)" style="margin-top:0.75rem;font-size:0.85rem">\u25b6 Show payload</button>
            <div id="payload-${ce.id}" class="hidden" style="margin-top:0.5rem"></div>` : ""}
            <div style="margin-top:0.5rem"><a href="#event/${ce.id}" style="color:var(--accent)">\u2192 Full activity details</a></div>
          </div>`;
        }
      } catch {}
    }

    const crumbs = [];
    crumbs.push(`<a href="#evidence">Evidence</a>`);
    if (ev.event_id) crumbs.push(`<a href="#event/${ev.event_id}">Activity</a>`);
    if (ev.episode_id) crumbs.push(`<a href="#episode/${ev.episode_id}">Episode</a>`);
    crumbs.push(`<span>${trunc(ev.evidence_type, 24)}</span>`);

    showContent(`
      <div class="breadcrumbs">${crumbs.map((c, i) => `${i > 0 ? ' <span class="sep">›</span> ' : ''}${c}`).join("")}</div>
      <div class="detail-header">
        <h2>${ev.evidence_type} ${originBadge(ev)}</h2>
        <div class="subtitle">${ev.id} &middot; ${ev.mime_type} &middot; ${fmt(ev.timestamp)}</div>
      </div>
      ${bboxHtml || (mediaHtml ? `<div class="section">${mediaHtml}</div>` : "")}
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.75rem;margin-top:1rem">
        <div class="card"><div class="meta"><strong>Device</strong><br>${ev.device_id}</div></div>
        <div class="card"><div class="meta"><strong>Area</strong><br>${ev.area_id}</div></div>
        <div class="card"><div class="meta"><strong>Original file</strong><br>${ev.original_filename || "-"}</div></div>
        <div class="card"><div class="meta"><strong>Stored at</strong><br>${ev.file_path}</div></div>
      </div>
    `);
  } catch (e) { showError(e.message); }
}

/* ─── Episode evidence (flat by type, no device grouping) ─── */

function renderEpisodeEvidence(list) {
  const sorted = [...list].sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
  const recordings = sorted.filter(e => e.evidence_type === "recording");
  const snapshots = sorted.filter(e => e.evidence_type === "snapshot");
  const other = sorted.filter(e => e.evidence_type !== "recording" && e.evidence_type !== "snapshot");
  if (sorted.length === 0) return '<div class="empty">No evidence</div>';
  _carouselItems = sorted;
  const idxOf = ev => sorted.indexOf(ev);
  let html = "";
  if (recordings.length) {
    html += `<div class="section-sub"><div class="label" style="margin-bottom:0.25rem">Recordings (${recordings.length})</div><div class="evidence-grid">${recordings.map(ev => renderEvidenceItem(ev, idxOf(ev))).join("")}</div></div>`;
  }
  if (snapshots.length) {
    html += `<div class="section-sub"><div class="label" style="margin-bottom:0.25rem">Snapshots (${snapshots.length})</div><div class="evidence-grid">${snapshots.map(ev => renderEvidenceItem(ev, idxOf(ev))).join("")}</div></div>`;
  }
  if (other.length) {
    html += `<div class="section-sub"><div class="evidence-grid">${other.map(ev => renderEvidenceItem(ev, idxOf(ev))).join("")}</div></div>`;
  }
  return html;
}

function fmtDurationShort(seconds) {
  if (!seconds && seconds !== 0) return "";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  const pad = n => String(n).padStart(2, "0");
  if (h > 0) return `${h}:${pad(m)}:${pad(s)}`;
  if (m > 0) return `${m}:${pad(s)}`;
  return `${s}s`;
}

function renderEvidenceItem(ev, i) {
  const isVideo = ev.mime_type?.startsWith("video/");
  const isImage = ev.mime_type?.startsWith("image/");
  const dur = ev.metadata?.duration_seconds;
  const label = ev.evidence_type === "recording" ? (ev.device_id || "recording") : ev.evidence_type;
  return `<div class="evidence-item" tabindex="0" role="button" onclick="showCarousel(null, ${i})" onkeydown="if(event.key==='Enter')showCarousel(null, ${i})">
    ${isVideo ? `<video src="${API}/evidence/${ev.id}/file" preload="metadata"></video>` : ""}
    ${isImage ? `<img src="${API}/evidence/${ev.id}/file" loading="lazy">` : ""}
    ${!isVideo && !isImage ? `<div style="padding:2rem;text-align:center;color:var(--text-muted)">${ev.evidence_type}<br>${ev.mime_type}</div>` : ""}
    <div class="info">
      <div class="label">${originBadge(ev)} <strong>${label}</strong> ${fmtShort(ev.timestamp)} ${dur ? fmtDurationShort(dur) : ""}</div>
      <div class="label"><a href="#evidence/${ev.id}" style="color:var(--accent)" onclick="event.stopPropagation()">Details</a></div>
    </div>
  </div>`;
}

/* ─── Evidence grid renderer ─── */

function originBadge(ev) {
  const origin = (ev.metadata && ev.metadata.origin) || ev.evidence_type;
  const cls = origin === "isapi" ? "badge-isapi"
    : origin === "alarm_server" ? "badge-alarm"
    : origin === "ftp" ? "badge-ftp"
    : origin === "recording" ? "badge-recording"
    : "badge-payload";
  return `<span class="badge ${cls}">${origin}</span>`;
}

/* ─── Evidence carousel ─── */

let _carouselItems = [];
let _carouselIdx = 0;

window.showCarousel = function(items, idx) {
  if (Array.isArray(items)) {
    _carouselItems = items.filter(ev => ev.evidence_type !== "payload");
  }
  _carouselIdx = idx;
  _renderCarousel();
  const el = $("#evidence-carousel");
  el.classList.remove("hidden");
  document.body.style.overflow = "hidden";
};

window.closeCarousel = function() {
  $("#evidence-carousel").classList.add("hidden");
  document.body.style.overflow = "";
  const slide = $("#carousel-slide");
  for (const el of slide.querySelectorAll("video, audio")) {
    el.pause();
    el.removeAttribute("src");
    el.load();
  }
};

window.carouselNav = function(delta) {
  _carouselIdx = (_carouselIdx + delta + _carouselItems.length) % _carouselItems.length;
  _renderCarousel();
};

let _bboxReq = 0;

function _renderCarousel() {
  const ev = _carouselItems[_carouselIdx];
  if (!ev) return;

  const slide = $("#carousel-slide");
  for (const el of slide.querySelectorAll("video, audio")) {
    el.pause();
    el.removeAttribute("src");
    el.load();
  }

  const isVideo = ev.mime_type?.startsWith("video/");
  const isImage = ev.mime_type?.startsWith("image/");

  let mediaHtml = "";
  if (isVideo) {
    mediaHtml = `<video src="${API}/evidence/${ev.id}/file" controls autoplay style="max-width:100%;max-height:80vh;border-radius:var(--radius)"></video>`;
  } else if (isImage) {
    mediaHtml = `<div style="position:relative;display:inline-block;max-width:100%;max-height:80vh">
      <img src="${API}/evidence/${ev.id}/file" style="max-width:100%;max-height:80vh;object-fit:contain;border-radius:var(--radius);display:block">
      <svg id="carousel-bbox" style="position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;display:none" viewBox="0 0 1 1" preserveAspectRatio="none">
        <rect id="carousel-bbox-rect" x="0" y="0" width="0" height="0" fill="none" stroke="#00C2C7" stroke-width="0.008" stroke-linecap="round">
          <animate attributeName="stroke-opacity" values="1;0.4;1" dur="2s" repeatCount="indefinite"/>
        </rect>
      </svg>
    </div>`;
  } else {
    mediaHtml = `<div style="padding:2rem;color:var(--text-muted);text-align:center">${ev.evidence_type}<br>${ev.mime_type || ""}</div>`;
  }

  $("#carousel-slide").innerHTML = mediaHtml;
  $("#carousel-counter").textContent = `${_carouselIdx + 1} / ${_carouselItems.length}`;

  const infoEl = $("#carousel-info");
  infoEl.innerHTML = `
    ${originBadge(ev)} <strong>${ev.evidence_type}</strong>
    &middot; ${fmtShort(ev.timestamp)}
    &middot; ${trunc(ev.device_id, 20)}
    <a href="#evidence/${ev.id}" style="color:var(--accent)" onclick="closeCarousel()">Details</a>
    ${ev.event_id ? `&middot; <a href="#event/${ev.event_id}" style="color:var(--accent)" onclick="closeCarousel()">Activity</a>` : ""}
    ${ev.episode_id ? `&middot; <a href="#episode/${ev.episode_id}" style="color:var(--accent)" onclick="closeCarousel()">Episode</a>` : ""}
  `;

  // Fetch bounding box for image evidence
  if (isImage && ev.episode_id) {
    const reqId = ++_bboxReq;
    api("/evidence/" + ev.id + "/closest-event")
      .then(closeData => {
        if (reqId !== _bboxReq || !closeData || !closeData.bounding_box) return;
        const rect = document.getElementById("carousel-bbox-rect");
        if (rect) {
          rect.setAttribute("x", closeData.bounding_box.x);
          rect.setAttribute("y", closeData.bounding_box.y);
          rect.setAttribute("width", closeData.bounding_box.width);
          rect.setAttribute("height", closeData.bounding_box.height);
          document.getElementById("carousel-bbox").style.display = "block";
        }
        if (closeData.target_type) {
          infoEl.insertAdjacentHTML("afterbegin", `<span class="badge" style="background:var(--surface-elevated);color:var(--text-secondary);text-transform:none">${closeData.target_type}</span> `);
        }
      })
      .catch(() => {});
  }
}

// Keyboard support for carousel
document.addEventListener("keydown", e => {
  if ($("#evidence-carousel")?.classList.contains("hidden")) return;
  if (e.key === "Escape") closeCarousel();
  if (e.key === "ArrowLeft") carouselNav(-1);
  if (e.key === "ArrowRight") carouselNav(1);
});

/* ─── Evidence grid renderer ─── */

function renderEvidenceGrid(list) {
  const items = list.filter(ev => ev.evidence_type !== "payload");
  if (items.length === 0) return '<div class="empty">No evidence</div>';
  _carouselItems = items;
  return `<div class="evidence-grid">
    ${items.map((ev, i) => renderEvidenceItem(ev, i)).join("")}
  </div>`;
}

/* ─── Devices & Areas ─── */

let inventoryAreas = [];
let inventoryDevices = [];

function operationalIndicator(state) {
  if (state === "healthy") return "online";
  if (state === "degraded") return "warning";
  if (state === "disabled" || state === "unknown") return "idle";
  return "offline";
}

function operationalBadge(state) {
  return `<span class="badge badge-${state}">${titleCase(state)}</span>`;
}

function capabilityBadges(capabilities) {
  return (capabilities || [])
    .filter(capability => capability !== "events")
    .map(capability => `<span class="badge badge-neutral">${titleCase(capability)}</span>`)
    .join(" ");
}

function integrationBadges(integrations) {
  return (integrations || [])
    .map(integration => `<span class="badge badge-${integration.state}">${titleCase(integration.type)}</span>`)
    .join(" ");
}

function restartBanner(status) {
  if (!status?.restart_required) return "";
  return `<div class="notice notice-warning">
    <div><strong>Restart required</strong><span>Device changes are saved. Restart Episode to apply integration connections.</span></div>
    <code>docker compose restart episode</code>
  </div>`;
}

function renderIntegrationRows(integrations, showDetails = false) {
  if (!integrations.length) return '<div class="empty">No integrations configured</div>';
  return `<div class="resource-list">
    ${integrations.map(integration => {
      const detailEntries = Object.entries(integration.details || {})
        .filter(([, value]) => value !== null && value !== "" && (!Array.isArray(value) || value.length));
      return `<div class="resource-row">
        <span class="status-indicator ${operationalIndicator(integration.state)}"></span>
        <div class="resource-main">
          <strong>${integration.name}</strong>
          <span>${integration.summary || titleCase(integration.type)}</span>
          <div class="badge-cluster">${capabilityBadges(integration.capabilities)}</div>
        </div>
        ${operationalBadge(integration.state)}
        ${showDetails && detailEntries.length ? `
          <details class="diagnostic-details">
            <summary>Technical details</summary>
            <pre>${escHtml(JSON.stringify(integration.details, null, 2))}</pre>
          </details>` : ""}
      </div>`;
    }).join("")}
  </div>`;
}

window.addArea = () => openAreaEditor(null, areas);
window.editArea = id => {
  const area = inventoryAreas.find(candidate => candidate.id === id);
  if (area) openAreaEditor(area, areas);
};
window.deleteArea = id => {
  const area = inventoryAreas.find(candidate => candidate.id === id);
  if (area) confirmAreaDelete(area, areas);
};
window.addDevice = () => {
  const activeAreas = inventoryAreas.filter(area => area.enabled);
  if (!activeAreas.length) {
    notify("Create an active Area before adding a Device", "warning");
    openAreaEditor(null, devices);
    return;
  }
  openDeviceEditor(null, inventoryAreas, devices);
};
window.editDevice = async id => {
  try {
    const device = await api("/devices/" + encodeURIComponent(id));
    inventoryDevices = [
      ...inventoryDevices.filter(candidate => candidate.id !== id),
      device,
    ];
    openDeviceEditor(
      device,
      inventoryAreas,
      () => location.hash.startsWith("#device/") ? deviceView(id) : devices(),
    );
  } catch (error) {
    notify(`Could not load Device configuration: ${error.message}`, "warning");
  }
};
window.deleteDevice = id => {
  const device = inventoryDevices.find(candidate => candidate.id === id);
  if (device) confirmDeviceDelete(device, async () => {
    location.hash = "devices";
    await devices();
  });
};

async function devices() {
  showLoading();
  try {
    const [list, areaList, status] = await Promise.all([
      api("/devices?include_disabled=true"),
      api("/areas?include_disabled=true"),
      api("/status"),
    ]);
    inventoryDevices = list;
    inventoryAreas = areaList;
    const areaNames = Object.fromEntries(areaList.map(area => [area.id, area.name]));
    showContent(`
      <div class="page-header">
        <div>
          <div class="eyebrow">Inventory</div>
          <h2>Devices</h2>
          <div class="meta">Equipment, capture behavior, and source integrations.</div>
        </div>
        <div class="page-actions">
          <a href="#areas" class="button button-ghost">Manage Areas</a>
          <button class="button button-primary" onclick="addDevice()">Add Device</button>
        </div>
      </div>
      ${restartBanner(status)}
      ${list.length === 0 ? `<div class="empty-state">
        <div class="empty-icon">◎</div>
        <h3>Add your first Device</h3>
        <p>Connect a camera, doorbell, or another event source to an Area.</p>
        <button class="button button-primary" onclick="addDevice()">Add Device</button>
      </div>` : `
      <div class="resource-list inventory-list">
        ${list.map(device => {
          const identity = device.identity || {};
          return `<div class="resource-row inventory-row ${device.enabled ? "" : "resource-disabled"}">
            <span class="status-indicator ${operationalIndicator(device.state)}"></span>
            <a href="#device/${device.id}" class="resource-main resource-primary-link">
              <strong>${device.name || device.id}</strong>
              <span>${titleCase(device.device_type)} · ${[identity.manufacturer, identity.model].filter(Boolean).join(" ") || "Manufacturer not detected"}</span>
            </a>
            <div class="resource-context">${areaNames[device.area_id] || device.area_id || "No Area"}</div>
            <div class="resource-badges">${integrationBadges(device.integrations) || '<span class="meta">No integrations</span>'}</div>
            <div class="resource-actions">
              <button class="icon-button" onclick="editDevice('${device.id}')" aria-label="Edit ${device.name}">Edit</button>
              <button class="icon-button danger-text" onclick="deleteDevice('${device.id}')" aria-label="Delete ${device.name}">Delete</button>
            </div>
          </div>`;
        }).join("")}
      </div>`}
    `);
  } catch (error) { showError(error.message); }
}

async function deviceView(id) {
  showLoading();
  try {
    const [item, areaList, activity, evidenceList, status] = await Promise.all([
      api("/devices/" + encodeURIComponent(id)),
      api("/areas?include_disabled=true"),
      api("/events?device_id=" + encodeURIComponent(id) + "&limit=12"),
      api("/evidence?device_id=" + encodeURIComponent(id) + "&limit=24"),
      api("/status"),
    ]);
    inventoryAreas = areaList;
    inventoryDevices = [item];
    const area = areaList.find(candidate => candidate.id === item.area_id);
    const identity = item.identity || {};
    const origins = [...new Set(evidenceList.map(entry => entry.metadata?.origin).filter(Boolean))];
    const onvif = item.integrations.find(integration => integration.type === "onvif");
    const profiles = onvif?.details?.profiles || [];
    const selectedProfile = onvif?.details?.selected_profile || "";
    const topics = onvif?.details?.event_topics || [];

    showContent(`
      <div class="breadcrumbs"><a href="#devices">Devices</a> <span class="sep">›</span> <span>${item.name || item.id}</span></div>
      <div class="detail-header device-detail-header">
        <div>
          <div class="device-title">
            <span class="status-indicator ${operationalIndicator(item.state)}"></span>
            <h2>${item.name || item.id}</h2>
            ${operationalBadge(item.state)}
          </div>
          <div class="subtitle">${titleCase(item.device_type)} · ${[identity.manufacturer, identity.model].filter(Boolean).join(" ") || "Manufacturer not detected"}</div>
        </div>
        <div class="page-actions">
          <button class="button button-ghost" onclick="editDevice('${item.id}')">Edit Device</button>
          <button class="button button-ghost danger-text" onclick="deleteDevice('${item.id}')">Delete</button>
        </div>
      </div>
      ${restartBanner(status)}

      <dl class="detail-facts section">
        <div><dt>Type</dt><dd>${titleCase(item.device_type)}</dd></div>
        <div><dt>Area</dt><dd>${area?.name || item.area_id || "Not assigned"}</dd></div>
        <div><dt>Manufacturer</dt><dd>${identity.manufacturer || "Not detected"}</dd></div>
        <div><dt>Model</dt><dd>${identity.model || "Not detected"}</dd></div>
        <div><dt>Firmware</dt><dd>${identity.firmware_version || "Not reported"}</dd></div>
        <div><dt>Network address</dt><dd>${item.ip_address || "Not configured"}</dd></div>
        <div><dt>Recording behavior</dt><dd>${titleCase(item.capture_policy.recording)}</dd></div>
        <div><dt>Automatic snapshots</dt><dd>${item.capture_policy.automatic_snapshots ? "Enabled" : "Disabled"}</dd></div>
        <div><dt>ONVIF Events</dt><dd>${item.capture_policy.onvif_events === null ? "Unavailable" : item.capture_policy.onvif_events ? "Enabled" : "Disabled"}</dd></div>
      </dl>

      <div class="section">
        <div class="section-heading"><div><h3>Capabilities</h3><span>What this Device can contribute</span></div></div>
        <div class="compact-panel badge-cluster">${capabilityBadges(item.capabilities) || '<span class="meta">None reported</span>'}</div>
      </div>

      <div class="section">
        <div class="section-heading"><div><h3>Integrations</h3><span>${plural(item.integrations.length, "configured connection")}</span></div></div>
        ${renderIntegrationRows(item.integrations)}
      </div>

      ${profiles.length ? `
      <div class="section">
        <div class="section-heading"><div><h3>ONVIF media</h3><span>Runtime discovery · read-only</span></div></div>
        <div class="resource-list">
          ${profiles.map(profile => `
            <div class="resource-row">
              <div class="resource-main">
                <strong>${profile.name || profile.token}</strong>
                <span>${profile.width} × ${profile.height} · ${profile.encoding || "Unknown codec"} · Snapshot ${profile.snapshot ? "available" : "unavailable"}</span>
              </div>
              <span class="badge ${profile.token === selectedProfile ? "badge-active" : "badge-neutral"}">${profile.token === selectedProfile ? "Selected" : "Discovered"} · read-only</span>
            </div>`).join("")}
        </div>
      </div>` : ""}

      <div class="section">
        <div class="section-heading"><div><h3>Observed evidence</h3><span>Sources seen for this Device</span></div></div>
        <div class="compact-panel">${origins.map(titleCase).join(", ") || "None yet"}</div>
      </div>

      <div class="section">
        <div class="section-heading"><div><h3>Recent activity</h3><span>Latest normalized Events</span></div></div>
        ${activity.length ? `<div class="table-wrap"><table>
          <thead><tr><th>Type</th><th>State</th><th>Source</th><th>Time</th></tr></thead>
          <tbody>${activity.map(entry => `<tr class="clickable" onclick="location='#event/${entry.id}'">
            <td>${eventBadge(entry.event_type)}</td>
            <td>${stateBadge(entry.event_state)}</td>
            <td>${sourceBadges(entry.sources)}</td>
            <td>${fmtShort(entry.timestamp)}</td>
          </tr>`).join("")}</tbody>
        </table></div>` : '<div class="empty">No activity recorded</div>'}
      </div>

      ${topics.length ? `
      <div class="section">
        <h3 class="collapse-header collapsed" onclick="toggleCollapse(this)">Technical details</h3>
        <div class="collapse-body collapsed">
          <div class="compact-panel">
            <div class="meta"><strong>Device ID</strong><br>${item.id}</div>
            <div class="meta" style="margin-top:0.75rem"><strong>ONVIF event topics</strong><br>${topics.join("<br>")}</div>
          </div>
        </div>
      </div>` : ""}
    `);
  } catch (error) { showError(error.message); }
}

async function areas() {
  showLoading();
  try {
    const [list, deviceList] = await Promise.all([
      api("/areas?include_disabled=true"),
      api("/devices?include_disabled=true"),
    ]);
    inventoryAreas = list;
    inventoryDevices = deviceList;
    showContent(`
      <div class="page-header">
        <div>
          <div class="eyebrow">Inventory</div>
          <h2>Areas</h2>
          <div class="meta">Correlation boundaries that keep related activity together.</div>
        </div>
        <div class="page-actions">
          <a href="#devices" class="button button-ghost">Back to Devices</a>
          <button class="button button-primary" onclick="addArea()">Create Area</button>
        </div>
      </div>
      ${list.length === 0 ? `<div class="empty-state">
        <div class="empty-icon">⌂</div>
        <h3>Create your first Area</h3>
        <p>Start with a meaningful physical boundary, such as Front entrance or Garage.</p>
        <button class="button button-primary" onclick="addArea()">Create Area</button>
      </div>` : `
      <div class="resource-list inventory-list">
        ${list.map(area => {
          const members = deviceList.filter(device => device.area_id === area.id);
          return `<div class="resource-row area-row ${area.enabled ? "" : "resource-disabled"}">
            <span class="status-indicator ${area.enabled ? "online" : "idle"}"></span>
            <div class="resource-main">
              <strong>${area.name}</strong>
              <span>${area.location || "No location description"}</span>
            </div>
            <div class="resource-context">${plural(members.length, "Device")}</div>
            <div class="resource-links">${members.slice(0, 3).map(device => `<a href="#device/${device.id}">${device.name || device.id}</a>`).join("")}${members.length > 3 ? `<span>+${members.length - 3}</span>` : ""}</div>
            <div class="resource-actions">
              <button class="icon-button" onclick="editArea('${area.id}')">Edit</button>
              <button class="icon-button danger-text" onclick="deleteArea('${area.id}')">Delete</button>
            </div>
          </div>`;
        }).join("")}
      </div>`}
    `);
  } catch (error) { showError(error.message); }
}

/* ─── System Status ─── */


async function systemStatus() {
  showLoading();
  try {
    const diagnostics = await api("/diagnostics");
    const status = diagnostics.status;
    const serviceRows = diagnostics.services.map(service => ({
      ...service,
      type: "service",
      capabilities: [],
      details: service.metrics,
    }));
    showContent(`
      <div class="page-header">
        <div>
          <h2>System</h2>
          <div class="meta">Runtime health and integration diagnostics.</div>
        </div>
        ${operationalBadge(status.state)}
      </div>
      ${restartBanner(status)}
      <dl class="detail-facts section">
        <div><dt>Version</dt><dd>v${status.version}</dd></div>
        <div><dt>Active recordings</dt><dd>${status.active_recordings}</dd></div>
        <div><dt>Integrations</dt><dd>${status.integrations.healthy}/${status.integrations.total} healthy</dd></div>
      </dl>
      <div class="section">
        <h3>Core services</h3>
        ${renderIntegrationRows(serviceRows, true)}
      </div>
      <div class="section">
        <h3>Integrations (${diagnostics.integrations.length})</h3>
        ${renderIntegrationRows(diagnostics.integrations, true)}
      </div>
    `);
  } catch (error) { showError(error.message); }
}

/* ─── Mobile sidebar ─── */
window.toggleSidebar = function() {
  const aside = document.querySelector("aside");
  const overlay = document.getElementById("sidebar-overlay");
  const isOpen = aside.classList.toggle("open");
  overlay.classList.toggle("hidden");
  document.body.style.overflow = isOpen ? "hidden" : "";
};

/* ─── Sidebar ─── */

async function updateRecentEpisodes(list = null) {
  const el = $("#recent-episodes-sidebar");
  try {
    const recent = (list || await api("/episodes?limit=8")).slice(0, 8);
    el.innerHTML = `<div class="label">Recent episodes</div>
      ${recent.length
        ? recent.map(e => `<a href="#episode/${e.id}">${stateBadge(e.state)} ${trunc(e.primary_area_id || "?", 22)}</a>`).join("")
        : '<span class="sidebar-empty">No episodes yet</span>'}
    `;
  } catch {
    el.innerHTML = '<div class="label">Recent episodes</div><span class="sidebar-empty">Unavailable</span>';
  }
}

/* ─── Sidebar status mini-view ─── */

async function updateSidebarStatus() {
  const el = $("#sidebar-status");
  try {
    const status = await api("/status");
    $("#app-version").textContent = status.version ? `v${status.version}` : "";
    const indicator = status.restart_required ? "warning" : operationalIndicator(status.state);
    const label = status.restart_required ? "Restart required" : ({
      healthy: "All systems operational",
      degraded: "Attention needed",
      unavailable: "System unavailable",
    }[status.state] || "Status unknown");
    el.innerHTML = `<div class="sidebar-status">
      <span class="dot ${indicator}" title="${label}"></span>
      <span class="label">${label}</span>
      <span class="label" style="margin-left:auto">${status.active_recordings ? plural(status.active_recordings, "rec") : ""}</span>
    </div>`;
  } catch {
    el.innerHTML = `<div class="sidebar-status"><span class="dot offline"></span><span class="label">Offline</span></div>`;
  }
}

// Poll sidebar status every 10s
setInterval(updateSidebarStatus, 10000);
updateSidebarStatus();
updateRecentEpisodes();

navigate();
