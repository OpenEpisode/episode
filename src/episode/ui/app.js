const API = "/api/v1";
const LS_THEME = "episode-theme";

const $ = (s, p = document) => p.querySelector(s);
const $$ = (s, p = document) => [...p.querySelectorAll(s)];

/* ─── Helpers ─── */

async function api(path) {
  const r = await fetch(API + path);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return escapeApiData(await r.json());
}

function escapeApiData(value) {
  if (typeof value === "string") return escHtml(value);
  if (Array.isArray(value)) return value.map(escapeApiData);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, escapeApiData(item)])
    );
  }
  return value;
}
async function apiBlob(path) {
  const r = await fetch(API + path);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.blob();
}

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
function sourceBadges(sources) {
  if (!Array.isArray(sources)) return sources || "";
  return sources.map(s => `<span class="label">${s}</span>`).join(" ");
}
function fmt(d) {
  if (!d) return "-";
  const dt = new Date(d);
  const pad = n => String(n).padStart(2, "0");
  return `${dt.getFullYear()}-${pad(dt.getMonth()+1)}-${pad(dt.getDate())} ${pad(dt.getHours())}:${pad(dt.getMinutes())}:${pad(dt.getSeconds())}`;
}
function fmtShort(d) {
  if (!d) return "-";
  const dt = new Date(d);
  const pad = n => String(n).padStart(2, "0");
  const ms = dt.getMilliseconds();
  let s = `${dt.getFullYear()}-${pad(dt.getMonth()+1)}-${pad(dt.getDate())} ${pad(dt.getHours())}:${pad(dt.getMinutes())}:${pad(dt.getSeconds())}`;
  if (ms) s += `.${String(ms).padStart(3, "0")}`;
  return s;
}
function plural(n, s) { return `${n} ${s}${n === 1 ? "" : "s"}`; }
function trunc(s, n = 40) { return s && s.length > n ? s.slice(0, n) + "\u2026" : s; }
function toggleCollapse(el) {
  const body = el.nextElementSibling;
  el.classList.toggle("collapsed");
  body.classList.toggle("collapsed");
}
function fmtDuration(start, end) {
  if (!start || !end) return "";
  const s = new Date(end) - new Date(start);
  if (s < 0) return "-";
  const totalSec = Math.floor(s / 1000);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const sec = totalSec % 60;
  const pad = n => String(n).padStart(2, "0");
  if (h > 0) return `${h}:${pad(m)}:${pad(sec)}`;
  return `${m}:${pad(sec)}`;
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

/* ─── Router ─── */

function navigate() {
  const hash = location.hash.slice(1) || "episodes";
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
    episode, event, device, system: systemStatus,
  };
  if (view === "evidence" && args.length && args[0].includes("-")) {
    return evidenceDetail(args[0]);
  }
  if (view === "episode" && args.length > 1) {
    return episode(args[0]);
  }
  (routes[view] || episodes)(...args);
}
window.addEventListener("hashchange", navigate);

/* ─── Views ─── */

async function episodes() {
  showLoading();
  try {
    const list = await api("/episodes?limit=200");
    const epIds = list.map(e => e.id).join(",");
    const covers = epIds ? await api("/covers?ids=" + encodeURIComponent(epIds)) : {};

    showContent(`
      <div class="page-header"><h2>Episodes</h2></div>
      ${list.length === 0 ? '<div class="empty">No episodes yet</div>' : `
      <div class="card-grid">
        ${list.map(e => `
          <a href="#episode/${e.id}" class="card episode-card" style="text-decoration:none;color:inherit;display:block">
            ${covers[e.id] ? `<div class="episode-cover"><img src="${API}/evidence/${covers[e.id]}/file" loading="lazy"></div>` : ""}
            <div class="episode-card-body">
              <div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:0.5rem">
                <h3>${trunc(e.primary_area_id || "?", 24)}</h3>
                ${stateBadge(e.state)}
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
    `);
    updateRecentEpisodes(list);
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
      const isClosed = ep.state === "closed";
      const timelapseDevices = isClosed ? [...new Set(evidence.filter(e => e.evidence_type === "snapshot").map(e => e.device_id).filter(Boolean))] : [];
      const allEv = [...evidence].sort((a,b) => new Date(a.timestamp) - new Date(b.timestamp));
      _carouselItems = allEv;
      const recordings = evidence.filter(e => e.evidence_type === "recording");
      const snapshots = evidence.filter(e => e.evidence_type === "snapshot");

      showContent(`
      <div class="breadcrumbs"><a href="#episodes">Episodes</a> <span class="sep">›</span> <span>${trunc(ep.primary_area_id || ep.id, 40)}</span></div>
      <div class="detail-header">
        <h2>${trunc(ep.primary_area_id || "?", 40)} ${stateBadge(ep.state)} ${dur ? `<span class="badge badge-duration">${dur}</span>` : ""}</h2>
        <div class="subtitle">
          ${ep.id} &middot; ${plural(ep.event_count, "event")}, ${plural(ep.evidence_count, "evidence")}
          &middot; ${fmt(ep.start_time)} ${ep.last_event_time ? `\u2192 ${fmt(ep.last_event_time)}` : ""}
        </div>
      </div>

      ${timelapseDevices.map(s => `
      <div class="section">
        <h3>Timelapse — ${trunc(s, 30)}</h3>
        <video src="${API}/episodes/${ep.id}/timelapse?device_id=${encodeURIComponent(s)}" controls autoplay muted style="max-width:480px;width:100%;border-radius:var(--radius);background:#000"></video>
      </div>`).join("")}

      ${recordings.length ? `
      <div class="section">
        <h3>Recordings (${recordings.length})</h3>
        <div class="evidence-grid">${recordings.sort((a,b) => new Date(a.timestamp) - new Date(b.timestamp)).map(ev => renderEvidenceItem(ev, allEv.indexOf(ev))).join("")}</div>
      </div>` : ""}

      ${snapshots.length ? `
      <div class="section">
        <div class="collapse-header collapsed" onclick="toggleCollapse(this)">
          <h3>Snapshots (${snapshots.length})</h3>
        </div>
        <div class="collapse-body collapsed">
          <div class="section-sub">
            <div class="evidence-grid">${snapshots.sort((a,b) => new Date(a.timestamp) - new Date(b.timestamp)).map(ev => renderEvidenceItem(ev, allEv.indexOf(ev))).join("")}</div>
          </div>
        </div>
      </div>` : ""}

      <div class="section">
        <div class="collapse-header collapsed" onclick="toggleCollapse(this)">
          <h3>Activity (${ep.event_count})</h3>
        </div>
        <div class="collapse-body collapsed">
        ${events.length === 0 ? '<div class="empty">No activity</div>' : `
        <table>
          <thead><tr><th>Type</th><th>Device</th><th>State</th><th>Time</th><th>Source</th></tr></thead>
          <tbody>
            ${events.map(e => `
              <tr class="clickable" onclick="location='#event/${e.id}'">
                <td>${eventBadge(e.event_type)}</td>
                <td>${trunc(e.device_id, 20)}</td>
                <td>${stateBadge(e.event_state)}</td>
                <td>${fmtShort(e.timestamp)}</td>
                <td>${sourceBadges(e.sources)}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>`}
        </div>
      </div>
    `);
  } catch (e) { showError(e.message); }
}

async function events(device_id) {
  showLoading();
  try {
    const devices = await api("/devices");
    const params = device_id ? `?device_id=${device_id}` : "";
    const list = await api("/events" + params);
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
      ${snapHtml}
      ${ev.has_raw_payload ? `
        <div class="section">
          <h3>Raw Payload</h3>
          <div class="meta" style="margin-bottom:0.5rem">Original vendor payload preserved by Episode.</div>
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
    const text = await r.text();
    el.innerHTML = `<pre class="payload-xml">${escHtml(text)}</pre>`;
  } catch (e) {
    el.innerHTML = `<div class="meta" style="color:var(--danger)">Failed to load: ${e.message}</div>`;
  }
};
function escHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

async function evidence(device_id) {
  showLoading();
  try {
    const params = device_id ? `?device_id=${device_id}` : "";
    const list = await api("/evidence" + params);
    showContent(`
      <div class="page-header"><h2>Evidence</h2></div>
      ${list.length === 0 ? '<div class="empty">No evidence</div>' : (_carouselItems=list, renderEvidenceGrid(list))}
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
  return `<div class="evidence-item" tabindex="0" role="button" onclick="showCarousel(_carouselItems, ${i})" onkeydown="if(event.key==='Enter')showCarousel(_carouselItems, ${i})">
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
  _carouselItems = items.filter(ev => ev.evidence_type !== "payload");
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

function titleCase(value) {
  return String(value || "")
    .replaceAll("_", " ")
    .replace(/\b\w/g, letter => letter.toUpperCase());
}

function deviceStatus(connectors) {
  const onvif = connectors.find(c => c.type === "onvif");
  if (onvif) return { online: connectorHealthy(onvif), label: connectorHealthy(onvif) ? "Online" : "Degraded" };
  const online = connectors.some(connectorHealthy);
  return { online, label: online ? "Online" : "Not connected" };
}

function capabilityBadges(capabilities) {
  return (capabilities || [])
    .filter(capability => capability !== "events")
    .map(capability => `<span class="badge badge-active">${titleCase(capability)}</span>`)
    .join(" ");
}

async function devices() {
  showLoading();
  try {
    const [list, areaList, status] = await Promise.all([
      api("/devices"),
      api("/areas"),
      api("/status"),
    ]);
    const areaNames = Object.fromEntries(areaList.map(area => [area.id, area.name]));

    showContent(`
      <div class="page-header">
        <div>
          <h2>Devices</h2>
          <div class="meta">Physical cameras and equipment, independent of how they connect.</div>
        </div>
        <a href="#areas" class="btn-back">View areas</a>
      </div>
      ${list.length === 0 ? '<div class="empty">No devices configured</div>' : `
      <div class="card-grid">
        ${list.map(device => {
          const connectors = status.connectors.filter(connector => connector.device_id === device.id);
          const health = deviceStatus(connectors);
          const onvif = connectors.find(connector => connector.type === "onvif");
          const manufacturer = onvif?.manufacturer || device.metadata?.onvif?.manufacturer || "";
          const model = onvif?.model || device.metadata?.onvif?.model || "";
          const integrations = [
            ...(device.capabilities?.includes("onvif") ? ["ONVIF"] : []),
            ...(device.capabilities?.includes("isapi") ? ["ISAPI"] : []),
          ];
          return `
            <a href="#device/${device.id}" class="card device-card">
              <div class="device-card-header">
                <span class="status-indicator ${health.online ? "online" : "offline"}"></span>
                <div>
                  <h3>${device.name || device.id}</h3>
                  <div class="meta">${[manufacturer, model].filter(Boolean).join(" ") || titleCase(device.device_type) || "Network device"}</div>
                </div>
                <span class="badge ${health.online ? "badge-active" : "badge-inactive"}">${health.label}</span>
              </div>
              <div class="device-card-meta">
                <span>${areaNames[device.area_id] || device.area_id || "No area"}</span>
                <span>${device.ip_address || "No address"}</span>
              </div>
              <div class="device-card-section">
                <span class="label">Integrations</span>
                ${integrations.map(name => `<span class="badge badge-inactive">${name}</span>`).join(" ") || '<span class="meta">None</span>'}
              </div>
              <div class="device-card-section">${capabilityBadges(device.capabilities) || '<span class="meta">No capabilities discovered</span>'}</div>
              ${onvif?.last_event ? `<div class="meta">Last ONVIF activity ${fmtShort(onvif.last_event)}</div>` : ""}
            </a>`;
        }).join("")}
      </div>`}
    `);
  } catch (error) { showError(error.message); }
}

async function device(id) {
  showLoading();
  try {
    const [item, areaList, status, activity, evidenceList] = await Promise.all([
      api("/devices/" + encodeURIComponent(id)),
      api("/areas"),
      api("/status"),
      api("/events?device_id=" + encodeURIComponent(id) + "&limit=12"),
      api("/evidence?device_id=" + encodeURIComponent(id) + "&limit=24"),
    ]);
    const area = areaList.find(candidate => candidate.id === item.area_id);
    const connectors = status.connectors.filter(connector => connector.device_id === item.id);
    const health = deviceStatus(connectors);
    const onvif = connectors.find(connector => connector.type === "onvif");
    const manufacturer = onvif?.manufacturer || item.metadata?.onvif?.manufacturer || "";
    const model = onvif?.model || item.metadata?.onvif?.model || "";
    const firmware = onvif?.firmware_version || item.metadata?.onvif?.firmware_version || "";
    const origins = [...new Set(evidenceList.map(entry => entry.metadata?.origin).filter(Boolean))];
    const eventSources = [...new Set(activity.flatMap(entry => entry.sources || []))];
    const integrations = new Set();
    if (item.capabilities?.includes("onvif")) integrations.add("ONVIF");
    if (item.capabilities?.includes("isapi") || eventSources.includes("hikvision:isapi")) integrations.add("ISAPI");
    if (eventSources.includes("hikvision:alarm_server")) integrations.add("Alarm Server");
    if (origins.includes("ftp")) integrations.add("FTP");

    showContent(`
      <div class="breadcrumbs"><a href="#devices">Devices</a> <span class="sep">›</span> <span>${item.name || item.id}</span></div>
      <div class="detail-header device-detail-header">
        <div>
          <div class="device-title">
            <span class="status-indicator ${health.online ? "online" : "offline"}"></span>
            <h2>${item.name || item.id}</h2>
            <span class="badge ${health.online ? "badge-active" : "badge-inactive"}">${health.label}</span>
          </div>
          <div class="subtitle">${[manufacturer, model].filter(Boolean).join(" ") || titleCase(item.device_type) || "Network device"}</div>
        </div>
      </div>

      <div class="status-grid section">
        <div class="card"><div class="meta"><strong>Area</strong><br>${area?.name || item.area_id || "Not assigned"}</div></div>
        <div class="card"><div class="meta"><strong>Network address</strong><br>${item.ip_address || "Not configured"}</div></div>
        <div class="card"><div class="meta"><strong>Firmware</strong><br>${firmware || "Not reported"}</div></div>
      </div>

      <div class="section">
        <h3>Capabilities</h3>
        <div class="card">
          <div class="device-card-section">${capabilityBadges(item.capabilities) || '<span class="meta">No capabilities discovered</span>'}</div>
        </div>
      </div>

      <div class="section">
        <h3>Integrations</h3>
        <div class="status-grid">
          ${[...integrations].map(name => {
            const connector = connectors.find(candidate =>
              name === "ONVIF" ? candidate.type === "onvif" :
              name === "ISAPI" ? candidate.type === "isapi" : false
            );
            const online = connector ? connectorHealthy(connector) : true;
            return `<div class="card">
              <span class="status-indicator ${online ? "online" : "offline"}"></span>
              <div class="meta"><strong>${name}</strong><br>${connector ? (online ? "Connected" : connector.last_error || "Unavailable") : "Observed in recent data"}</div>
            </div>`;
          }).join("") || '<div class="empty">No integrations observed</div>'}
        </div>
      </div>

      ${onvif ? `
      <div class="section">
        <h3>ONVIF media</h3>
        <div class="status-grid">
          ${(onvif.profiles || []).map(profile => `
            <div class="card">
              <div class="meta">
                <strong>${profile.name || profile.token}</strong><br>
                ${profile.width} × ${profile.height} &middot; ${profile.encoding || "Unknown codec"}<br>
                Snapshot ${profile.snapshot ? "available" : "unavailable"}
              </div>
            </div>`).join("") || '<div class="empty">No media profiles reported</div>'}
        </div>
      </div>` : ""}

      <div class="section">
        <h3>Capture policy</h3>
        <div class="status-grid">
          <div class="card"><div class="meta"><strong>Recording</strong><br>${item.capabilities?.includes("video") ? "Available for Episode activity" : "Unavailable"}</div></div>
          <div class="card"><div class="meta"><strong>Automatic ONVIF snapshots</strong><br>${status.snapshotter.enabled ? "Enabled" : "Disabled"}</div></div>
          <div class="card"><div class="meta"><strong>ONVIF events</strong><br>${onvif ? (onvif.events_enabled ? "Enabled" : "Disabled by device policy") : "Unavailable"}</div></div>
          <div class="card"><div class="meta"><strong>Observed evidence</strong><br>${origins.map(titleCase).join(", ") || "None yet"}</div></div>
        </div>
      </div>

      <div class="section">
        <h3>Recent activity</h3>
        ${activity.length ? `<table>
          <thead><tr><th>Type</th><th>State</th><th>Source</th><th>Time</th></tr></thead>
          <tbody>${activity.map(entry => `<tr class="clickable" onclick="location='#event/${entry.id}'">
            <td>${eventBadge(entry.event_type)}</td>
            <td>${stateBadge(entry.event_state)}</td>
            <td>${sourceBadges(entry.sources)}</td>
            <td>${fmtShort(entry.timestamp)}</td>
          </tr>`).join("")}</tbody>
        </table>` : '<div class="empty">No activity recorded</div>'}
      </div>

      ${onvif ? `
      <div class="section">
        <h3 class="collapse-header collapsed" onclick="toggleCollapse(this)">Technical details</h3>
        <div class="collapse-body collapsed">
          <div class="card">
            <div class="meta"><strong>Device ID</strong><br>${item.id}</div>
            <div class="meta" style="margin-top:0.75rem"><strong>ONVIF event topics</strong><br>${(onvif.event_topics || []).join("<br>") || "None reported"}</div>
          </div>
        </div>
      </div>` : ""}
    `);
  } catch (error) { showError(error.message); }
}

async function areas() {
  showLoading();
  try {
    const [list, deviceList] = await Promise.all([api("/areas"), api("/devices")]);
    showContent(`
      <div class="page-header">
        <div>
          <h2>Areas</h2>
          <div class="meta">Coverage and correlation boundaries shared by related devices.</div>
        </div>
      </div>
      ${list.length === 0 ? '<div class="empty">No areas configured</div>' : `
      <div class="card-grid">
        ${list.map(area => {
          const members = deviceList.filter(device => device.area_id === area.id);
          return `<div class="card">
            <h3>${area.name}</h3>
            <div class="meta">${area.location || "No location description"} &middot; ${plural(members.length, "device")}</div>
            <div class="device-card-section">${members.map(device => `<a href="#device/${device.id}">${device.name || device.id}</a>`).join(" &middot; ") || '<span class="meta">No devices assigned</span>'}</div>
          </div>`;
        }).join("")}
      </div>`}
    `);
  } catch (error) { showError(error.message); }
}

/* ─── System Status ─── */


function connectorHealthy(c) {
  return c.healthy === undefined ? c.running : c.healthy;
}

function pluginIndicatorState(plugin) {
  if (plugin.state === "ready") return "online";
  if (plugin.state === "validating") return "warning";
  if (plugin.state === "not_installed") return "idle";
  return "offline";
}

function pluginSummary(plugin) {
  if (plugin.state === "not_installed") return "Optional · not installed";
  const details = [titleCase(plugin.state)];
  if (plugin.version) details.push("SDK " + plugin.version);
  if (plugin.architecture) details.push(plugin.architecture);
  return details.join(" · ");
}

async function systemStatus() {
  showLoading();
  try {
    const [st, plugins] = await Promise.all([api("/status"), api("/plugins")]);
    showContent(`
      <div class="page-header"><h2>System Status</h2></div>

      <div class="section">
        <h3>Server</h3>
        <div class="status-grid">
          <div class="card">
            <div class="status-indicator ${st.server.running ? "online" : "offline"}"></div>
            <div class="meta"><strong>Episode Server</strong><br>${st.server.running ? "Running" : "Stopped"} &middot; v${st.server.version}</div>
          </div>
        </div>
      </div>

      <div class="section">
        <h3>Episode Engine</h3>
        <div class="status-grid">
          <div class="card">
            <div class="status-indicator ${st.engine.running ? "online" : "offline"}"></div>
            <div class="meta"><strong>Engine</strong><br>${st.engine.running ? "Running" : "Stopped"} &middot; ${st.engine.timeout}s timeout</div>
          </div>
        </div>
      </div>

      <div class="section">
        <h3>Capture Actions</h3>
        <div class="status-grid">
          <div class="card">
            <div class="status-indicator ${st.recorder.running ? "online" : "offline"}"></div>
            <div class="meta"><strong>Recorder</strong><br>${st.recorder.running ? "Running" : "Stopped"} &middot; ${st.recorder.active_recordings} active recording${st.recorder.active_recordings === 1 ? "" : "s"}</div>
          </div>
          <div class="card">
            <div class="status-indicator ${st.snapshotter.enabled && !st.snapshotter.running ? "offline" : "online"}"></div>
            <div class="meta"><strong>ONVIF snapshots</strong><br>${st.snapshotter.enabled ? st.snapshotter.captured + " captured · " + st.snapshotter.failures + " failed" : "Disabled by policy"}</div>
          </div>

        </div>
      </div>

      <div class="section">
        <h3>Connectors (${st.connectors.length})</h3>
        <div class="status-grid">
          ${st.connectors.map(c => `
            <div class="card">
              <div class="status-indicator ${connectorHealthy(c) ? "online" : "offline"}"></div>
              <div class="meta">
                <strong>${c.name}</strong><br>
                <span class="badge ${connectorHealthy(c) ? "badge-active" : "badge-inactive"}">${c.type}</span>
                ${connectorHealthy(c) ? "" : " (unavailable)"}
                ${renderConnectorMeta(c)}
              </div>
            </div>
          `).join("")}
          ${st.connectors.length === 0 ? '<div class="empty">No connectors configured</div>' : ""}
        </div>
      </div>

      <div class="section">
        <h3>Native plugins (${plugins.length})</h3>
        <div class="status-grid">
          ${plugins.map(plugin => `
            <div class="card">
              <div class="status-indicator ${pluginIndicatorState(plugin)}"></div>
              <div class="meta">
                <strong>${plugin.name}</strong><br>
                <span class="badge ${plugin.state === "ready" ? "badge-active" : "badge-inactive"}">${plugin.kind}</span>
                ${pluginSummary(plugin)}
                ${plugin.error ? "<br>" + plugin.error : ""}
              </div>
            </div>
          `).join("")}
          ${plugins.length === 0 ? '<div class="empty">No native plugins recognized</div>' : ""}
        </div>
      </div>
    `);
  } catch (e) { showError(e.message); }
}

function renderConnectorMeta(c) {
  const parts = [];
  if (c.path) parts.push(c.path);
  if (c.host && c.port) parts.push(c.host + ":" + c.port);
  else if (c.port) parts.push("port " + c.port);
  if (c.url) parts.push(c.url);
  if (c.device_id) parts.push("device: " + trunc(c.device_id, 20));
  if (c.stream_active !== undefined) parts.push(c.stream_active ? "streaming" : "disconnected");
  if (c.connected !== undefined) parts.push(c.connected ? "connected" : "disconnected");
  if (c.subscribed) parts.push("subscribed");
  if (c.profiles?.length) parts.push(c.profiles.length + " media profile" + (c.profiles.length === 1 ? "" : "s"));
  if (c.events_enabled === false) parts.push("ONVIF events disabled");
  if (c.events_received !== undefined) parts.push(c.events_received + " ONVIF events");
  if (c.events_suppressed) parts.push(c.events_suppressed + " repeats suppressed");
  if (c.last_error) parts.push("error: " + c.last_error);
  if (c.last_event) parts.push("last: " + fmtShort(c.last_event));
  if (c.requests_handled !== undefined) parts.push(c.requests_handled + " requests");
  return parts.length ? "<br>" + parts.join(" &middot; ") : "";
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

async function updateRecentEpisodes(list) {
  const el = $("#recent-episodes-sidebar");
  const recent = list.slice(0, 8);
  el.innerHTML = `<div class="label">Recent episodes</div>
    ${recent.map(e => `<a href="#episode/${e.id}">${stateBadge(e.state)} ${trunc(e.primary_area_id || "?", 22)}</a>`).join("")}
  `;
}

/* ─── Sidebar status mini-view ─── */

async function updateSidebarStatus() {
  const el = $("#sidebar-status");
  try {
    const st = await api("/status");
    $("#app-version").textContent = st.server.version ? `v${st.server.version}` : "";
    const allOnline = st.connectors.every(connectorHealthy)
      && st.engine.running && st.recorder.running
      && (!st.snapshotter.enabled || st.snapshotter.running);
    const anyOnline = st.connectors.some(connectorHealthy)
      || st.engine.running || st.recorder.running || st.snapshotter.running;
    const status = allOnline ? "online" : anyOnline ? "warning" : "offline";
    const label = allOnline ? "All systems operational" : anyOnline ? "Partial outage" : "System offline";
    el.innerHTML = `<div class="sidebar-status">
      <span class="dot ${status}" title="${label}"></span>
      <span class="label">${label}</span>
      <span class="label" style="margin-left:auto">${st.recorder.active_recordings ? plural(st.recorder.active_recordings, "rec") : ""}</span>
    </div>`;
  } catch {
    el.innerHTML = `<div class="sidebar-status"><span class="dot offline"></span><span class="label">Offline</span></div>`;
  }
}

// Poll sidebar status every 10s
setInterval(updateSidebarStatus, 10000);
updateSidebarStatus();

navigate();
